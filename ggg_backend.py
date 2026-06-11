"""Backend for gnome-gamma-gui.

This module contains everything that is *not* GTK widgets:

* loading the engine (``gnome-gamma-tool.py``) without modifying it,
* colord operations (device list, baseline detection, cleanup),
* persistent config under ``~/.config/gnome-gamma-gui/``,
* :class:`ApplyJob`, which runs the engine in a pseudo-terminal and drives its
  interactive keep/revert prompt.

Design notes (see also SPEC.md):

* The engine's keep/revert prompt is *TTY-gated* (``sys.stdout.isatty()`` and
  ``termios`` on stdin), so it can only be driven through a real PTY, never a
  plain pipe. ``y`` keeps; any other character reverts immediately; a 10 s
  timeout reverts. The engine -- not the GUI -- is the source of truth for the
  outcome, which we read from its stdout ("Reverting settings" => reverted).
* The engine always clones ``device.get_profiles()[0]`` (the active profile), so
  to keep slider values absolute we reassign the *pristine baseline* as the
  device default before every apply. This causes a brief flash-to-neutral, which
  is expected behaviour, not a bug.

This module is Linux-only (colord typelib, ``pty``); it cannot be imported on
Windows.
"""

import os
import re
import json
import sys
import pty
import fcntl
import signal
import subprocess
import importlib.util

import gi

gi.require_version("Colord", "1.0")
from gi.repository import GLib, Colord  # noqa: E402


# --------------------------------------------------------------------------- #
# Engine loading
# --------------------------------------------------------------------------- #

_HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_PATH = os.path.join(_HERE, "gnome-gamma-tool.py")


def _load_engine():
    """Import the dash-named engine module so we can reuse its constants/classes.

    Only top-level imports + definitions run (``main()`` is ``__main__``-guarded).
    """
    spec = importlib.util.spec_from_file_location("ggt_engine", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_engine = _load_engine()
OUR_PREFIX = _engine.OUR_PREFIX            # "gnome-gamma-tool-"
ProfileMgr = _engine.ProfileMgr
GAMMA_TITLE_PREFIX = "gamma-tool:"          # description set on engine profiles


# --------------------------------------------------------------------------- #
# Neutral values / settings model
# --------------------------------------------------------------------------- #

NEUTRAL = {
    "gamma": [1.0, 1.0, 1.0],
    "contrast": [1.0, 1.0, 1.0],
    "brightness": [1.0, 1.0, 1.0],       # max output brightness
    "min_brightness": [0.0, 0.0, 0.0],
    "temperature": 6500,
}


def neutral_values():
    """A fresh deep copy of the neutral settings."""
    return {
        "gamma": list(NEUTRAL["gamma"]),
        "contrast": list(NEUTRAL["contrast"]),
        "brightness": list(NEUTRAL["brightness"]),
        "min_brightness": list(NEUTRAL["min_brightness"]),
        "temperature": NEUTRAL["temperature"],
    }


def values_to_argv(values, display_idx):
    """Build engine CLI args from a settings dict.

    The ``--arg=value`` form is used everywhere so negative channel values
    (e.g. contrast ``-1``) don't trip argparse's "expected one argument".
    """
    def trip(key):
        r, g, b = values[key]
        return f"{r:g}:{g:g}:{b:g}"

    return [
        f"--display={display_idx}",
        f"--gamma={trip('gamma')}",
        f"--contrast={trip('contrast')}",
        f"--brightness={trip('brightness')}",
        f"--min-brightness={trip('min_brightness')}",
        f"--temperature={int(values['temperature'])}",
    ]


def contrast_is_unsafe(values, eps=1e-3):
    """Contrast of (near) zero makes the whole screen grey; guard against it."""
    return any(abs(c) < eps for c in values["contrast"])


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #

class Config:
    """Tiny JSON config under ~/.config/gnome-gamma-gui/config.json.

    Shape::

        {
          "baselines": { "<device-id>": "<profile-id>" },
          "saved": [ {name, values, filename, profile_id, device_id}, ... ]
        }
    """

    def __init__(self):
        self.dir = os.path.join(GLib.get_user_config_dir(), "gnome-gamma-gui")
        self.path = os.path.join(self.dir, "config.json")
        self.data = {"baselines": {}, "saved": []}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.setdefault("baselines", {})
                self.data.setdefault("saved", [])
                self.data.update(loaded)
                self.data.setdefault("baselines", {})
                self.data.setdefault("saved", [])
        except (FileNotFoundError, ValueError):
            pass

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    # baseline ----------------------------------------------------------------
    def get_baseline_id(self, device_id):
        return self.data["baselines"].get(device_id)

    def set_baseline_id(self, device_id, profile_id):
        self.data["baselines"][device_id] = profile_id
        self.save()

    # saved profiles ----------------------------------------------------------
    def saved_profiles(self):
        return list(self.data["saved"])

    def add_saved(self, entry):
        # replace any existing entry with the same name
        self.data["saved"] = [e for e in self.data["saved"] if e.get("name") != entry["name"]]
        self.data["saved"].append(entry)
        self.save()

    def remove_saved(self, name):
        self.data["saved"] = [e for e in self.data["saved"] if e.get("name") != name]
        self.save()

    def saved_filenames(self):
        return {e.get("filename") for e in self.data["saved"] if e.get("filename")}


# --------------------------------------------------------------------------- #
# Colord backend
# --------------------------------------------------------------------------- #

class ColordError(Exception):
    pass


def _profile_is_ours(profile):
    """True if a (connected) profile was created by the engine/GUI."""
    fn = profile.get_filename() or ""
    pid = profile.get_id() or ""
    title = profile.get_title() or ""
    return (
        OUR_PREFIX in fn
        or OUR_PREFIX in pid
        or title.startswith(GAMMA_TITLE_PREFIX)
    )


class Backend:
    """Colord-facing operations, layered on the engine's ProfileMgr."""

    def __init__(self):
        try:
            self.mgr = ProfileMgr()
        except Exception as exc:  # pragma: no cover - depends on live colord
            raise ColordError(f"Could not connect to colord: {exc}") from exc
        self.config = Config()
        if self.mgr.get_device_count() == 0:
            raise ColordError("No display devices found via colord.")
        # Stable device ids (e.g. "xrandr-BOE-...") captured once. We re-fetch
        # the live device proxy by id on every operation so we never read a
        # stale profile list -- colord regenerates the EDID profile (new id)
        # across reboots/session resets, and a cached snapshot would miss it.
        self._device_ids = [d.get_id() for d in self.mgr.get_display_devices()]

    # devices -----------------------------------------------------------------
    def device_names(self):
        return self.mgr.get_device_names()

    def device_count(self):
        return self.mgr.get_device_count()

    def device_id(self, idx):
        return self._device_ids[idx]

    def _fresh_device(self, idx):
        """Re-fetch a live device proxy by its stable id and connect it.

        Always returns colord's *current* view (profiles included), unlike the
        snapshot the engine's ProfileMgr cached at startup. Falls back to the
        cached device only if the re-fetch fails.
        """
        dev_id = self._device_ids[idx]
        try:
            device = self.mgr.cd.find_device_sync(dev_id, None)
            if device:
                device.connect_sync()
                return device
        except Exception:
            pass
        device = self.mgr.get_display_devices()[idx]
        device.connect_sync()
        return device

    @staticmethod
    def _first_non_ours(device):
        """First associated profile that is not an engine-created one (connected)."""
        for profile in (device.get_profiles() or []):
            try:
                profile.connect_sync()
            except Exception:
                continue
            if not _profile_is_ours(profile):
                return profile
        return None

    def _find_profile_in(self, device, profile_id):
        for profile in (device.get_profiles() or []):
            try:
                profile.connect_sync()
            except Exception:
                continue
            if profile.get_id() == profile_id:
                return profile
        try:
            profile = self.mgr.cd.find_profile_sync(profile_id)
            if profile:
                profile.connect_sync()
                return profile
        except Exception:
            pass
        return None

    # baseline ----------------------------------------------------------------
    def detect_baseline(self, idx):
        """Find the pristine (non-engine) profile for a display and persist it.

        Reads colord fresh and returns the first associated profile that is not
        an engine-created one -- robust even when a ``gnome-gamma-tool-`` profile
        is currently the active default. Returns the colord profile id, or
        ``None`` if no non-engine profile is associated with the display.
        """
        device = self._fresh_device(idx)
        profile = self._first_non_ours(device)
        if profile is None:
            return None
        pid = profile.get_id()
        self.config.set_baseline_id(self._device_ids[idx], pid)
        return pid

    def baseline_id(self, idx, autodetect=True):
        pid = self.config.get_baseline_id(self._device_ids[idx])
        if pid is None and autodetect:
            pid = self.detect_baseline(idx)
        return pid

    def baseline_description(self, idx):
        """Human-readable description of the current baseline (for the UI)."""
        device = self._fresh_device(idx)
        profile = self._first_non_ours(device)
        if profile is None:
            return None
        return profile.get_title() or profile.get_filename() or profile.get_id()

    def ensure_baseline_default(self, idx):
        """Make the pristine baseline the device default before an apply.

        Detects the baseline live (the non-engine profile currently associated
        with the display) rather than trusting a remembered id, since colord can
        renumber the EDID profile across sessions. Self-heals the stored id.
        Causes a brief flash-to-neutral once it becomes the default.
        """
        device = self._fresh_device(idx)
        if not device.get_enabled():
            device.set_enabled_sync(True)
            device = self._fresh_device(idx)

        profile = self._first_non_ours(device)
        if profile is None:
            # Nothing but engine profiles are associated; try a remembered id as
            # a last resort before giving up.
            pid = self.config.get_baseline_id(self._device_ids[idx])
            if pid:
                profile = self._find_profile_in(device, pid)
        if profile is None:
            raise ColordError(
                "No pristine baseline profile is associated with this display. "
                "Reboot (so gsd-color recreates the default profile), then try "
                "again."
            )

        # Persist the live id (it may have changed since last run).
        self.config.set_baseline_id(self._device_ids[idx], profile.get_id())
        device.make_profile_default_sync(profile)

    # active profile ----------------------------------------------------------
    def active_profile(self, idx):
        """The current default profile (connected), or None."""
        device = self._fresh_device(idx)
        profiles = device.get_profiles() or []
        if not profiles:
            return None
        profiles[0].connect_sync()
        return profiles[0]

    def active_profile_filename(self, idx):
        profile = self.active_profile(idx)
        return profile.get_filename() if profile else None

    # enumeration / cleanup ---------------------------------------------------
    def _all_ggt_profiles(self):
        profiles = self.mgr.cd.get_profiles_sync() or []
        ours = []
        for profile in profiles:
            try:
                profile.connect_sync()
            except Exception:
                continue
            fn = profile.get_filename() or ""
            pid = profile.get_id() or ""
            if OUR_PREFIX in fn or OUR_PREFIX in pid:
                ours.append(profile)
        return ours

    @staticmethod
    def _delete_profile(device, profile):
        """Deassociate (if needed) and delete an engine profile + its file."""
        fname = profile.get_filename()
        # remove the device association if present (ignore failure -- it may not
        # be associated, in which case deleting the file is enough)
        try:
            device.remove_profile_sync(profile)
        except Exception:
            pass
        if fname and os.path.exists(fname):
            try:
                os.remove(fname)
            except OSError:
                pass

    def cleanup_after_keep(self, idx, keep_filename):
        """Delete every engine profile except the active one and saved ones.

        Never touches the pristine baseline (it isn't an engine profile).
        """
        device = self._fresh_device(idx)
        protected = set(self.config.saved_filenames())
        if keep_filename:
            protected.add(keep_filename)
        deleted = 0
        for profile in self._all_ggt_profiles():
            if (profile.get_filename() or None) in protected:
                continue
            self._delete_profile(device, profile)
            deleted += 1
        return deleted

    def remove_all_ggt_profiles(self, idx):
        """Remove every engine profile (respecting saved ones is the caller's job).

        Used by an explicit "remove all" action; reassigns baseline first so the
        screen returns to pristine.
        """
        self.ensure_baseline_default(idx)
        device = self._fresh_device(idx)
        removed = 0
        for profile in self._all_ggt_profiles():
            self._delete_profile(device, profile)
            removed += 1
        return removed


# --------------------------------------------------------------------------- #
# Apply job: drive the engine through a PTY
# --------------------------------------------------------------------------- #

class ApplyJob:
    """Run the engine in a PTY and drive its keep/revert prompt.

    All work happens on the GLib main loop (a periodic poll reads the PTY), so
    callbacks are safe to touch GTK widgets directly -- no threads involved.

    Callbacks (all optional, all invoked on the main loop):

    * ``on_prompt()``          -- engine reached the keep/revert prompt
    * ``on_countdown(secs)``   -- engine's countdown ticked (seconds remaining)
    * ``on_resolved(kept)``    -- engine committed (kept=True) or reverted (False)
    * ``on_error(message)``    -- the engine failed / could not be driven
    """

    POLL_MS = 80

    _RE_COUNTDOWN = re.compile(r"revert in (\d+) seconds")
    _RE_NEWPROFILE = re.compile(r"New profile is (.+)")

    def __init__(self, values, display_idx,
                 on_prompt=None, on_countdown=None, on_resolved=None, on_error=None):
        self.values = values
        self.display_idx = display_idx
        self.on_prompt = on_prompt
        self.on_countdown = on_countdown
        self.on_resolved = on_resolved
        self.on_error = on_error

        self._master = None
        self._proc = None
        self._buf = ""
        self._prompt_seen = False
        self._reverted = False
        self._new_profile = None
        self._finished = False
        self._poll_id = None

    # lifecycle ---------------------------------------------------------------
    def start(self):
        argv = [sys.executable, ENGINE_PATH] + values_to_argv(self.values, self.display_idx)
        try:
            master, slave = pty.openpty()
            self._master = master
            self._proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave)
            flags = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception as exc:
            self._fail(f"Could not launch engine: {exc}")
            return
        self._poll_id = GLib.timeout_add(self.POLL_MS, self._poll)

    @property
    def new_profile_filename(self):
        return self._new_profile

    # user actions ------------------------------------------------------------
    def keep(self):
        self._write("y")

    def revert(self):
        # any non-'y' character triggers an immediate revert
        self._write("n")

    def _write(self, ch):
        if self._master is None or self._finished:
            return
        try:
            os.write(self._master, ch.encode())
        except OSError:
            pass

    # internals ---------------------------------------------------------------
    def _poll(self):
        if self._finished:
            return False

        data = b""
        eof = False
        try:
            while True:
                chunk = os.read(self._master, 4096)
                if not chunk:
                    eof = True
                    break
                data += chunk
        except BlockingIOError:
            pass
        except OSError:
            # EIO on a pty master means the slave (child) is gone. Treat as EOF
            # but still consume anything we managed to read this tick.
            eof = True

        if data:
            self._consume(data.decode(errors="replace"))

        if eof or self._proc.poll() is not None:
            return self._maybe_finish(force=True)

        return True  # keep polling

    def _consume(self, text):
        self._buf += text

        if "Reverting settings" in self._buf:
            self._reverted = True

        m = self._RE_NEWPROFILE.search(self._buf)
        if m and not self._new_profile:
            self._new_profile = m.group(1).strip()

        # countdown / prompt
        for m in self._RE_COUNTDOWN.finditer(text):
            if not self._prompt_seen:
                self._prompt_seen = True
                if self.on_prompt:
                    self.on_prompt()
            if self.on_countdown:
                self.on_countdown(int(m.group(1)))

        # keep the buffer from growing without bound
        if len(self._buf) > 16384:
            self._buf = self._buf[-4096:]

    def _maybe_finish(self, force=False):
        if self._finished:
            return False
        if not force and self._proc.poll() is None:
            return True
        self._finished = True
        if self._poll_id is not None:
            # returning False from the poll callback already removes it, but be
            # safe if finish was triggered elsewhere
            self._poll_id = None
        rc = self._proc.poll()
        if rc is None:
            try:
                rc = self._proc.wait(timeout=2)
            except Exception:
                rc = None
        self._cleanup_fds()

        # A non-zero exit with no revert means the engine failed before/around
        # applying (e.g. "No such display"), not a normal keep. Both a normal
        # keep and a normal revert exit 0.
        if rc not in (0, None) and not self._reverted:
            if self.on_error:
                tail = self._buf.strip().splitlines()[-3:]
                detail = "\n".join(tail) if tail else "(no output)"
                self.on_error(f"Engine exited with code {rc}.\n{detail}")
            return False

        kept = not self._reverted
        if self.on_resolved:
            self.on_resolved(kept)
        return False

    def _fail(self, message):
        if self._finished:
            return
        self._finished = True
        self._cleanup_fds()
        self._terminate_proc()
        if self.on_error:
            self.on_error(message)

    def _terminate_proc(self):
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def _cleanup_fds(self):
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None

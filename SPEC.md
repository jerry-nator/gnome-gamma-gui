# gnome-gamma-gui — v1 Spec

A GTK GUI wrapper around the existing `gnome-gamma-tool.py` CLI. The CLI is the
engine that talks to colord/gsd-color and writes the VCGT color profile; the GUI's
job is to collect settings, apply them through the engine, and manage the resulting
profiles. Target: Ubuntu (25.10+) with GNOME 49 on Wayland. Verified working: the
engine successfully changes screen color via a colord-assigned profile on this setup.

## Hard constraints

- **Do not modify `gnome-gamma-tool.py`.** Treat it as the engine. Invoke it
  (subprocess or import), but leave its source untouched so upstream fixes can be
  merged cleanly from the `upstream` remote.
- GUI lives in new files alongside it (e.g. `gnome-gamma-gui.py` + any modules).
- No new heavy dependencies beyond what the engine and GTK already require.

## Task 0 — Read the engine first

Before writing UI code, read `gnome-gamma-tool.py` and its argument parser, and
report back the following so the GUI is built against reality, not assumptions:

1. The exact CLI flags and their value formats (gamma, contrast, brightness,
   min-brightness, temperature, display index, out-file, in-file).
2. The exact mechanics of the engine's interactive keep/revert prompt (CONFIRMED to
   exist): after assigning the profile the engine prints a prompt and runs its own
   countdown, keeping the profile only if the user confirms, otherwise auto-reverting.
   Extract precisely: (a) the prompt text and what it reads from stdin, (b) what input
   means "keep" (e.g. `y` + newline), (c) what input — if any — triggers an *immediate*
   revert vs. having to wait for timeout, (d) the timeout duration and that timeout =
   revert. The GUI **drives this prompt over stdin** (see Interaction model), so these
   details are load-bearing.
3. How it names, registers, and associates profiles with the device via colord,
   and how it identifies "the current profile" it clones from.
4. How the `--in-file` / base-profile option works (needed for the baseline logic
   below).

## Controls (all required in v1)

Expose every adjustment the engine supports, each with a sensible range, a live
numeric readout, and a reset-to-neutral button:

- **Gamma** — per-channel R / G / B. Neutral = 1.0.
- **Contrast** — per-channel R / G / B. Neutral = 1.0. Allow negative values
  (engine supports inversion; -1 inverts a channel). Warn/guard against exactly 0.
- **Brightness (max)** — per-channel R / G / B, range 0–1, neutral = 1.0.
- **Min brightness** — per-channel R / G / B, range 0–1, neutral = 0.0.
- **Color temperature** — single value, redshift-style, neutral = 6500 K.
- **Display selector** — index-based, default 0. (Framework 12 is single-panel, but
  support multiple for completeness; mirror the engine's display ordering.)

A master "Reset all to neutral" returns every control to its neutral value.

## Interaction model — apply, then confirm by driving the engine's prompt

The engine already implements the safety loop: when it assigns a profile it prints a
keep/revert prompt, runs its own countdown, and auto-reverts on timeout unless the
user confirms. **The GUI does not reimplement this — it drives it.** The engine is
launched as a long-lived subprocess with its stdin connected to a pipe, and the GUI's
modal is a front-end to the engine's live prompt.

1. User drags sliders freely. **Nothing is applied during dragging** — the change only
   lands once gsd-color picks up the new profile, so per-pixel application would feel
   broken.
2. User clicks **Apply**. The GUI launches the engine to generate + assign the profile.
   The engine applies it and begins its keep/revert prompt + countdown. The GUI shows a
   modal whose countdown is synchronized to the engine's, with **Keep** and **Revert**
   buttons.
The engine's prompt accepts two explicit inputs and also reverts on timeout — there
are therefore **three** paths to resolution, two driven by the modal and one by the
engine alone:

3. **Keep** → the GUI writes the engine's keep input to stdin (`y` + newline, confirm
   exact format in Task 0). The engine commits the profile; the GUI then runs profile
   cleanup (below).
4. **Revert** → the GUI writes the engine's revert input to stdin (`n` + newline,
   confirm format in Task 0). This triggers an *immediate* revert by the engine — the
   modal does not wait for the countdown.
5. **Countdown reaches 0 with no choice** → the engine auto-reverts on its own. The GUI
   sends nothing; it must detect that the engine has reverted (watch the engine's
   stdout/exit, not the GUI's own timer) and **auto-close the modal in sync** with the
   timeout reversal, so the UI never lingers on a screen the engine has already undone.

In all three cases the engine performs the actual keep or revert; the modal's only job
is to send `y`, send `n`, or send nothing and observe the timeout outcome.

Implementation notes that matter:

- Drive stdin via the subprocess pipe; read the engine's stdout so the GUI's countdown
  tracks the engine's real one rather than running an independent timer that can drift.
- The engine — not the GUI — is the source of truth for whether a profile was kept or
  reverted. Treat the engine's output/exit state as authoritative.
- Because the engine owns revert, the GUI does **not** need to reassign profiles via
  colord for the keep/revert flow itself; colord/`colormgr` is needed only for the
  separate profile-enumeration and cleanup tasks below.

## Baseline handling (correctness-critical)

The engine clones *the currently active profile* as its base. Applying repeatedly
would otherwise stack adjustments on top of each other. To prevent this:

- On first launch, capture the **pristine baseline profile** (the EDID-derived
  default colord generated for the display) and remember its ID.
- Every Apply must build from that baseline (via the engine's `--in-file`/base
  option, or by reassigning baseline before generating), so slider values are always
  **absolute from neutral**, never relative to the last applied profile.

## Profile management

Every Apply creates a new profile, so without management these accumulate. Required:

- **Tag GUI-created profiles** with a recognizable identifier (filename prefix and/or
  colord metadata, e.g. `ggg-`) so the GUI can enumerate only its own profiles.
- **Auto-cleanup on Keep:** after a kept Apply, delete all GUI-created profiles
  *except* the now-active one and any explicitly saved ones (below). Never delete the
  pristine baseline.
- **Cleanup on Revert:** after the engine reports it has reverted, delete the
  just-created profile.
- Run cleanup **only after the engine has resolved** the keep/revert outcome — never
  delete any profile while the engine's countdown is still live, since the engine may
  still revert to a prior profile. Let the engine settle, then clean up based on its
  reported outcome.

## Save / load named profiles

- **Save:** let the user name the current settings; the resulting profile is exempt
  from auto-cleanup and recorded (settings values + profile reference).
- **Load:** list saved profiles; selecting one applies it (through the same
  Apply→countdown flow) and sets the sliders to its stored values.
- Store saved-profile metadata somewhere persistent under the user's config dir
  (e.g. `~/.config/gnome-gamma-gui/`), not in the repo.

## Persistence requirement

Applied (kept) settings must persist across reboots and ordinary `apt` updates. This
should come for free from colord assigning the profile to the device and gsd-color
reapplying on login — confirm the kept profile remains device-associated and is not
auto-cleaned. Do not implement a separate autostart hack unless the profile
assignment proves not to persist on this setup.

## Tech stack

- Python 3 + PyGObject (GTK). Match the GTK major version available on the target
  (GTK 4 preferred on GNOME 49; confirm what's installed).
- Reuse the engine for profile generation/assignment; use `colormgr` or libcolord
  D-Bus for profile enumeration, assignment, and deletion.
- Keep it a small, launchable app (single entry point), no packaging/installer in v1.

## Out of scope for v1 (note as future)

- Live drag-to-preview (blocked by backend latency on GNOME/Wayland).
- Kelvin-accurate calibration (would need a colorimeter / measurement).
- Packaging, .desktop entry, distro packages.

## Testing note

The main machine where this is written may not apply color changes visibly (colord/
gsd-color only drive the screen on the GNOME-49 laptop). Develop and confirm the app
launches and runs without errors on the main machine; **all real color/persistence
verification happens on the laptop** after `git pull`.

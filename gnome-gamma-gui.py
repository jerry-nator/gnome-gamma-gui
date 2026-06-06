#!/usr/bin/env python3
"""gnome-gamma-gui -- a GTK4 front-end for gnome-gamma-tool.py.

The CLI tool is the engine (it writes the VCGT colour profile and talks to
colord/gsd-color); this GUI collects settings, applies them through the engine,
drives its keep/revert prompt, and manages the resulting profiles.

Run on the target GNOME/Wayland machine with:

    python3 gnome-gamma-gui.py

Requires PyGObject GTK 4 and the colord typelib (gir1.2-colord-1.0 on Debian/
Ubuntu) -- the same colord dependency the engine already needs.

See SPEC.md for the full design. This file is the UI; ggg_backend.py is the
colord / engine-driving logic.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

import ggg_backend as backend  # noqa: E402


APP_ID = "io.github.jerry_nator.GnomeGammaGui"


def make_description(text):
    """A small, italic, regular-weight helper label explaining a control."""
    lbl = Gtk.Label(xalign=0.0)
    lbl.set_wrap(True)
    lbl.set_markup(f"<i>{GLib.markup_escape_text(text)}</i>")
    lbl.add_css_class("caption")
    lbl.add_css_class("dim-label")
    lbl.set_margin_bottom(2)
    return lbl


# --------------------------------------------------------------------------- #
# Reusable controls
# --------------------------------------------------------------------------- #

class ChannelControl(Gtk.Box):
    """A single adjustable value: slider + numeric spin + reset-to-neutral.

    The scale and spin button share one adjustment so they stay in lock-step.
    """

    def __init__(self, label, lower, upper, step, neutral, digits, page=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.neutral = neutral

        self.adjustment = Gtk.Adjustment(
            value=neutral,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=page if page is not None else step * 10,
        )

        if label:
            lbl = Gtk.Label(label=label)
            lbl.set_width_chars(2)
            lbl.set_xalign(0.0)
            self.append(lbl)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                               adjustment=self.adjustment)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_mark(neutral, Gtk.PositionType.BOTTOM, None)
        self.append(self.scale)

        self.spin = Gtk.SpinButton(adjustment=self.adjustment, digits=digits)
        self.spin.set_numeric(True)
        self.spin.set_width_chars(6)
        self.append(self.spin)

        reset = Gtk.Button(icon_name="edit-undo-symbolic")
        reset.set_tooltip_text("Reset to neutral")
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self.reset())
        self.append(reset)

    def get_value(self):
        return self.adjustment.get_value()

    def set_value(self, value):
        self.adjustment.set_value(value)

    def reset(self):
        self.adjustment.set_value(self.neutral)


class RGBControl(Gtk.Frame):
    """A titled group of three :class:`ChannelControl`s (R/G/B) + group reset."""

    def __init__(self, title, lower, upper, step, neutral, digits, page=None,
                 description=None):
        super().__init__()
        self.neutral = neutral

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=10, margin_bottom=10,
                        margin_start=10, margin_end=10)
        self.set_child(outer)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_lbl = Gtk.Label(label=title)
        title_lbl.add_css_class("heading")
        title_lbl.set_xalign(0.0)
        title_lbl.set_hexpand(True)
        header.append(title_lbl)
        group_reset = Gtk.Button(label="Reset")
        group_reset.add_css_class("flat")
        group_reset.connect("clicked", lambda *_: self.reset())
        header.append(group_reset)
        outer.append(header)

        if description:
            outer.append(make_description(description))

        self.channels = []
        for name in ("R", "G", "B"):
            ch = ChannelControl(name, lower, upper, step, neutral, digits, page)
            self.channels.append(ch)
            outer.append(ch)

    def get_values(self):
        return [c.get_value() for c in self.channels]

    def set_values(self, values):
        for c, v in zip(self.channels, values):
            c.set_value(v)

    def reset(self):
        for c in self.channels:
            c.reset()


# --------------------------------------------------------------------------- #
# Apply modal -- front-end to the engine's live keep/revert prompt
# --------------------------------------------------------------------------- #

class ApplyModal(Gtk.Window):
    """Modal whose Keep/Revert/timeout map onto the engine's prompt."""

    def __init__(self, parent, on_keep, on_revert):
        super().__init__(title="Applying changes", transient_for=parent, modal=True)
        self.set_default_size(380, -1)
        self.set_deletable(False)
        self._on_keep = on_keep
        self._on_revert = on_revert

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.set_child(box)

        self.heading = Gtk.Label(label="Applying…")
        self.heading.add_css_class("title-3")
        box.append(self.heading)

        self.status = Gtk.Label(
            label="Waiting for the display to pick up the new profile…")
        self.status.set_wrap(True)
        box.append(self.status)

        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(1.0)
        box.append(self.progress)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.add_css_class("destructive-action")
        self.revert_btn.set_sensitive(False)
        self.revert_btn.connect("clicked", lambda *_: self._do_revert())
        self.keep_btn = Gtk.Button(label="Keep")
        self.keep_btn.add_css_class("suggested-action")
        self.keep_btn.set_sensitive(False)
        self.keep_btn.connect("clicked", lambda *_: self._do_keep())
        buttons.append(self.revert_btn)
        buttons.append(self.keep_btn)
        box.append(buttons)

        self._resolved = False

    def on_prompt(self):
        self.heading.set_label("Keep these changes?")
        self.status.set_label("The new colour profile is active. Keep it, or it "
                              "reverts automatically.")
        self.keep_btn.set_sensitive(True)
        self.revert_btn.set_sensitive(True)

    def on_countdown(self, seconds):
        self.status.set_label(
            f"Reverting automatically in {seconds} second"
            f"{'' if seconds == 1 else 's'}…")
        # engine's countdown starts at 10s
        self.progress.set_fraction(max(0.0, min(1.0, seconds / 10.0)))

    def _do_keep(self):
        if self._resolved:
            return
        self._resolved = True
        self._lock()
        self._on_keep()

    def _do_revert(self):
        if self._resolved:
            return
        self._resolved = True
        self._lock()
        self._on_revert()

    def _lock(self):
        self.keep_btn.set_sensitive(False)
        self.revert_btn.set_sensitive(False)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app, backend_obj):
        super().__init__(application=app, title="GNOME Gamma GUI")
        self.backend = backend_obj
        self.set_default_size(560, 720)
        self._job = None
        self._modal = None
        self._busy_values = None  # values being applied
        self._device_idx = 0

        self._build_actions()
        self._build_header()
        self._build_body()
        self._refresh_baseline_label()

    # -- construction ---------------------------------------------------------
    def _build_actions(self):
        for name, handler in (
            ("save", self.on_save),
            ("load", self.on_load),
            ("redetect", self.on_redetect_baseline),
            ("removeall", self.on_remove_all),
            ("resetall", lambda *_: self.reset_all()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _build_header(self):
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        menu = Gio.Menu()
        menu.append("Save current as…", "win.save")
        menu.append("Load saved profile…", "win.load")
        section = Gio.Menu()
        section.append("Re-detect baseline", "win.redetect")
        section.append("Remove all GUI profiles", "win.removeall")
        menu.append_section(None, section)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)

    def _build_body(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        # Persistent (non-overlay) scrollbar so the vertical scroll control
        # stays visible whenever content runs below the fold.
        scroller.set_overlay_scrolling(False)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
        scroller.set_child(body)

        # display selector ----------------------------------------------------
        disp_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        disp_row.append(Gtk.Label(label="Display:"))
        names = self.backend.device_names()
        self.display_dd = Gtk.DropDown.new_from_strings(names if names else ["(none)"])
        self.display_dd.set_hexpand(True)
        self.display_dd.connect("notify::selected", self.on_display_changed)
        disp_row.append(self.display_dd)
        body.append(disp_row)

        self.baseline_lbl = Gtk.Label(xalign=0.0)
        self.baseline_lbl.add_css_class("dim-label")
        self.baseline_lbl.set_wrap(True)
        body.append(self.baseline_lbl)

        # gamma + contrast (always visible, top of the window) ----------------
        self.gamma = RGBControl(
            "Gamma", 0.1, 3.0, 0.01, 1.0, 2, page=0.1,
            description="Adjusts midtone brightness via a power curve. Lower "
                        "values brighten midtones, higher values darken them. "
                        "1.0 is neutral.")
        self.contrast = RGBControl(
            "Contrast", -2.0, 2.0, 0.05, 1.0, 2, page=0.25,
            description="Spreads or compresses the tonal range. Below 1 reduces "
                        "contrast; negative values invert the channel. Avoid 0, "
                        "which makes the screen flat grey.")
        body.append(self.gamma)
        body.append(self.contrast)

        # colour temperature --------------------------------------------------
        temp_frame = Gtk.Frame()
        temp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                           margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        temp_title = Gtk.Label(label="Colour temperature (K)")
        temp_title.add_css_class("heading")
        temp_title.set_xalign(0.0)
        temp_box.append(temp_title)
        temp_box.append(make_description(
            "Warms or cools the whole image. Lower is warmer (redder), higher "
            "is cooler (bluer). 6500 K is neutral."))
        self.temperature = ChannelControl("", 1000, 10000, 100, 6500, 0, page=500)
        temp_box.append(self.temperature)
        temp_frame.set_child(temp_box)
        body.append(temp_frame)

        # brightness (advanced) -- collapsed by default -----------------------
        self.brightness = RGBControl(
            "Brightness (max)", 0.0, 1.0, 0.01, 1.0, 2, page=0.1,
            description="Caps the maximum output level per channel. Can only "
                        "dim the display, never brighten it. 1.0 is neutral.")
        self.min_brightness = RGBControl(
            "Min brightness", 0.0, 1.0, 0.01, 0.0, 2, page=0.1,
            description="Raises the minimum output level, lifting blacks toward "
                        "grey. 0.0 is neutral.")
        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                          margin_top=8)
        adv_box.append(self.brightness)
        adv_box.append(self.min_brightness)
        expander = Gtk.Expander()
        expander.set_use_markup(True)
        expander.set_label("<b>Brightness (advanced)</b>")
        expander.set_expanded(False)
        expander.set_child(adv_box)
        body.append(expander)

        # save / load (also available in the menu) ----------------------------
        sl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         margin_top=6, homogeneous=True)
        save_btn = Gtk.Button(label="Save profile…")
        save_btn.connect("clicked", self.on_save)
        load_btn = Gtk.Button(label="Load saved…")
        load_btn.connect("clicked", self.on_load)
        sl_row.append(save_btn)
        sl_row.append(load_btn)
        body.append(sl_row)

        # footer --------------------------------------------------------------
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                         margin_top=6, margin_bottom=10, margin_start=14, margin_end=14)
        reset_all = Gtk.Button(label="Reset all to neutral")
        reset_all.connect("clicked", lambda *_: self.reset_all())
        footer.append(reset_all)

        self.status_lbl = Gtk.Label(xalign=0.0)
        self.status_lbl.add_css_class("dim-label")
        self.status_lbl.set_hexpand(True)
        footer.append(self.status_lbl)

        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        footer.append(self.apply_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(scroller)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        outer.append(footer)
        self.set_child(outer)

    # -- settings model -------------------------------------------------------
    def current_values(self):
        return {
            "gamma": self.gamma.get_values(),
            "contrast": self.contrast.get_values(),
            "brightness": self.brightness.get_values(),
            "min_brightness": self.min_brightness.get_values(),
            "temperature": int(round(self.temperature.get_value())),
        }

    def load_values(self, values):
        self.gamma.set_values(values["gamma"])
        self.contrast.set_values(values["contrast"])
        self.brightness.set_values(values["brightness"])
        self.min_brightness.set_values(values["min_brightness"])
        self.temperature.set_value(values["temperature"])

    def reset_all(self):
        self.load_values(backend.neutral_values())
        self._set_status("Reset to neutral. Apply to take effect.")

    # -- helpers --------------------------------------------------------------
    def _set_status(self, text):
        self.status_lbl.set_label(text)

    def _set_busy(self, busy):
        self.apply_btn.set_sensitive(not busy)
        self.display_dd.set_sensitive(not busy)

    def _refresh_baseline_label(self):
        try:
            desc = self.backend.baseline_description(self._device_idx)
        except Exception:
            desc = None
        if desc:
            self.baseline_lbl.set_label(f"Baseline: {desc}")
        else:
            self.baseline_lbl.set_label(
                "Baseline: not yet detected — will detect on first Apply.")

    def on_display_changed(self, dd, _pspec):
        self._device_idx = dd.get_selected()
        self._refresh_baseline_label()

    # -- apply flow -----------------------------------------------------------
    def on_apply(self, *_):
        values = self.current_values()
        if backend.contrast_is_unsafe(values):
            self._confirm_unsafe_contrast(values)
            return
        self._start_apply(values)

    def _confirm_unsafe_contrast(self, values):
        dlg = Gtk.AlertDialog()
        dlg.set_message("Contrast is at (or near) zero")
        dlg.set_detail("A contrast of 0 makes the whole screen a flat grey. "
                       "Apply anyway?")
        dlg.set_buttons(["Cancel", "Apply anyway"])
        dlg.set_cancel_button(0)
        dlg.set_default_button(0)

        def done(d, res):
            try:
                choice = d.choose_finish(res)
            except GLib.Error:
                return
            if choice == 1:
                self._start_apply(values)

        dlg.choose(self, None, done)

    def _start_apply(self, values):
        idx = self._device_idx
        # Enforce the pristine baseline so the engine clones it (absolute values,
        # no stacking). A brief flash-to-neutral here is expected.
        try:
            self.backend.ensure_baseline_default(idx)
        except backend.ColordError as exc:
            self._error("Could not prepare baseline", str(exc))
            return

        self._refresh_baseline_label()
        self._busy_values = values
        self._set_busy(True)
        self._set_status("Applying…")

        self._modal = ApplyModal(self, on_keep=self._modal_keep, on_revert=self._modal_revert)
        self._modal.present()

        self._job = backend.ApplyJob(
            values, idx,
            on_prompt=self._on_prompt,
            on_countdown=self._on_countdown,
            on_resolved=self._on_resolved,
            on_error=self._on_job_error,
        )
        self._job.start()

    def _modal_keep(self):
        if self._job:
            self._job.keep()

    def _modal_revert(self):
        if self._job:
            self._job.revert()

    def _on_prompt(self):
        if self._modal:
            self._modal.on_prompt()

    def _on_countdown(self, seconds):
        if self._modal:
            self._modal.on_countdown(seconds)

    def _on_resolved(self, kept):
        idx = self._device_idx
        if self._modal:
            self._modal.destroy()
            self._modal = None

        if kept:
            keep_file = (self._job.new_profile_filename
                         or self.backend.active_profile_filename(idx))
            try:
                deleted = self.backend.cleanup_after_keep(idx, keep_file)
            except Exception as exc:
                deleted = 0
                self._error("Cleanup warning",
                            f"Profile kept, but cleanup failed: {exc}")
            self._last_applied_file = keep_file
            self._set_status(
                f"Applied and kept. (cleaned up {deleted} old profile"
                f"{'' if deleted == 1 else 's'})")
        else:
            self._set_status("Reverted by the engine. No changes kept.")

        self._job = None
        self._busy_values = None
        self._set_busy(False)
        self._refresh_baseline_label()

    def _on_job_error(self, message):
        if self._modal:
            self._modal.destroy()
            self._modal = None
        self._job = None
        self._busy_values = None
        self._set_busy(False)
        self._error("Apply failed", message)

    # -- menu actions ---------------------------------------------------------
    def on_redetect_baseline(self, *_):
        idx = self._device_idx
        pid = self.backend.detect_baseline(idx)
        self._refresh_baseline_label()
        if pid:
            self._set_status("Baseline re-detected.")
        else:
            self._error("Baseline not found",
                        "No non-GUI profile is associated with this display. "
                        "If a gnome-gamma-tool profile is currently active, "
                        "remove it (or run the CLI with -r) and try again.")

    def on_remove_all(self, *_):
        idx = self._device_idx
        dlg = Gtk.AlertDialog()
        dlg.set_message("Remove all GUI-created profiles?")
        dlg.set_detail("This deletes every gnome-gamma-tool profile (including "
                       "saved ones' files) and returns the display to its "
                       "pristine baseline.")
        dlg.set_buttons(["Cancel", "Remove all"])
        dlg.set_cancel_button(0)
        dlg.set_default_button(0)

        def done(d, res):
            try:
                choice = d.choose_finish(res)
            except GLib.Error:
                return
            if choice != 1:
                return
            try:
                removed = self.backend.remove_all_ggt_profiles(idx)
                self._set_status(f"Removed {removed} profile"
                                 f"{'' if removed == 1 else 's'}.")
            except Exception as exc:
                self._error("Remove failed", str(exc))
            self._refresh_baseline_label()

        dlg.choose(self, None, done)

    def on_save(self, *_):
        self._text_entry_dialog(
            title="Save profile",
            message="Name for these settings:",
            on_ok=self._do_save)

    def _do_save(self, name):
        name = name.strip()
        if not name:
            return
        idx = self._device_idx
        values = self.current_values()
        active = self.backend.active_profile(idx)
        filename = active.get_filename() if active else None
        profile_id = active.get_id() if active else None
        entry = {
            "name": name,
            "values": values,
            "filename": filename,
            "profile_id": profile_id,
            "device_id": self.backend.device_id(idx),
        }
        self.backend.config.add_saved(entry)
        if filename and backend.OUR_PREFIX in (filename or ""):
            self._set_status(f"Saved '{name}' (current applied profile exempt "
                             f"from cleanup).")
        else:
            self._set_status(f"Saved '{name}'. Apply then Keep to make it "
                             f"persistent.")

    def on_load(self, *_):
        saved = self.backend.config.saved_profiles()
        if not saved:
            self._error("No saved profiles", "You haven't saved any profiles yet.")
            return

        names = [e["name"] for e in saved]
        dialog = Gtk.Window(title="Load saved profile", transient_for=self, modal=True)
        dialog.set_default_size(320, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        dialog.set_child(box)
        box.append(Gtk.Label(label="Choose a saved profile:", xalign=0.0))
        dd = Gtk.DropDown.new_from_strings(names)
        box.append(dd)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.destroy())
        delete = Gtk.Button(label="Delete")
        delete.add_css_class("destructive-action")
        load = Gtk.Button(label="Load")
        load.add_css_class("suggested-action")

        def do_delete(*_):
            entry = saved[dd.get_selected()]
            self.backend.config.remove_saved(entry["name"])
            self._set_status(f"Deleted saved profile '{entry['name']}'.")
            dialog.destroy()

        def do_load(*_):
            entry = saved[dd.get_selected()]
            dialog.destroy()
            self.load_values(entry["values"])
            self._set_status(f"Loaded '{entry['name']}'. Applying…")
            self._start_apply(entry["values"])

        delete.connect("clicked", do_delete)
        load.connect("clicked", do_load)
        buttons.append(delete)
        buttons.append(cancel)
        buttons.append(load)
        box.append(buttons)
        dialog.present()

    # -- small dialogs --------------------------------------------------------
    def _text_entry_dialog(self, title, message, on_ok):
        dialog = Gtk.Window(title=title, transient_for=self, modal=True)
        dialog.set_default_size(320, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        dialog.set_child(box)
        box.append(Gtk.Label(label=message, xalign=0.0))
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        box.append(entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.destroy())
        ok = Gtk.Button(label="Save")
        ok.add_css_class("suggested-action")

        def confirm(*_):
            text = entry.get_text()
            dialog.destroy()
            on_ok(text)

        ok.connect("clicked", confirm)
        entry.connect("activate", confirm)
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        dialog.present()

    def _error(self, message, detail):
        dlg = Gtk.AlertDialog()
        dlg.set_message(message)
        dlg.set_detail(detail)
        dlg.set_buttons(["OK"])
        dlg.show(self)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

class GammaGuiApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self._backend = None

    def do_activate(self):
        win = self.props.active_window
        if win:
            win.present()
            return

        try:
            if self._backend is None:
                self._backend = backend.Backend()
        except backend.ColordError as exc:
            self._fatal(str(exc))
            return

        MainWindow(self, self._backend).present()

    def _fatal(self, detail):
        win = Gtk.ApplicationWindow(application=self, title="GNOME Gamma GUI")
        win.set_default_size(420, -1)
        dlg = Gtk.AlertDialog()
        dlg.set_message("colord is not available")
        dlg.set_detail(
            detail + "\n\nThis tool only works on GNOME/Cinnamon with colord "
            "(install gir1.2-colord-1.0 on Debian/Ubuntu).")
        dlg.set_buttons(["Quit"])

        def done(d, res):
            try:
                d.choose_finish(res)
            except GLib.Error:
                pass
            win.close()

        win.present()
        dlg.choose(win, None, done)


def main():
    app = GammaGuiApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

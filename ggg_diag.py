#!/usr/bin/env python3
"""Diagnostic for gnome-gamma-gui baseline detection.

Run on the laptop:

    python3 ggg_diag.py

It connects to colord the same way the app does and dumps, for every display
device, the full profile list with the exact fields the app's baseline
detection looks at -- plus how each profile would be classified. Paste the
output back so the detection bug can be pinned down.

Read-only: it does not change any profile or device.
"""

import gi

gi.require_version("Colord", "1.0")
from gi.repository import Colord  # noqa: E402

OUR_PREFIX = "gnome-gamma-tool-"
GAMMA_TITLE_PREFIX = "gamma-tool:"


def classify(filename, pid, title):
    return (
        OUR_PREFIX in (filename or "")
        or OUR_PREFIX in (pid or "")
        or (title or "").startswith(GAMMA_TITLE_PREFIX)
    )


def main():
    cd = Colord.Client()
    cd.connect_sync()

    devices = cd.get_devices_sync() or []
    print(f"colord total devices: {len(devices)}")

    display_seen = 0
    for dev in devices:
        try:
            dev.connect_sync()
        except Exception as exc:
            print(f"  (device connect failed: {exc!r})")
            continue

        if dev.get_kind() != Colord.DeviceKind.DISPLAY:
            continue

        display_seen += 1
        print("=" * 64)
        print(f"DISPLAY device id : {dev.get_id()!r}")
        try:
            print(f"  enabled         : {dev.get_enabled()}")
        except Exception as exc:
            print(f"  enabled         : <error {exc!r}>")

        profiles = dev.get_profiles() or []
        print(f"  profile count   : {len(profiles)}")

        first_non_ours = None
        for i, prof in enumerate(profiles):
            try:
                prof.connect_sync()
                fn = prof.get_filename()
                pid = prof.get_id()
                title = prof.get_title()
                ours = classify(fn, pid, title)
                print(f"   [{i}] ours={ours}")
                print(f"        id       = {pid!r}")
                print(f"        title    = {title!r}")
                print(f"        filename = {fn!r}")
                if not ours and first_non_ours is None:
                    first_non_ours = pid
            except Exception as exc:
                print(f"   [{i}] connect/read FAILED: {exc!r}")

        print(f"  -> detection would pick baseline id: {first_non_ours!r}")

    if display_seen == 0:
        print("No DISPLAY-kind devices found.")


if __name__ == "__main__":
    main()

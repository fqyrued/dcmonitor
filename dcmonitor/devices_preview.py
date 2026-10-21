"""Preview of the rack popup and the Devices window on a second computer
(the Mac) while the real dcmonitor runs on the VM.

This is not a second dcmonitor (CLAUDE.md 2.2: only one may run). It reads
only PDUs (the racks named, or all of them when none are named), read only,
every 30 s like the real PDU job, and the cabinet list. No AC unit, UPS or generator is contacted ("Poll
all now" polls only those PDUs). Its database is a temporary file deleted
when the window closes, so the real history is not touched; it plays no
sounds.

    python -m dcmonitor.devices_preview 21 60 --file "<path to the .xlsx>"
    python -m dcmonitor.devices_preview --file "<path to the .xlsx>"     (every PDU)

Without --file, [cabinet_list] file in config.toml is used. Run it from the
program folder (config.toml and pdus.toml are read from there).
"""

import argparse
import dataclasses
import logging
import sys
import tempfile
import time
from pathlib import Path

try:
    from dcmonitor import config as config_module
    from dcmonitor import panel as panel_module
except ImportError:  # run as a file rather than with -m
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dcmonitor import config as config_module
    from dcmonitor import panel as panel_module


def main() -> int:
    parser = argparse.ArgumentParser(description="dcmonitor Devices preview (PDUs of the "
                                                 "racks named only, no other device)")
    parser.add_argument("racks", nargs="*", type=int,
                        help="rack numbers, e.g. 21 60 (none: every PDU)")
    parser.add_argument("--file", help="the cabinet list .xlsx (else [cabinet_list] file "
                                       "in config.toml)")
    args = parser.parse_args()

    try:
        config = config_module.load("config.toml")
    except config_module.ConfigError as error:
        print(f"config error: {error}")
        return 1
    if args.file:
        if not args.file.lower().endswith(".xlsx"):
            print("--file must be the .xlsx workbook")
            return 1
        cabinet = {"file": args.file, "check_interval_s": 5.0}
    else:
        cabinet = config.cabinet_list
    if not cabinet:
        print("give the workbook with --file \"<path>\", or set [cabinet_list] in config.toml")
        return 1
    if not config.pdus:
        print("config.toml has no [pdus] section")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    import main as main_module      # the program's Monitor, from the program folder

    with tempfile.TemporaryDirectory(prefix="dcmonitor-preview-") as folder:
        preview = dataclasses.replace(config, database=Path(folder) / "preview.db",
                                      cabinet_list=cabinet)
        monitor = main_module.Monitor(preview)
        # Only the PDUs of the racks asked for, and only the PDU job.
        if args.racks:
            monitor._direct_entries = [entry for entry in monitor._direct_entries
                                       if entry["rack"] in args.racks]
        monitor.jobs = ([job for job in monitor.jobs if job.device == "PDUs"]
                        if monitor._direct_entries else [])
        racks = ", ".join(map(str, args.racks)) or "all"
        print(f"preview: racks {racks}; {len(monitor._direct_entries)} PDUs read")

        # No sounds from a preview.
        for name in ("play_alarm_sound", "play_notice_sound"):
            setattr(panel_module, name, lambda: None)

        monitor.start()
        panel = panel_module.Panel(monitor)
        panel.title(f"dcmonitor PREVIEW: racks {racks} (PDUs only, no AC, UPS or "
                    "generator)")

        def open_first() -> None:
            if panel.floor_map is None or not args.racks:
                return
            panel.tabs.select(panel.tab_pages["Floor map"])
            panel.refresh_floor_map()
            panel.floor_map.pin(("rack", args.racks[0]))
            if panel.floor_map.device_counts.get(args.racks[0]):
                panel._show_devices(args.racks[0])

        # Once the first PDU round and the workbook are in.
        panel.after(4000, open_first)
        try:
            panel.mainloop()
        finally:
            monitor.stop()
            time.sleep(0.3)
            monitor.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

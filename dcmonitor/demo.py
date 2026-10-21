"""Demo: the real dcmonitor window with simulated devices.

    python -m dcmonitor.demo           (from the program folder)

Runs the real program (jobs, alarms, history, the window) against made-up
AC units, UPSes, a generator and rack PDUs, with a sample floor map
(demo/site_map.toml), PDU list (demo/pdus.toml) and cabinet list (an .xlsx
written into a temporary folder). Nothing is read from the network: the
collectors are replaced by simulators, and every network connection is
refused, so the demo can never reach a real device. The database is a
temporary file, deleted when the window closes; no sounds are played.

What the demo shows on purpose: rack 7 runs warm (31 C, red on the floor
map) and one of its breakers is near its limit (an amber warning), AC-4 is
in standby, UPS 2 runs a higher load. Values drift slowly while it runs.
"""

import argparse
import dataclasses
import io
import logging
import math
import random
import socket
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

FOLDER = Path(__file__).resolve().parent.parent
DEMO = FOLDER / "demo"
sys.path.insert(0, str(FOLDER))

from dcmonitor import config as config_module                        # noqa: E402
from dcmonitor import panel as panel_module                          # noqa: E402
from dcmonitor.collectors import generator, new_ac, old_ac, ups      # noqa: E402
from dcmonitor.collectors.raritan_px2 import (BreakerReading,        # noqa: E402
                                              OutletReading, PduReading)

START = time.time()


def _wave(seed: str, period_s: float = 600.0, amount: float = 1.0) -> float:
    """A slow, smooth drift per device, so the values move while it runs."""
    phase = (sum(map(ord, seed)) % 97) / 97 * 2 * math.pi
    return amount * math.sin((time.time() - START) / period_s * 2 * math.pi + phase)


def _jitter(amount: float) -> float:
    return random.uniform(-amount, amount)


# ------------------------------------------------------------- no network

def _no_network(*_args, **_kwargs):
    raise OSError("dcmonitor demo: network access is switched off")


def block_network() -> None:
    """Refuse every network connection and name lookup in this process."""
    socket.create_connection = _no_network
    socket.getaddrinfo = _no_network
    socket.socket.connect = _no_network
    socket.socket.connect_ex = _no_network
    socket.socket.sendto = _no_network


# ------------------------------------------------------------ AC units

AC_NAMES = {"demo-ac-1": "AC-1", "demo-ac-2": "AC-2", "demo-ac-3": "AC-3",
            "demo-ac-4": "AC-4"}
STANDBY = {"AC-4"}


def new_ac_values(ip, timeout=15.0, variables=None, max_query_chars=480):
    name = AC_NAMES.get(ip, ip)
    if name in STANDBY:
        values = {"return_temp_c": round(25.5 + _wave(name, 900, 0.4), 1),
                  "supply_temp_c": round(24.8 + _jitter(0.2), 1),
                  "supply_setpoint_c": 18.0,
                  "return_humidity_pct": round(44 + _jitter(1), 1)}
        return values, {"unit_status": "Standby"}
    values = {"return_temp_c": round(26.2 + _wave(name, 600, 0.8) + _jitter(0.1), 1),
              "supply_temp_c": round(18.4 + _wave(name, 300, 0.3), 1),
              "supply_setpoint_c": 18.0,
              "return_humidity_pct": round(42 + _wave(name, 1200, 3), 1)}
    return values, {"unit_status": "Unit On"}


def _utc(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ac_events(ip, timeout=15.0, utc_offset_hours=0.0):
    name = AC_NAMES.get(ip, ip)
    events = [new_ac.UnitEvent(_utc(60 * 26), 155, "Unit On", "Start", "", ""),
              new_ac.UnitEvent(_utc(60 * 5), 212, "Unit Standby Mode", "Stop", "", "")]
    if name in STANDBY:
        events.append(new_ac.UnitEvent(_utc(45), 212, "Unit Standby Mode", "Start", "", ""))
    return events


def old_ac_values(ip, timeout=35.0, items=None):
    name = AC_NAMES.get(ip, ip)
    values = {"return_temp_c": round(23.6 + _wave(name, 700, 0.7) + _jitter(0.1), 1),
              "return_setpoint_c": 24.0,
              "return_humidity_pct": round(47 + _wave(name, 1000, 2), 1)}
    states = {"unit_status": "Unit On", "fan": "On", "cooling": "On", "freecooling": "Off",
              "hot_water": "Off", "heater": "Off", "humidifier": "Off",
              "dehumidification": "Off", "maintenance": "Off"}
    return values, states, []


def old_ac_last_modified(ip, timeout=35.0):
    return "Mon, 01 Jan 2024 00:00:00 GMT"


def old_ac_events(ip, timeout=35.0, utc_offset_hours=0.0):
    return [old_ac.UnitEvent(_utc(60 * 30), "Unit On", "Message", "1", "1", ""),
            old_ac.UnitEvent(_utc(60 * 3), "Filter check due", "Message", "2", "2", "")]


# ------------------------------------------------------------------ UPS

UPS_LOAD = {"demo-ups-1": 31.0, "demo-ups-2": 58.0}


def ups_read(ip, community="public", timeout=2.0, retries=2):
    base = UPS_LOAD.get(ip, 30.0)
    metrics = {"battery_charge_pct": 100, "battery_temp_c": 24,
               "battery_minutes_remaining": int(95 - base),
               "output_frequency_hz": 50.0}
    for phase in (1, 2, 3):
        load = round(base + _wave(f"{ip}{phase}", 400, 3) + phase)
        metrics[f"output_load_pct_l{phase}"] = load
        metrics[f"output_voltage_v_l{phase}"] = 230
        metrics[f"output_current_a_l{phase}"] = round(load * 1.6, 1)
        metrics[f"input_voltage_v_l{phase}"] = 226 + phase % 2
    return {"reported_name": ip, "metrics": metrics, "output_source_code": 3,
            "output_source": ups.OUTPUT_SOURCE.get(3, "normal"), "on_battery": False,
            "on_bypass": False, "alarms_present": 0}


def ups_details(ip, community="public", timeout=2.0, retries=2):
    states = {"agent": "Demo UPS card", "manufacturer": "Demo", "model": "Demo UPS 120 kVA",
              "ups_software": "1.0", "agent_software": "1.0",
              "ups_name": "UPS 1" if ip.endswith("1") else "UPS 2",
              "agent_uptime_s": int(time.time() - START) + 86400 * 40,
              "battery_status": "normal", "shutdown_countdown": "none"}
    metrics = {"seconds_on_battery": 0, "battery_voltage_v": 545.0, "battery_current_a": 0.0,
               "input_line_bads": 0, "bypass_frequency_hz": 50.0}
    for phase in (1, 2, 3):
        metrics[f"input_frequency_hz_l{phase}"] = 50.0
        metrics[f"input_current_a_l{phase}"] = round(UPS_LOAD.get(ip, 30) * 1.5, 1)
        metrics[f"bypass_voltage_v_l{phase}"] = 227
        metrics[f"bypass_current_a_l{phase}"] = 0.0
    return {"metrics": metrics, "states": states}


def ups_alarms(ip, community="public", timeout=2.0, retries=2):
    return []


# ------------------------------------------------------------ generator

def generator_read(ip, unit=1, timeout=5.0):
    return {"fuel_level_pct": 92.0, "battery_v": round(13.5 + _jitter(0.1), 1)}


# ----------------------------------------------------------------- PDUs

# Per rack: average amps per used outlet, and the rack's temperature.
RACK_LOAD = {1: 0.30, 2: 0.45, 3: 0.20, 4: 0.55, 5: 0.35, 6: 0.25,
             7: 1.05, 8: 0.40, 9: 0.15, 10: 0.50, 11: 0.30, 12: 0.0}
RACK_TEMP = {1: 21.5, 2: 23.0, 3: 22.0, 4: 24.5, 5: 22.5, 6: 21.0,
             7: 31.0, 8: 25.5, 9: 20.5, 10: 27.5, 11: 23.5, 12: 19.5}


class DemoPdu:
    """Stands in for every PDU family: two 16 A breakers, 20 metered outlets
    (1-10 on C1, 11-20 on C2) and one temperature probe."""

    BY_NAME = False

    @staticmethod
    def describe(ip, community, timeout=3.0, **_kwargs):
        return ip

    @staticmethod
    def read(ip, community, layout, timeout=3.0, **_kwargs):
        index = int(ip.rsplit("-", 1)[1])                # demo-pdu-NN = pdus.toml id
        rack, side = (index + 1) // 2, 1 if index % 2 else 2
        per_outlet = RACK_LOAD.get(rack, 0.3)
        outlets = []
        for number in range(1, 21):
            used = number in DEVICE_OUTLETS.get(rack, ()) or (rack == 7 and number <= 10)
            amps = 0.0
            if used and per_outlet:
                amps = max(0.0, per_outlet * (1 + 0.15 * _wave(f"{ip}{number}", 300))
                           + _jitter(0.02))
            outlets.append(OutletReading(number, round(amps, 2), "C1" if number <= 10 else "C2"))
        c1 = round(sum(o.amps for o in outlets if o.breaker == "C1"), 2)
        c2 = round(sum(o.amps for o in outlets if o.breaker == "C2"), 2)
        if rack == 7 and side == 1:
            c1 = round(13.2 + _wave(ip, 200, 0.4), 2)  # near the 16 A limit: amber
        total = c1 + c2
        temp = round(RACK_TEMP.get(rack, 22.0) + _wave(f"t{ip}", 900, 0.3), 1)
        return PduReading(
            name=f"DEMO{rack:02d}-PDU{side:02d}", amps=round(total, 2), volts=230.0,
            watts=round(total * 230 * 0.95), va=round(total * 230), power_factor=0.95,
            energy_wh=None, hz=50.0, inlet_rated=32.0, lines=(),
            breakers=(BreakerReading("C1", c1, 16.0, 1, "closed"),
                      BreakerReading("C2", c2, 16.0, 1, "closed")),
            temperatures=(("DEMO0001", "Temperature 1", temp),) if side == 1 else (),
            reading_utc=None, outlets=tuple(outlets))


# -------------------------------------------------------- cabinet list

# Per rack: (top U, height in U, name, PDU cell as written in the Excel).
DEVICES = {
    rack: [(42, 1, "Patch panel", ""), (41, 1, "Cable management", ""),
           (40, 1, "Top of rack switch", "PDU1/1 PDU2/1"),
           (38, 2, "Firewall", "PDU1/2 PDU2/2"),
           (35, 2, "Web server 01", "PDU1/3 PDU2/3"),
           (33, 2, "Web server 02", "PDU1/4 PDU2/4"),
           (31, 1, "Cable management", ""),
           (28, 4, "Database server", "PDU1/5-6 PDU2/5-6"),
           (20, 6, "Storage array", "PDU1/7-8-9 PDU2/7-8-9"),
           (12, 10, "Blade chassis", "PDU1/11-12-13 PDU2/11-12-13"),
           (2, 1, "KVM switch", "PDU1/14")][: 4 + rack % 8]
    for rack in range(1, 13) if rack != 12
}
DEVICES[12] = []                                # an empty rack
DEVICE_OUTLETS = {}
for _rack, _rows in DEVICES.items():
    for _top, _height, _name, _cell in _rows:
        for _group in _cell.split():
            _outlets = _group.split("/")[1]
            DEVICE_OUTLETS.setdefault(_rack, set()).update(int(o) for o in _outlets.split("-"))


def write_cabinet_list(path: Path) -> None:
    """A small .xlsx in the layout dcmonitor/cabinet_list.py reads: one sheet
    per rack, U/Cabinet | devices (merged over their U's) | PDU."""
    strings = []

    def string(text):
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    def column(n):
        name = ""
        while n:
            n, rest = divmod(n - 1, 26)
            name = chr(65 + rest) + name
        return name

    def sheet_xml(rack):
        cells = {(1, 1): "U/Cabinet", (1, 2): f"{rack}  DEMO CUSTOMER {rack}", (1, 3): "PDU"}
        merges = []
        for u in range(42, 0, -1):
            cells[(44 - u, 1)] = u
        for top, height, name, cell in DEVICES[rack]:
            row = 44 - top
            cells[(row, 2)] = name
            if cell:
                cells[(row, 3)] = cell
            if height > 1:
                merges.append(f"B{row}:B{row + height - 1}")
                if cell:
                    merges.append(f"C{row}:C{row + height - 1}")
        rows = {}
        for (r, c), value in cells.items():
            rows.setdefault(r, []).append((c, value))
        out = ['<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.'
               'openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        for r in sorted(rows):
            out.append(f'<row r="{r}">')
            for c, value in sorted(rows[r]):
                ref = f"{column(c)}{r}"
                if isinstance(value, int):
                    out.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    out.append(f'<c r="{ref}" t="s"><v>{string(value)}</v></c>')
            out.append("</row>")
        out.append("</sheetData>")
        if merges:
            out.append(f'<mergeCells count="{len(merges)}">'
                       + "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells>")
        out.append("</worksheet>")
        return "".join(out)

    racks = sorted(DEVICES)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        workbook = ['<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships"><sheets>']
        rels = ['<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships">']
        sheets = [("Overview", None)] + [(str(rack), rack) for rack in racks]
        for index, (name, rack) in enumerate(sheets, start=1):
            workbook.append(f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>')
            rels.append(f'<Relationship Id="rId{index}" Type="worksheet" '
                        f'Target="worksheets/sheet{index}.xml"/>')
            xml = (sheet_xml(rack) if rack is not None else
                   '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><sheetData/></worksheet>')
            book.writestr(f"xl/worksheets/sheet{index}.xml", xml)
        book.writestr("xl/workbook.xml", "".join(workbook) + "</sheets></workbook>")
        book.writestr("xl/_rels/workbook.xml.rels", "".join(rels) + "</Relationships>")
        book.writestr("xl/sharedStrings.xml",
                      '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                      'spreadsheetml/2006/main">'
                      + "".join(f"<si><t>{escape(t)}</t></si>" for t in strings) + "</sst>")
    path.write_bytes(buffer.getvalue())


# ------------------------------------------------------------------ run

def install_simulators(main_module) -> None:
    new_ac.read_values, new_ac.read_events = new_ac_values, new_ac_events
    old_ac.read_values, old_ac.last_modified = old_ac_values, old_ac_last_modified
    old_ac.read_events = old_ac_events
    ups.read, ups.read_details, ups.read_alarms = ups_read, ups_details, ups_alarms
    generator.read = generator_read
    for family in list(main_module.DIRECT_READERS):
        main_module.DIRECT_READERS[family] = DemoPdu


def main() -> int:
    parser = argparse.ArgumentParser(description="dcmonitor demo with simulated devices")
    parser.add_argument("--sound", action="store_true", help="play the alarm sounds")
    args = parser.parse_args()

    block_network()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    try:
        config = config_module.load(DEMO / "config.toml")
    except config_module.ConfigError as error:
        print(f"demo config error: {error}")
        return 1
    import main as main_module          # the program's Monitor
    install_simulators(main_module)
    if not args.sound:
        for name in ("play_alarm_sound", "play_notice_sound"):
            setattr(panel_module, name, lambda: None)

    with tempfile.TemporaryDirectory(prefix="dcmonitor-demo-") as folder:
        workbook = Path(folder) / "cabinet_list.xlsx"
        write_cabinet_list(workbook)
        demo = dataclasses.replace(
            config, database=Path(folder) / "demo.db", log_folder=Path(folder) / "logs",
            cabinet_list={"file": str(workbook), "check_interval_s": 5.0})
        monitor = main_module.Monitor(demo)
        monitor.start()
        window = panel_module.Panel(monitor)
        window.title("dcmonitor DEMO (simulated devices)")
        try:
            window.mainloop()
        finally:
            monitor.stop()
            time.sleep(0.3)
            monitor.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

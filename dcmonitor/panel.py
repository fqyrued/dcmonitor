"""The dcmonitor window.

tkinter rule that shapes this file: only the main thread may touch the window.
The collectors run in their own threads and put results on a queue; the window
drains that queue on a timer, so nothing blocks the screen and no widget is
touched from a worker thread.

Layout:
    alarm bar        always visible, with Silence, Poll all now and Refresh
    AC UNITS         ten cards in two rows of five, AC-1..AC-5, AC-6..AC-10
    UPS SYSTEMS      five cards, in their own framed box
    GENERATORS       three cards, in a second framed box beside the UPS box
    UPS tab          every value the UPSes give, one column per UPS

Cards are drawn on a Canvas rather than built from labels, so they can have
rounded corners, a coloured status strip and a charge meter. Each card redraws
itself when its data or its size changes.
"""

import logging
import platform
from pathlib import Path
import queue
import re
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk

from dcmonitor.floor_map import FloorMap, MapError, load_layout
from dcmonitor.alarms import NOTICE_KINDS
from dcmonitor.storage import utc_now

QUEUE_INTERVAL_MS = 200      # how often the window drains the update queue
TICK_INTERVAL_MS = 1000      # how often ages and colours are refreshed
SOUND_INTERVAL_MS = 3000     # gap between alarm sounds while an alarm stands
STALE_AFTER_S = 180          # values older than this are shown as stale
LOG_REFRESH_MS = 5000        # how often the open log view reloads
JOBS_REFRESH_MS = 2000       # how often the open jobs view reloads
UPS_TAB_REFRESH_MS = 2000    # how often the open UPS tab redraws
ALARMS_REFRESH_MS = 5000     # how often the open alarms view reloads
GRAPHS_REFRESH_MS = 60000    # hourly data, so once a minute is plenty
MAP_REFRESH_MS = 5000        # how often the open floor map redraws

GRAPH_BAND = "#2c4a66"       # min to max shading
GRAPH_LINE = "#4da3ff"       # average
GRAPH_REFERENCE = "#e0a030"  # setpoint
GRAPH_EXTRA = ("#4da3ff", "#2e9e5b", "#e0a030")   # UPS phase loads

ALARM_KIND_LABELS = {
    "unit_event": "Unit alarm",
    "temperature": "Temperature",
    "ups": "UPS",
    "reachability": "Unreachable",
    "rack_temperature": "Rack temperature",
    "breaker": "Breaker load",
    "breaker_trip": "Breaker tripped",
    "ups_notice": "UPS notice",
    "pdu_health": "PDU health",
    "generator": "Generator",
    "breaker_notice": "Breaker warning",
    "circuit_drop": "Circuit dropped",
    "circuit_change": "Circuit change",
}
PROGRAM_LOG_LINES = 800      # lines of the program log kept on screen

# Ranges offered in the log views, as label -> days.
RANGES = {
    "Last 24 hours": 1,
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last year": 365,
}

BACKGROUND = "#15171c"
CARD = "#22262e"
CARD_ALARM = "#3a1f22"
CARD_NOTICE = "#302a1c"
CARD_STALE = "#202227"
TEXT = "#f2f4f8"
TEXT_DIM = "#9aa3b2"
TEXT_DETAIL = "#c9d0db"      # detail lines: brighter than TEXT_DIM, easier to read
GROUP_BG = "#1c1f26"         # framed box behind the UPS and generator cards
GROUP_BORDER = "#3a404a"

# Text sizes for the smaller details, kept together so they are easy to tune.
DETAIL_SIZE = 14             # card detail lines
DETAIL_GAP = 22              # distance between card detail lines
TABLE_ROW_GAP = 27           # distance between a card's phase rows
TABLE_AFTER_GAP = 6          # extra space between a table and the next line
FOOTER_SIZE = 12             # card footer: "updated 3s ago" or the alarm text
STATUS_SIZE = 13             # card status, e.g. "Standby"
TABLE_SIZE = 13              # rows in the Logs, Readings, Alarms and Jobs tables
CONTROL_SIZE = 12            # labels next to drop-downs and search boxes
ACCENT_OK = "#2e9e5b"
ACCENT_ALARM = "#e5484d"
ACCENT_NOTICE = "#d69e2e"
ACCENT_STALE = "#5f6672"
ACCENT_IDLE = "#8a93a3"      # an AC unit in standby or switched off: grey, lighter than stale
# Devices window: the rack drawn like its sheet in the cabinet list.
SHEET_HEADER = "#1f6f73"     # header and U column, the sheet's teal
SHEET_U = "#1a5357"
SHEET_DEVICE = "#8f2a2f"     # a device block, the sheet's red
SHEET_OWN = "#5d6470"        # the site operator's own equipment: grey
# Device names (letters only, upper case) drawn grey as the operator's own
# equipment in the Devices window, e.g. {"RAFOPERATOR"} for "RAF -OPERATOR-".
OWN_EQUIPMENT = set()
SHEET_CABLES = "#3a3f48"     # cable management: darker grey, dimmer text
GROUP_GAP = 8                # px between a device's PDU1 and PDU2 outlets
SHEET_PDU = "#24486b"        # the outlets beside a device, the sheet's blue
SHEET_ROW = "#1c1f26"
SHEET_ROW_ALT = "#20242c"
SHEET_GRID = "#3a414c"
BAR_OK = "#1b5e20"
BAR_ALARM = "#b3261e"
BAR_NOTICE = "#8a6a1c"

# dcmonitor's own sounds (made by sounds/make_sounds.py): a two-tone siren
# for alarms and a rising triple beep for notices. If a file is missing the
# Windows sounds are used instead, so the panel is never silent.
SOUND_FOLDER = Path(__file__).resolve().parent.parent / "sounds"
ALARM_SOUND_FILE = SOUND_FOLDER / "alarm.wav"
NOTICE_SOUND_FILE = SOUND_FOLDER / "notice.wav"
WINDOWS_ALARM_SOUND = r"C:\Windows\Media\Alarm01.wav"
MAC_ALARM_SOUND = "/System/Library/Sounds/Sosumi.aiff"

_mac_player = None       # the running afplay, so Silence can stop it
_sound_errors = set()    # sound failures already written to the program log


def _sound_failed(which: str, error: Exception) -> None:
    """A sound that cannot play must never stop the panel, but it must not
    fail silently either: the first failure of each kind goes to the log."""
    text = f"{type(error).__name__}: {error}"
    if (which, text) not in _sound_errors:
        _sound_errors.add((which, text))
        logging.getLogger("dcmonitor").error("%s sound did not play: %s", which, text)


def _play_file(path: Path, fallback_windows, fallback_mac: str) -> None:
    """Play a WAV without blocking. fallback_windows is a file path or a
    MessageBeep type, used when the file is missing."""
    global _mac_player
    system = platform.system()
    if system == "Windows":
        import winsound
        if path.is_file():
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif isinstance(fallback_windows, str):
            winsound.PlaySound(fallback_windows, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.MessageBeep(fallback_windows)
    elif system == "Darwin":
        _mac_player = subprocess.Popen(["afplay", str(path) if path.is_file() else fallback_mac],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print("\a", end="", flush=True)


def visible_screen(widget) -> tuple:
    """(top, height) for a window's outer frame to stay in the visible part
    of the screen. On Windows the work area (the screen minus the taskbar)
    less the title bar; elsewhere the screen less room for a menu bar and
    dock."""
    title_bar = 40
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes
            area = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
                return area.top + 4, area.bottom - area.top - title_bar - 8
        except (AttributeError, OSError):
            pass
        return 4, widget.winfo_screenheight() - 60 - title_bar
    return 30, widget.winfo_screenheight() - 140


def play_notice_sound() -> None:
    """The notice sound, played once per new notice, never repeated."""
    try:
        import winsound
        beep = winsound.MB_ICONASTERISK
    except ImportError:
        beep = None
    try:
        _play_file(NOTICE_SOUND_FILE, beep, "/System/Library/Sounds/Tink.aiff")
    except Exception as error:
        _sound_failed("notice", error)


def stop_alarm_sound() -> None:
    """Stop a sound that is still playing, at once.

    On Windows, PlaySound with no sound stops any sound playing
    asynchronously. On macOS the afplay process is ended.
    """
    global _mac_player
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.PlaySound(None, 0)
        elif _mac_player is not None and _mac_player.poll() is None:
            _mac_player.terminate()
    except Exception:
        pass
    _mac_player = None


def play_alarm_sound() -> None:
    """The alarm siren, without blocking the window. The panel repeats it
    every SOUND_INTERVAL_MS until Silence."""
    try:
        _play_file(ALARM_SOUND_FILE, WINDOWS_ALARM_SOUND, MAC_ALARM_SOUND)
    except Exception as error:
        _sound_failed("alarm", error)


# ---------------------------------------------------------------- formatting
# Pure functions, so the card contents can be tested without a window.

def age_text(age) -> str:
    if age is None:
        return "no data yet"
    if age > STALE_AFTER_S:
        return f"stale, {age / 60:.0f} min old"
    if age < 90:
        return f"updated {age:.0f}s ago"
    return f"updated {age / 60:.0f} min ago"


def local_text(utc_text: str) -> str:
    """Turn a stored UTC timestamp into the machine's local time for display."""
    try:
        moment = datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return utc_text or ""
    return (moment.replace(tzinfo=timezone.utc)
            .astimezone()
            .strftime("%d.%m.%Y %H:%M:%S"))


TIME_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y %H", "%d.%m.%Y",
                "%Y-%m-%d %H:%M", "%Y-%m-%d")


def hour_key_from_local(text: str, end_of_day: bool = False):
    """Turn typed local time into a UTC hour key, or None if it is unusable.

    Accepts "18.09.2026 14:30", "18.09.2026 14" or "18.09.2026". A date with no
    time means the start of that day, or its last hour when end_of_day is set.
    """
    text = (text or "").strip()
    if not text:
        return None
    for pattern in TIME_FORMATS:
        try:
            moment = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern in ("%d.%m.%Y", "%Y-%m-%d") and end_of_day:
            moment = moment.replace(hour=23)
        return (moment.astimezone()
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H"))
    return None


def is_notice(alarm: dict) -> bool:
    return alarm.get("kind") in NOTICE_KINDS


def footer_text(alarms: list, age) -> str:
    """A real alarm first; a notice only if there is no real alarm."""
    real = [alarm for alarm in alarms if not is_notice(alarm)]
    if real:
        return real[0]["description"]
    if alarms:
        extra = f"  (+{len(alarms) - 1})" if len(alarms) > 1 else ""
        return alarms[0]["description"] + extra
    return age_text(age)


def state_of(alarms: list, age) -> str:
    if any(not is_notice(alarm) for alarm in alarms):
        return "alarm"
    if alarms:
        return "notice"
    if age is None or age > STALE_AFTER_S:
        return "stale"
    return "ok"


def ac_content(unit, values: dict, states: dict, alarms: list, age) -> dict:
    """What an AC card shows. unit needs .name and .is_new."""
    # Both generations show return temperature. The new units add their
    # supply side as a detail, since their setpoint is a supply setpoint.
    temperature = values.get("return_temp_c")
    humidity = values.get("return_humidity_pct")
    if unit.is_new:
        supply = values.get("supply_temp_c")
        setpoint = values.get("supply_setpoint_c")
        detail = ("supply " + (f"{supply:.1f}" if supply is not None else "--")
                  + " \u00b0C \u00b7 set " + (f"{setpoint:.1f}" if setpoint is not None else "--")
                  + " \u00b0C")
    else:
        setpoint = values.get("return_setpoint_c")
        detail = (f"return setpoint  {setpoint:.1f} \u00b0C" if setpoint is not None
                  else "return setpoint  --")

    return {
        "title": unit.name,
        "state": state_of(alarms, age),
        "headline": f"{temperature:.1f}" if temperature is not None else "--",
        "unit_text": "\u00b0C",
        "status": states.get("unit_status", ""),
        "lines": [
            detail,
            f"humidity  {humidity:.1f} %" if humidity is not None else "humidity  --",
        ],
        "footer": footer_text(alarms, age),
        "meter": None,
    }


def generator_content(unit, values: dict, alarms: list, age) -> dict:
    """What a generator card shows: fuel as the headline, with a meter.

    Only fuel and battery are confirmed on site. Engine state and mode are
    not shown until a test run confirms their registers.
    """
    fuel = values.get("fuel_level_pct")
    battery = values.get("battery_v")
    return {
        "title": unit.name,
        "state": state_of(alarms, age),
        "headline": f"{fuel:g}" if fuel is not None else "--",
        "unit_text": "% fuel",
        "status": "",
        "lines": [
            f"battery  {battery:.1f} V" if battery is not None else "battery  --",
            "engine: unconfirmed",
        ],
        "footer": footer_text(alarms, age),
        "meter": None if fuel is None else max(0.0, min(1.0, fuel / 100)),
    }


def _phase_rows(values: dict) -> list:
    """One row per output phase: its name, load % and current."""
    rows = []
    for phase in (1, 2, 3):
        load = values.get(f"output_load_pct_l{phase}")
        current = values.get(f"output_current_a_l{phase}")
        rows.append({
            "phase": f"L{phase}",
            "cells": [f"{load:g} %" if load is not None else "--",
                      f"{current:.1f} A" if current is not None else "-- A"],
        })
    return rows


def ups_content(unit, values: dict, alarms: list, age) -> dict:
    """What a UPS card shows.

    Order: highest phase load as the headline, then UPS temperature, battery
    charge, autonomy (hh:mm, as on the NetMan) and one row per output phase
    (load % and current). The bar shows battery charge. Voltages and frequency are read and stored but not shown
    here: the card has no room for them.
    """
    charge = values.get("battery_charge_pct")
    temperature = values.get("battery_temp_c")
    minutes = values.get("battery_minutes_remaining")
    loads = [values.get(f"output_load_pct_l{phase}") for phase in (1, 2, 3)]
    known = [value for value in loads if value is not None]
    highest = max(known) if known else None

    return {
        "title": unit.name,
        "state": state_of(alarms, age),
        "headline": f"{highest:g}" if highest is not None else "--",
        "unit_text": "% load",
        "status": "",
        "lines": [
            f"UPS temp  {temperature:g} \u00b0C" if temperature is not None else "UPS temp  --",
            f"charge  {charge:g} %" if charge is not None else "charge  --",
            f"autonomy  {hh_mm(minutes)}",
            *_phase_rows(values),
        ],
        "footer": footer_text(alarms, age),
        "meter": None if charge is None else max(0.0, min(1.0, charge / 100)),
    }


def hh_mm(minutes) -> str:
    """Minutes as hh:mm, the way the NetMan shows autonomy (81 -> 01:21)."""
    if minutes is None:
        return "--"
    minutes = int(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------- UPS tab panel
# One panel per UPS, most important first: status banner, power path, three
# big numbers, a phase table like the NetMan DATA page, details, identity.

def _joined(values: dict, metric: str, pattern: str) -> str:
    parts = [values.get(f"{metric}_l{phase}") for phase in (1, 2, 3)]
    if all(part is None for part in parts):
        return "--"
    return " / ".join(format(part, pattern) if part is not None else "--" for part in parts)


def _text(value, pattern: str, unit: str = "") -> str:
    return "--" if value is None else format(value, pattern) + unit


def ups_panel_content(unit, values: dict, states: dict, alarms: list, age,
                      thresholds: dict) -> dict:
    """What one UPS panel on the UPS tab shows. Pure, so it can be tested."""
    source = states.get("output_source")
    seconds = values.get("seconds_on_battery")
    state = state_of(alarms, age)

    # Status banner: where the load is fed from.
    if age is None:
        banner, path = ("No data yet", "stale"), None
    elif age > STALE_AFTER_S:
        banner, path = (f"Stale, {age / 60:.0f} min old", "stale"), None
    elif source == "normal":
        banner, path = ("On inverter", "ok"), "inverter"     # NetMan: LOAD ON INVERTER
    elif source == "battery":
        on_for = f" {int(seconds) // 60} min {int(seconds) % 60} s" if seconds else ""
        banner, path = ("On battery" + on_for, "alarm"), "battery"
    elif source == "bypass":
        banner, path = ("On bypass", "alarm"), "bypass"
    elif source == "none":
        banner, path = ("Output off", "alarm"), None
    elif source is None:
        banner, path = ("No status yet", "stale"), None
    else:
        banner, path = (f"On {source}", "notice"), "inverter"

    # First alarm the banner does not already say, e.g. low input or UPS temp.
    alert = None
    others = [alarm for alarm in alarms if alarm.get("key") not in ("on_battery", "on_bypass")]
    real = [alarm for alarm in others if not is_notice(alarm)]
    if real:
        alert = (real[0]["description"], ACCENT_ALARM)
    elif others:
        alert = (others[0]["description"], ACCENT_NOTICE)

    low_limit = thresholds.get("ups_input_voltage_alarm_below_v")
    inputs = [values.get(f"input_voltage_v_l{phase}") for phase in (1, 2, 3)]
    known_inputs = [value for value in inputs if value is not None]
    if not known_inputs:
        mains, mains_state = "--", None
    else:
        lowest = min(known_inputs)
        mains = f"{lowest:g} V"
        mains_state = "bad" if low_limit is not None and lowest < low_limit else "ok"

    loads = [values.get(f"output_load_pct_l{phase}") for phase in (1, 2, 3)]
    known_loads = [value for value in loads if value is not None]
    highest = max(known_loads) if known_loads else None

    charge = values.get("battery_charge_pct")
    charge_floor = thresholds.get("ups_battery_charge_alarm_below_pct")
    charge_colour = (ACCENT_ALARM if charge is not None and charge_floor is not None
                     and charge < charge_floor else None)
    big = [
        ("Highest load", _text(highest, "g", " %"), None),
        ("Autonomy", hh_mm(values.get("battery_minutes_remaining")),
         ACCENT_ALARM if path == "battery" else None),
        ("Charge", _text(charge, "g", " %"), charge_colour),
    ]

    rows = []
    for phase, voltage in zip((1, 2, 3), inputs):
        low = voltage is not None and low_limit is not None and voltage < low_limit
        rows.append([
            (f"L{phase}", None),
            (_text(voltage, "g"), ACCENT_ALARM if low else None),
            (_text(values.get(f"input_current_a_l{phase}"), ".1f"), None),
            (_text(values.get(f"output_voltage_v_l{phase}"), "g"), None),
            (_text(values.get(f"output_load_pct_l{phase}"), "g"), None),
            (_text(values.get(f"output_current_a_l{phase}"), ".1f"), None),
        ])

    in_freq = [values.get(f"input_frequency_hz_l{phase}") for phase in (1, 2, 3)]
    in_freq = next((value for value in in_freq if value is not None), None)
    temperature = values.get("battery_temp_c")
    temp_limit = thresholds.get("ups_battery_temp_alarm_c")
    status = states.get("battery_status")
    alarm_count = states.get("alarms_present")
    countdown = states.get("shutdown_countdown")
    details = [
        ("Bypass", _joined(values, "bypass_voltage_v", "g") + " V", None),
        ("Frequency", f"in {_text(in_freq, '.1f')}  \u00b7  out "
         f"{_text(values.get('output_frequency_hz'), '.1f')}  \u00b7  byp "
         f"{_text(values.get('bypass_frequency_hz'), '.1f')} Hz", None),
        ("Battery", _text(values.get("battery_voltage_v"), ".1f", " V") + "  \u00b7  "
         + _text(values.get("battery_current_a"), ".1f", " A"), None),
        ("UPS temp", _text(temperature, "g", " \u00b0C"),
         ACCENT_ALARM if temperature is not None and temp_limit is not None
         and temperature >= temp_limit else None),
        ("Battery status", status or "--",
         ACCENT_ALARM if status not in (None, "normal") else None),
        ("Alarms reported by UPS", "--" if alarm_count is None else str(alarm_count),
         ACCENT_NOTICE if alarm_count else None),
        ("Shutdown countdown", countdown or "--",
         ACCENT_ALARM if countdown not in (None, "none") else None),
        ("Input out-of-tolerance count", _text(values.get("input_line_bads"), "d"), None),
    ]

    uptime = states.get("agent_uptime_s")
    card_version = (states.get("agent_software") or "").removeprefix("AppVer.").strip()
    identity = [states.get("model"), states.get("manufacturer"),
                f"UPS fw {states['ups_software']}" if states.get("ups_software") else None]
    agent = [states.get("agent"), f"card {card_version}" if card_version else None,
             f"up {int(uptime) // 86400} d" if uptime is not None else None,
             age_text(age).removeprefix("updated ").replace("ago", "ago").strip()
             if age is not None else None]
    footer = [" \u00b7 ".join(part for part in identity if part) or "identity not read yet",
              " \u00b7 ".join(part for part in agent if part)]
    if age is not None:
        footer[1] = footer[1].replace(age_text(age).removeprefix("updated "),
                                      "read " + age_text(age).removeprefix("updated "))

    return {
        "title": unit.name, "state": state, "banner": banner, "alert": alert,
        "flow": {"path": path, "mains": mains, "mains_state": mains_state,
                 "load": _text(highest, "g", " %")},
        "big": big,
        "table": {"header": ["", "In V", "In A", "Out V", "Out %", "Out A"], "rows": rows},
        "details": details, "footer": footer,
    }


# --------------------------------------------------------------------- cards

class Card(tk.Canvas):
    """A rounded tile with a coloured status strip down its left edge."""

    def __init__(self, parent, compact: bool = False, bg: str = BACKGROUND):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0)
        self.compact = compact
        self.content = {
            "title": "", "state": "stale", "headline": "--", "unit_text": "",
            "status": "", "lines": [], "footer": "", "meter": None,
        }
        self.bind("<Configure>", lambda event: self._draw())

    def show(self, content: dict) -> None:
        if content != self.content:
            self.content = content
            self._draw()

    def _rounded(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _draw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 20 or height < 20:
            return

        state = self.content["state"]
        accent = {"alarm": ACCENT_ALARM, "notice": ACCENT_NOTICE,
                  "stale": ACCENT_STALE}.get(state, ACCENT_OK)
        fill = {"alarm": CARD_ALARM, "notice": CARD_NOTICE,
                "stale": CARD_STALE}.get(state, CARD)
        # A unit in standby or switched off is not in alarm, but it is not
        # cooling either: strip, frame and dot go grey. An alarm or notice
        # still shows in its own colour.
        status = self.content.get("status", "")
        idle = state == "ok" and status in ("Standby", "Unit Off")
        if idle:
            accent = ACCENT_IDLE

        self._rounded(2, 2, width - 2, height - 2, 14, fill=fill, outline=accent,
                      width=2 if state == "alarm" else 1)
        # Status strip: the fastest thing to read from across the room.
        self._rounded(2, 2, 12, height - 2, 10, fill=accent, outline=accent)

        left = 26
        title_y = 22 if self.compact else 26
        title_size = 13 if self.compact else 15
        self.create_text(left, title_y, anchor="w", text=self.content["title"],
                         fill=TEXT, font=("Helvetica", title_size, "bold"))
        dot = accent
        self.create_oval(width - 26, title_y - 6, width - 14, title_y + 6,
                         fill=dot, outline=dot)
        if status:
            self.create_text(width - 34, title_y, anchor="e", text=status,
                             fill=TEXT_DETAIL, font=("Helvetica", STATUS_SIZE))

        headline_size = 26 if self.compact else 32
        headline_y = title_y + (28 if self.compact else 34)
        text_id = self.create_text(left, headline_y, anchor="w",
                                   text=self.content["headline"],
                                   fill=TEXT, font=("Helvetica", headline_size, "bold"))
        bounds = self.bbox(text_id)
        self.create_text(bounds[2] + 4, headline_y + 6, anchor="w",
                         text=self.content["unit_text"], fill=TEXT_DIM,
                         font=("Helvetica", 13))

        line_y = headline_y + (20 if self.compact else 26)
        in_table = False
        for line in self.content["lines"]:
            # A dict is one phase row.
            if isinstance(line, dict):
                line_y += TABLE_ROW_GAP
                self._phase_row(left, line_y, width - left - 16, line)
                in_table = True
                continue
            line_y += DETAIL_GAP + (TABLE_AFTER_GAP if in_table else 0)
            in_table = False
            self._fit(left, line_y, line, width - left - 12, DETAIL_SIZE, TEXT_DETAIL)

        meter = self.content["meter"]
        if meter is not None:
            line_y += 20
            bar_width = width - left - 20
            self.create_rectangle(left, line_y, left + bar_width, line_y + 7,
                                  fill="#2f343d", outline="")
            self.create_rectangle(left, line_y, left + bar_width * meter,
                                  line_y + 7, fill=accent, outline="")

        footer = self.content["footer"]
        if footer:
            self._fit(left, height - 15, footer, width - left - 12, FOOTER_SIZE, TEXT_DIM)

    def _phase_row(self, x, y, max_width, line: dict) -> None:
        """A phase row: the phase name on the left, then load % and current
        right-aligned in two columns, so the phases line up under each other."""
        size = DETAIL_SIZE if max_width >= 170 else max(9, int(DETAIL_SIZE * max_width / 170))
        self.create_text(x, y, anchor="w", text=line["phase"], fill=TEXT_DIM,
                         font=("Helvetica", size, "bold"))
        load_text, current_text = line["cells"]
        self.create_text(x + max_width * 0.52, y, anchor="e", text=load_text,
                         fill=TEXT_DETAIL, font=("Helvetica", size))
        self.create_text(x + max_width, y, anchor="e", text=current_text,
                         fill=TEXT_DETAIL, font=("Helvetica", size))

    def _fit(self, x, y, text, max_width, size, colour):
        """Left-aligned text that shrinks rather than running past the card."""
        while True:
            item = self.create_text(x, y, anchor="w", text=text, fill=colour,
                                    font=("Helvetica", size))
            x1, _y1, x2, _y2 = self.bbox(item)
            if x2 - x1 <= max_width or size <= 9:
                return item
            self.delete(item)
            size -= 1


def section_label(parent, text: str, bg: str = BACKGROUND) -> tk.Frame:
    """A heading with a rule, so AC and UPS blocks are told apart at a glance."""
    row = tk.Frame(parent, bg=bg)
    tk.Label(row, text=text, bg=bg, fg=TEXT_DIM,
             font=("Helvetica", 12, "bold")).pack(side="left", padx=(6, 10))
    tk.Frame(row, bg="#2c313a", height=1).pack(side="left", fill="x", expand=True,
                                               pady=(2, 0))
    return row



PILL_COLOURS = {"ok": BAR_OK, "alarm": BAR_ALARM, "notice": BAR_NOTICE, "stale": "#3a404a"}
FLOW_IDLE = "#3a404a"        # a power path that is not in use


class UpsPanel(Card):
    """One UPS on the UPS tab, drawn on a canvas like the Overview cards.
    Content comes from ups_panel_content()."""

    HEIGHT = 680

    def __init__(self, parent):
        super().__init__(parent)
        self.content = {"title": "", "state": "stale", "banner": ("", "stale"),
                        "alert": None, "flow": {}, "big": [], "table": {"rows": []},
                        "details": [], "footer": []}

    def _draw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 120 or height < 120:
            return
        content = self.content
        state = content["state"]
        accent = {"alarm": ACCENT_ALARM, "notice": ACCENT_NOTICE,
                  "stale": ACCENT_STALE}.get(state, ACCENT_OK)
        fill = {"alarm": CARD_ALARM, "notice": CARD_NOTICE,
                "stale": CARD_STALE}.get(state, CARD)
        self._rounded(2, 2, width - 2, height - 2, 14, fill=fill, outline=accent,
                      width=2 if state == "alarm" else 1)
        left, right = 18, width - 18
        inner = right - left

        # Title and status banner.
        self.create_text(left, 28, anchor="w", text=content["title"], fill=TEXT,
                         font=("Helvetica", 16, "bold"))
        text, kind = content["banner"]
        pill = self.create_text(right - 12, 28, anchor="e", text=text, fill=TEXT,
                                font=("Helvetica", 12, "bold"))
        x1, y1, x2, y2 = self.bbox(pill)
        box = self._rounded(x1 - 12, y1 - 5, x2 + 12, y2 + 5, 10,
                            fill=PILL_COLOURS.get(kind, FLOW_IDLE), outline="")
        self.tag_raise(pill, box)
        y = 56
        if content["alert"]:
            alert, colour = content["alert"]
            self._fit(left, y + 4, alert, inner, 12, colour)
            y += 22

        y = self._draw_flow(left, right, y + 8, content["flow"])

        # Three big numbers.
        big = content["big"]
        gap = 8
        tile = (inner - gap * (len(big) - 1)) / max(1, len(big))
        for index, (label, value, colour) in enumerate(big):
            x = left + index * (tile + gap)
            self.create_rectangle(x, y, x + tile, y + 62, fill=GROUP_BG, outline="")
            self.create_text(x + 10, y + 14, anchor="w", text=label, fill=TEXT_DIM,
                             font=("Helvetica", 11))
            self.create_text(x + 10, y + 40, anchor="w", text=value, fill=colour or TEXT,
                             font=("Helvetica", 21, "bold"))
        y += 62 + 18

        # Phase table.
        table = content["table"]
        columns = len(table.get("header", [])) or 6
        label_width = 34
        column = (inner - label_width) / max(1, columns - 1)
        for index, heading in enumerate(table.get("header", [])[1:], start=1):
            self.create_text(left + label_width + column * index, y, anchor="e",
                             text=heading, fill=TEXT_DIM, font=("Helvetica", 11))
        y += 12
        for row in table["rows"]:
            self.create_line(left, y, right, y, fill="#2c313a")
            y += 16
            for index, (cell, colour) in enumerate(row):
                if index == 0:
                    self.create_text(left, y, anchor="w", text=cell, fill=TEXT_DIM,
                                     font=("Helvetica", TABLE_SIZE, "bold"))
                else:
                    self.create_text(left + label_width + column * index, y, anchor="e",
                                     text=cell, fill=colour or TEXT_DETAIL,
                                     font=("Helvetica", TABLE_SIZE))
            y += 14
        self.create_line(left, y, right, y, fill="#2c313a")
        y += 22

        # Details.
        for label, value, colour in content["details"]:
            self.create_text(left, y, anchor="w", text=label, fill=TEXT_DIM,
                             font=("Helvetica", 12))
            self.create_text(right, y, anchor="e", text=value, fill=colour or TEXT_DETAIL,
                             font=("Helvetica", 12))
            y += 23

        # Identity, dim, at the bottom.
        footer = content["footer"]
        y = max(y + 6, height - 16 - 20 * len(footer))
        self.create_line(left, y - 12, right, y - 12, fill="#2c313a")
        for line in footer:
            self._fit(left, y, line, inner, 11, TEXT_DIM)
            y += 20

    def _draw_flow(self, left, right, top, flow) -> int:
        """Mains -> UPS -> Load, bypass above, battery below; the path in use
        is lit, like the NetMan SYSTEM OVERVIEW page. Returns the next y."""
        path = flow.get("path")
        lit = ACCENT_OK if path == "inverter" else ACCENT_ALARM
        box_w, box_h = 70, 40
        mid = top + 30 + box_h / 2
        centre = (left + right) / 2
        boxes = {"mains": left + box_w / 2, "ups": centre, "load": right - box_w / 2}

        def line(points, active):
            self.create_line(*points, fill=lit if active else FLOW_IDLE,
                             width=4 if active else 2, capstyle="round", joinstyle="round")

        # Bypass: over the top, from mains to load.
        by_y = top + 8
        line([boxes["mains"], mid - box_h / 2, boxes["mains"], by_y,
              boxes["load"], by_y, boxes["load"], mid - box_h / 2], path == "bypass")
        self.create_text(centre, by_y - 1, text=" bypass ", fill=TEXT_DIM,
                         font=("Helvetica", 10), anchor="s")
        # Through the UPS.
        line([boxes["mains"] + box_w / 2, mid, centre - box_w / 2, mid], path == "inverter")
        line([centre + box_w / 2, mid, boxes["load"] - box_w / 2, mid],
             path in ("inverter", "battery"))
        # Battery under the UPS.
        battery_top = mid + box_h / 2 + 18
        line([centre, mid + box_h / 2, centre, battery_top], path == "battery")

        mains_state = flow.get("mains_state")
        mains_colour = {"ok": ACCENT_OK, "bad": ACCENT_ALARM}.get(mains_state, FLOW_IDLE)
        on_path = lit if path else FLOW_IDLE
        for name, label, value, outline in (
                ("mains", "Mains", flow.get("mains", "--"), mains_colour),
                ("ups", "UPS", "", on_path if path in ("inverter", "battery") else FLOW_IDLE),
                ("load", "Load", flow.get("load", "--"), on_path)):
            x = boxes[name]
            self._rounded(x - box_w / 2, mid - box_h / 2, x + box_w / 2, mid + box_h / 2, 8,
                          fill=GROUP_BG, outline=outline, width=2)
            if value:
                self.create_text(x, mid - 8, text=label, fill=TEXT_DIM, font=("Helvetica", 10))
                self.create_text(x, mid + 8, text=value, fill=TEXT, font=("Helvetica", 12, "bold"))
            else:
                self.create_text(x, mid, text=label, fill=TEXT, font=("Helvetica", 12, "bold"))
        self._rounded(centre - box_w / 2, battery_top, centre + box_w / 2, battery_top + 24, 8,
                      fill=GROUP_BG, outline=lit if path == "battery" else FLOW_IDLE, width=2)
        self.create_text(centre, battery_top + 12, text="Battery", fill=TEXT_DIM,
                         font=("Helvetica", 10))
        return int(battery_top + 24 + 18)


# --------------------------------------------------------------------- graphs

class LineChart(tk.Canvas):
    """Hourly history as lines, drawn directly on a canvas.

    band     list of (time, low, high): shaded area between lowest and highest
    lines    list of (label, colour, dashed, [(time, value), ...])
    A gap of more than one hour between points breaks the line, so time the
    program was not running is shown as a gap instead of a straight join.
    """

    PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 56, 16, 30, 28

    def __init__(self, parent, title: str, unit: str):
        super().__init__(parent, bg=CARD, highlightthickness=0, bd=0)
        self.title, self.unit = title, unit
        self.band, self.lines = [], []
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _event: self.delete("hover"))

    def show(self, band: list, lines: list) -> None:
        self.band, self.lines = band, lines
        self.redraw()

    # ----------------------------------------------------------- geometry

    def _times(self):
        times = [point[0] for point in self.band]
        for _label, _colour, _dashed, points in self.lines:
            times += [time for time, _value in points]
        return times

    def _values(self):
        values = []
        for _time, low, high in self.band:
            values += [low, high]
        for _label, _colour, _dashed, points in self.lines:
            values += [value for _time, value in points]
        return values

    def _scales(self):
        times, values = self._times(), self._values()
        if not times:
            return None
        start, end = min(times), max(times)
        if end == start:
            end = start + timedelta(hours=1)
        low, high = min(values), max(values)
        margin = max((high - low) * 0.15, 0.5)
        low, high = low - margin, high + margin
        width, height = self.winfo_width(), self.winfo_height()
        left, right = self.PAD_LEFT, width - self.PAD_RIGHT
        top, bottom = self.PAD_TOP, height - self.PAD_BOTTOM
        span = (end - start).total_seconds()

        def x_of(time):
            return left + (right - left) * (time - start).total_seconds() / span

        def y_of(value):
            return bottom - (bottom - top) * (value - low) / (high - low)

        return start, end, low, high, left, right, top, bottom, x_of, y_of

    @staticmethod
    def _segments(points):
        """Split a series wherever consecutive points are over an hour apart."""
        segment, previous = [], None
        for time, value in points:
            if previous is not None and (time - previous) > timedelta(minutes=90):
                if segment:
                    yield segment
                segment = []
            segment.append((time, value))
            previous = time
        if segment:
            yield segment

    # ------------------------------------------------------------ drawing

    def redraw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 120 or height < 100:
            return
        self.create_text(self.PAD_LEFT, 14, anchor="w", fill=TEXT,
                         font=("Helvetica", 13, "bold"),
                         text=f"{self.title}  ({self.unit})")

        scales = self._scales()
        if scales is None:
            self.create_text(width / 2, height / 2, fill=TEXT_DIM,
                             font=("Helvetica", 12), text="no readings in this range")
            return
        start, end, low, high, left, right, top, bottom, x_of, y_of = scales

        # Horizontal grid with value labels.
        for step in range(6):
            value = low + (high - low) * step / 5
            y = y_of(value)
            self.create_line(left, y, right, y, fill="#30353e")
            self.create_text(left - 8, y, anchor="e", fill=TEXT_DIM,
                             font=("Helvetica", 10), text=f"{value:.1f}")

        # Time labels: clock times for about a day, date and time for about a
        # week, dates beyond that. Dates alone repeat on short ranges.
        span_hours = (end - start).total_seconds() / 3600
        if span_hours <= 36:
            label_format = "%H:%M"
        elif span_hours <= 8 * 24:
            label_format = "%d.%m %H:%M"
        else:
            label_format = "%d.%m"
        for step in range(7):
            moment = start + (end - start) * step / 6
            x = x_of(moment)
            self.create_line(x, top, x, bottom, fill="#2a2f37")
            # The outer labels align inward so the last one is not clipped.
            anchor = "nw" if step == 0 else "ne" if step == 6 else "n"
            self.create_text(x, bottom + 6, anchor=anchor, fill=TEXT_DIM,
                             font=("Helvetica", 10),
                             text=moment.astimezone().strftime(label_format))

        # Min-max band, drawn per continuous segment.
        band_points = [(time, (low_value, high_value))
                       for time, low_value, high_value in self.band]
        for segment in self._segments(band_points):
            if len(segment) < 2:
                continue
            upper = [(x_of(t), y_of(v[1])) for t, v in segment]
            lower = [(x_of(t), y_of(v[0])) for t, v in reversed(segment)]
            polygon = [coordinate for point in upper + lower for coordinate in point]
            self.create_polygon(polygon, fill=GRAPH_BAND, outline="")

        # Lines, and a legend along the top right.
        legend_x = right
        for label, colour, dashed, points in reversed(self.lines):
            for segment in self._segments(points):
                coordinates = [c for t, v in segment for c in (x_of(t), y_of(v))]
                if len(segment) == 1:
                    x, y = coordinates
                    self.create_oval(x - 2, y - 2, x + 2, y + 2, fill=colour, outline="")
                    continue
                self.create_line(*coordinates, fill=colour, width=2,
                                 dash=(6, 4) if dashed else None)
            text_id = self.create_text(legend_x, 14, anchor="e", fill=colour,
                                       font=("Helvetica", 11), text=label)
            legend_x = self.bbox(text_id)[0] - 16
        if self.band:
            self.create_text(legend_x, 14, anchor="e", fill="#7fa6c9",
                             font=("Helvetica", 11), text="low to high")

        self.create_rectangle(left, top, right, bottom, outline="#3a404a")

    def _hover(self, event) -> None:
        """Show the values of the hour nearest the pointer."""
        self.delete("hover")
        scales = self._scales()
        if scales is None:
            return
        start, end, low, high, left, right, top, bottom, x_of, y_of = scales
        if not left <= event.x <= right:
            return
        candidates = sorted(set(self._times()))
        nearest = min(candidates, key=lambda time: abs(x_of(time) - event.x))
        x = x_of(nearest)
        self.create_line(x, top, x, bottom, fill="#8a93a3", dash=(2, 3), tags="hover")

        lines = [nearest.astimezone().strftime("%d.%m %H:00")]
        for time, low_value, high_value in self.band:
            if time == nearest:
                lines.append(f"low {low_value:.1f}  high {high_value:.1f}")
        for label, _colour, _dashed, points in self.lines:
            for time, value in points:
                if time == nearest:
                    lines.append(f"{label} {value:.1f}")
        text = "\n".join(lines)
        box_x = x + 12 if x < (left + right) / 2 else x - 12
        anchor = "nw" if x < (left + right) / 2 else "ne"
        text_id = self.create_text(box_x, top + 8, anchor=anchor, text=text,
                                   fill=TEXT, font=("Helvetica", 11), tags="hover")
        x1, y1, x2, y2 = self.bbox(text_id)
        box = self.create_rectangle(x1 - 6, y1 - 4, x2 + 6, y2 + 4,
                                    fill="#15171c", outline="#3a404a", tags="hover")
        self.tag_lower(box, text_id)


# -------------------------------------------------------------------- window

class Panel(tk.Tk):
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.silenced = set()        # alarm ids whose sound the user stopped
        self.notified = set()        # notice ids whose soft sound has played
        self.info_window = None      # the Info button's window, while open
        self.devices_window = None   # the rack popup's Devices window, while open
        self.devices_rack = None
        self.sound_due = False

        self.title("dcmonitor")
        # The program's own icon in the title bar and taskbar, on Windows.
        # dcmonitor.ico sits in the project folder, next to main.py.
        icon = Path(__file__).resolve().parent.parent / "dcmonitor.ico"
        if platform.system() == "Windows" and icon.is_file():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        self.configure(bg=BACKGROUND)
        self.geometry("1500x860")
        self.minsize(1100, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._style_tables()
        self._build_alarm_bar()
        self._build_tabs()

        self.refresh_events()
        self.refresh_program_log()

        self.after(QUEUE_INTERVAL_MS, self._drain_queue)
        self.after(TICK_INTERVAL_MS, self._tick)
        self.after(SOUND_INTERVAL_MS, self._sound_tick)
        self.after(LOG_REFRESH_MS, self._logs_tick)
        self.after(JOBS_REFRESH_MS, self._jobs_tick)
        self.after(UPS_TAB_REFRESH_MS, self._ups_tab_tick)
        self.after(ALARMS_REFRESH_MS, self._alarms_tick)
        self.after(GRAPHS_REFRESH_MS, self._graphs_tick)
        self.after(MAP_REFRESH_MS, self._map_tick)

    def _build_alarm_bar(self) -> None:
        self.bar = tk.Frame(self, bg=BAR_OK, padx=14, pady=12)
        self.bar.pack(fill="x", side="top")

        # Buttons are packed first so they always keep their space; the text
        # gets whatever is left. Packed the other way round, a long alarm text
        # pushed Silence off the screen exactly when it was needed.
        self.silence_button = tk.Button(self.bar, text="Silence",
                                        font=("Helvetica", 14, "bold"),
                                        command=self._silence, state="disabled")
        self.silence_button.pack(side="right", padx=4)
        tk.Button(self.bar, text="Poll all now", font=("Helvetica", 13),
                  command=self._poll_all).pack(side="right", padx=4)
        tk.Button(self.bar, text="Refresh", font=("Helvetica", 13),
                  command=self.refresh).pack(side="right", padx=4)
        tk.Button(self.bar, text="Info", font=("Helvetica", 13),
                  command=self._show_info).pack(side="right", padx=4)

        self.bar_text = tk.Label(self.bar, text="All normal",
                                 font=("Helvetica", 20, "bold"),
                                 bg=BAR_OK, fg=TEXT, anchor="w", justify="left")
        self.bar_text.pack(side="left", fill="x", expand=True)
        # Clicking the alarm text opens the first alarm's device: a rack on
        # the floor map with its popup, a UPS on the UPS tab.
        self.bar_text.configure(cursor="hand2")
        self.bar_text.bind("<Button-1>", lambda _event: self._jump_to_first_alarm())

    def _style_tables(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")      # the only theme that recolours cleanly
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=TEXT_DIM,
                        padding=(16, 8), font=("Helvetica", 12, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", BACKGROUND)],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=TABLE_SIZE * 2 + 4, borderwidth=0,
                        font=("Helvetica", TABLE_SIZE))
        style.configure("Treeview.Heading", background="#2c313a", foreground=TEXT_DETAIL,
                        font=("Helvetica", TABLE_SIZE, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#37455c")])

    def _build_tabs(self) -> None:
        """The pages, switched from our own tab bar: views of the site on the
        left, tools (logs, graphs, alarms, jobs) on the right. ttk's own tab
        strip cannot split like that, so it is hidden and only its page
        switching is used; the tab texts stay, so the *_tick checks work."""
        style = ttk.Style(self)
        style.layout("Main.TNotebook.Tab", [])
        style.configure("Main.TNotebook", background=BACKGROUND, borderwidth=0,
                        tabmargins=0)
        bar = tk.Frame(self, bg=BACKGROUND, padx=10, pady=4)
        bar.pack(fill="x", side="top")
        self.tab_groups = {"left": tk.Frame(bar, bg=BACKGROUND),
                           "right": tk.Frame(bar, bg=BACKGROUND)}
        self.tab_groups["left"].pack(side="left")
        self.tab_groups["right"].pack(side="right")
        self.tab_buttons = {}
        self.tab_pages = {}             # tab name -> page frame
        self.tabs = ttk.Notebook(self, style="Main.TNotebook")
        self.tabs.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", lambda _event: self._mark_tab())

        self._add_tab("Overview", self._build_overview, "left")
        if self.monitor.config.ups_units:
            self._add_tab("UPS", self._build_ups_tab, "left")
        self._add_tab("Floor map", self._build_floor_map, "left")
        self._add_tab("Logs", self._build_logs, "right")
        self._add_tab("Graphs", self._build_graphs, "right")
        self._add_tab("Alarms", self._build_alarms, "right")
        self._add_tab("Jobs", self._build_jobs, "right")
        self._mark_tab()

    def _add_tab(self, name: str, build, side: str) -> None:
        page = tk.Frame(self.tabs, bg=BACKGROUND)
        self.tabs.add(page, text=name)
        button = tk.Label(self.tab_groups[side], text=name, bg=CARD, fg=TEXT_DIM,
                          font=("Helvetica", 12, "bold"), padx=16, pady=8,
                          cursor="hand2")
        button.pack(side="left", padx=(0, 2))
        button.bind("<Button-1>", lambda _event: self.tabs.select(page))
        self.tab_buttons[str(page)] = button
        self.tab_pages[name] = page
        build(page)

    def _jump_to_first_alarm(self) -> None:
        alarms = self.monitor.storage.active_alarms()
        real = [alarm for alarm in alarms if not is_notice(alarm)]
        first = (real or alarms or [None])[0]
        if first is not None:
            self._jump_to(first["device"], first["kind"])

    def _jump_to(self, device: str, kind: str = "") -> None:
        """Open the place where a device is shown: a rack on the floor map
        with its popup (breaker mode, temperature mode for a rack temperature
        alarm), an AC unit on the floor map, a UPS on the UPS tab, anything
        else on the Overview."""
        config = self.monitor.config
        if device.startswith("Rack ") and self.floor_map is not None:
            try:
                number = int(device.split()[1])
            except (IndexError, ValueError):
                return
            self.tabs.select(self.tab_pages["Floor map"])
            self.map_mode.set("temperature" if kind == "rack_temperature" else "breakers")
            self._map_mode_changed()
            self.floor_map.pin(("rack", number))
        elif any(unit.name == device for unit in config.ac_units) and self.floor_map is not None:
            self.tabs.select(self.tab_pages["Floor map"])
            self.refresh_floor_map()
            self.floor_map.pin(("ac", device))
        elif any(unit.name == device for unit in config.ups_units) and "UPS" in self.tab_pages:
            self.tabs.select(self.tab_pages["UPS"])
        else:
            self.tabs.select(self.tab_pages["Overview"])

    def _accept_change(self, key: str) -> None:
        """The popup's Accept button: the circuit's value now is its normal."""
        if self.monitor.follower is None:
            return
        text = self.monitor.follower.accept(key)
        import logging
        logging.getLogger("dcmonitor").info("accepted by the user: %s", text)
        self.refresh()
        self.refresh_floor_map()

    def _maintenance(self, pdu_id: int, on: bool) -> None:
        """The popup's Maintenance / End maintenance buttons. Going in asks
        first: a PDU in maintenance raises no alarms at all."""
        name = next((e["name"] for e in (self.monitor.config.pdus or {}).get("entries", ())
                     if e["id"] == pdu_id), f"PDU {pdu_id}")
        if on:
            if not messagebox.askyesno(
                    "PDU maintenance",
                    f"Put {name} in maintenance?\n\n"
                    "Its open alarms close and it raises none (not answering, "
                    "breaker load, breaker tripped, circuit changes) until "
                    "maintenance is ended here. It is still read and shown.",
                    parent=self):
                return
            self.monitor.start_maintenance(pdu_id)
        else:
            self.monitor.end_maintenance(pdu_id)
        self.refresh()
        self.refresh_floor_map()

    def _mark_tab(self) -> None:
        """Show which page is open: selected tab takes the page colour."""
        try:
            current = self.tabs.select()
        except tk.TclError:
            return
        for page, button in self.tab_buttons.items():
            selected = page == current
            button.configure(bg=BACKGROUND if selected else CARD,
                             fg=TEXT if selected else TEXT_DIM)

    def _build_overview(self, parent) -> None:
        body = tk.Frame(parent, bg=BACKGROUND, padx=10, pady=8)
        body.pack(fill="both", expand=True)

        self.cards = {}

        section_label(body, "AC UNITS").pack(fill="x", pady=(4, 2))
        ac_frame = tk.Frame(body, bg=BACKGROUND)
        ac_frame.pack(fill="both", expand=True)
        # AC cards keep a sensible minimum; any extra height goes to them, not
        # to the UPS row, which has a fixed height so its lines never overlap.
        for column in range(5):
            ac_frame.columnconfigure(column, weight=1, uniform="ac")
        for row in range(2):
            ac_frame.rowconfigure(row, weight=1, minsize=150)
        for index, unit in enumerate(self.monitor.config.ac_units):
            card = Card(ac_frame)
            card.grid(row=index // 5, column=index % 5, sticky="nsew", padx=5, pady=5)
            self.cards[unit.name] = card

        # UPS and generators share the bottom row, so adding the generators
        # costs no height from the AC cards. Each group sits in its own framed
        # box with a gap between, so the two are never read as one row.
        bottom = tk.Frame(body, bg=BACKGROUND)
        # Packed ahead of the AC cards, so on a short window the UPS and
        # generator row keeps its full height and the AC cards give up their
        # empty space instead of pushing this row off the screen.
        bottom.pack(side="bottom", fill="x", expand=False, pady=(12, 0), before=ac_frame)
        groups = [("UPS SYSTEMS", list(self.monitor.config.ups_units)),
                  ("GENERATORS", list(self.monitor.config.generators))]
        groups = [(title, units) for title, units in groups if units]
        for position, (title, units) in enumerate(groups):
            # Box widths follow their card counts, so all cards are one size.
            bottom.columnconfigure(position, weight=len(units), uniform="groups")
            box = tk.Frame(bottom, bg=GROUP_BG, padx=6, pady=6,
                           highlightthickness=1, highlightbackground=GROUP_BORDER,
                           highlightcolor=GROUP_BORDER)
            gap = (0 if position == 0 else 8, 0 if position == len(groups) - 1 else 8)
            box.grid(row=0, column=position, sticky="nsew", padx=gap)
            for column in range(len(units)):
                box.columnconfigure(column, weight=1, uniform="cards")
            # Tall enough for the UPS phase rows; generator cards share it.
            box.rowconfigure(1, minsize=294)
            section_label(box, title, bg=GROUP_BG).grid(
                row=0, column=0, columnspan=len(units), sticky="ew", pady=(0, 2))
            for index, unit in enumerate(units):
                card = Card(box, compact=True, bg=GROUP_BG)
                card.grid(row=1, column=index, sticky="nsew", padx=5, pady=5)
                self.cards[unit.name] = card

    # ----------------------------------------------------------------- UPS

    def _build_ups_tab(self, parent) -> None:
        """One panel per UPS, side by side."""
        body = tk.Frame(parent, bg=BACKGROUND, padx=10, pady=10)
        body.pack(fill="both", expand=True)
        self.ups_panels = {}
        for column, unit in enumerate(self.monitor.config.ups_units):
            body.columnconfigure(column, weight=1, uniform="ups")
            panel = UpsPanel(body)
            panel.configure(height=UpsPanel.HEIGHT)
            panel.grid(row=0, column=column, sticky="new", padx=6)
            self.ups_panels[unit.name] = panel
        self.refresh_ups_tab()

    def refresh_ups_tab(self) -> None:
        by_device = {}
        for alarm in self.monitor.storage.active_alarms():
            by_device.setdefault(alarm["device"], []).append(alarm)
        for unit in self.monitor.config.ups_units:
            age, values = self._age_and_values(unit.name)
            self.ups_panels[unit.name].show(ups_panel_content(
                unit, values, self.monitor.states.get(unit.name, {}),
                by_device.get(unit.name, []), age, self.monitor.config.thresholds))

    # ---------------------------------------------------------------- logs

    def _build_logs(self, parent) -> None:
        """Two views in one tab: unit events from the database, program log."""
        inner = ttk.Notebook(parent)
        inner.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_tabs = inner

        events = tk.Frame(inner, bg=BACKGROUND)
        inner.add(events, text="Unit events")
        self._build_event_view(events)

        readings = tk.Frame(inner, bg=BACKGROUND)
        inner.add(readings, text="Readings")
        self._build_readings_view(readings)

        program = tk.Frame(inner, bg=BACKGROUND)
        inner.add(program, text="Program log")
        self._build_program_view(program)

    def _controls_row(self, parent) -> tk.Frame:
        row = tk.Frame(parent, bg=BACKGROUND, pady=8)
        row.pack(fill="x")
        return row

    def _build_event_view(self, parent) -> None:
        controls = self._controls_row(parent)

        tk.Label(controls, text="Device", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(4, 4))
        names = ["All devices"] + [device.name for device in self.monitor.config.devices]
        self.event_device = ttk.Combobox(controls, values=names, state="readonly",
                                         width=14)
        self.event_device.current(0)
        self.event_device.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Range", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.event_range = ttk.Combobox(controls, values=list(RANGES), state="readonly",
                                        width=14)
        self.event_range.current(1)
        self.event_range.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Search", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.event_search = tk.Entry(controls, width=24, bg=CARD, fg=TEXT,
                                     insertbackground=TEXT, relief="flat")
        self.event_search.pack(side="left", padx=(0, 12))

        tk.Button(controls, text="Apply", font=("Helvetica", 11),
                  command=self.refresh_events).pack(side="left")
        self.event_count = tk.Label(controls, text="", bg=BACKGROUND, fg=TEXT_DIM,
                                    font=("Helvetica", 11))
        self.event_count.pack(side="right", padx=8)

        for widget in (self.event_device, self.event_range):
            widget.bind("<<ComboboxSelected>>", lambda _event: self.refresh_events())
        self.event_search.bind("<Return>", lambda _event: self.refresh_events())

        columns = ("time", "device", "description", "action", "source")
        headings = ("Time", "Device", "Event", "Start/Stop", "Source")
        widths = (170, 90, 520, 110, 100)
        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.pack(fill="both", expand=True)
        self.event_table = ttk.Treeview(frame, columns=columns, show="headings")
        for column, heading, width in zip(columns, headings, widths):
            self.event_table.heading(column, text=heading)
            self.event_table.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical",
                               command=self.event_table.yview)
        self.event_table.configure(yscrollcommand=scroll.set)
        self.event_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # Columns per device type: (key, heading, unit, show min and max).
    # The key is resolved to a real metric name per device, because the old and
    # new AC units store their temperature under different names.
    AC_COLUMNS = [
        ("temp", "Temp", "\u00b0C", True),
        ("humidity", "Hum", "%", True),
        ("setpoint", "Setpoint", "\u00b0C", False),
    ]
    UPS_COLUMNS = [
        ("battery_temp", "UPS temp", "\u00b0C", True),
        ("charge", "Charge", "%", False),
        ("load1", "Load L1", "%", False),
        ("load2", "Load L2", "%", False),
        ("load3", "Load L3", "%", False),
    ]
    AC_METRICS_NEW = {"temp": "return_temp_c", "humidity": "return_humidity_pct",
                      "setpoint": "supply_setpoint_c"}
    AC_METRICS_OLD = {"temp": "return_temp_c", "humidity": "return_humidity_pct",
                      "setpoint": "return_setpoint_c"}
    UPS_METRICS = {"battery_temp": "battery_temp_c", "charge": "battery_charge_pct",
                   "load1": "output_load_pct_l1", "load2": "output_load_pct_l2",
                   "load3": "output_load_pct_l3"}

    RACK_COLUMNS = [
        ("rack_temp", "Temp", "\u00b0C", True),
        ("rack_power", "Power", "kW", True),
        ("rack_breaker", "Breaker max", "%", False),
    ]
    RACK_METRICS = {"rack_temp": "rack_temp_c", "rack_power": "rack_power_kw",
                    "rack_breaker": "rack_breaker_max_pct"}
    GEN_COLUMNS = [
        ("fuel", "Fuel", "%", True),
        ("battery", "Battery", "V", True),
    ]
    GEN_METRICS = {"fuel": "fuel_level_pct", "battery": "battery_v"}

    ALL_AC = "All AC units"
    ALL_UPS = "All UPS"
    ALL_RACKS = "All racks"
    ALL_GEN = "All generators"
    GROUPS = ("All AC units", "All UPS", "All racks", "All generators")

    def _is_generator(self, device_name: str) -> bool:
        return any(unit.name == device_name for unit in self.monitor.config.generators)

    def _is_ups(self, device_name: str) -> bool:
        return any(unit.name == device_name for unit in self.monitor.config.ups_units)

    @staticmethod
    def _is_rack(device_name: str) -> bool:
        return device_name.startswith("Rack ")

    def _rack_devices(self) -> list:
        """Racks with a reading, in number order."""
        numbers = set(self.monitor.racks_view()) | set(self.monitor.rack_power)
        return [f"Rack {number}" for number in sorted(numbers)]

    def _device_choices(self, with_groups: bool) -> list:
        names = [device.name for device in self.monitor.config.devices]
        if with_groups:
            names = [self.ALL_AC, self.ALL_UPS] + names
            if self.monitor.config.generators:
                names.insert(2, self.ALL_GEN)
            if self.monitor.config.pdu_sensors or self.monitor.config.pdus:
                names.insert(2, self.ALL_RACKS)
        return names + self._rack_devices()

    def _metric_map(self, device_name: str) -> dict:
        if self._is_rack(device_name):
            return self.RACK_METRICS
        if self._is_generator(device_name):
            return self.GEN_METRICS
        for unit in self.monitor.config.ac_units:
            if unit.name == device_name:
                return self.AC_METRICS_NEW if unit.is_new else self.AC_METRICS_OLD
        return self.UPS_METRICS

    def _selected_devices(self, choice: str) -> list:
        if choice == self.ALL_AC:
            return [unit.name for unit in self.monitor.config.ac_units]
        if choice == self.ALL_UPS:
            return [unit.name for unit in self.monitor.config.ups_units]
        if choice == self.ALL_RACKS:
            return self._rack_devices()
        if choice == self.ALL_GEN:
            return [unit.name for unit in self.monitor.config.generators]
        return [choice]

    def _columns_for(self, choice: str) -> list:
        if choice == self.ALL_RACKS or self._is_rack(choice):
            return self.RACK_COLUMNS
        if choice == self.ALL_GEN or self._is_generator(choice):
            return self.GEN_COLUMNS
        if choice == self.ALL_UPS or self._is_ups(choice):
            return self.UPS_COLUMNS
        return self.AC_COLUMNS

    def _build_readings_view(self, parent) -> None:
        """Hourly history, one row per hour, every metric of the device at once."""
        controls = self._controls_row(parent)

        tk.Label(controls, text="Device", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(4, 4))
        # The rack list is filled by the PDU reads after startup, so the choices are
        # rebuilt each time the list is opened.
        self.reading_device = ttk.Combobox(
            controls, values=self._device_choices(True), state="readonly", width=16,
            postcommand=lambda: self.reading_device.configure(values=self._device_choices(True)),
        )
        self.reading_device.current(0)
        self.reading_device.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Range", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.reading_range = ttk.Combobox(controls, values=list(RANGES),
                                          state="readonly", width=14)
        self.reading_range.current(1)
        self.reading_range.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="From", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.reading_from = tk.Entry(controls, width=17, bg=CARD, fg=TEXT,
                                     insertbackground=TEXT, relief="flat")
        self.reading_from.pack(side="left", padx=(0, 8))
        tk.Label(controls, text="To", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.reading_to = tk.Entry(controls, width=17, bg=CARD, fg=TEXT,
                                   insertbackground=TEXT, relief="flat")
        self.reading_to.pack(side="left", padx=(0, 8))

        tk.Button(controls, text="Apply", font=("Helvetica", 11),
                  command=self.refresh_readings).pack(side="left")
        tk.Button(controls, text="Clear times", font=("Helvetica", 11),
                  command=self._clear_times).pack(side="left", padx=6)
        self.reading_count = tk.Label(controls, text="", bg=BACKGROUND, fg=TEXT_DIM,
                                      font=("Helvetica", 11))
        self.reading_count.pack(side="right", padx=8)

        for entry in (self.reading_from, self.reading_to):
            entry.bind("<Return>", lambda _event: self.refresh_readings())

        self.reading_device.bind("<<ComboboxSelected>>",
                                 lambda _event: self._rebuild_reading_columns())
        self.reading_range.bind("<<ComboboxSelected>>",
                                lambda _event: self.refresh_readings())

        self.reading_frame = tk.Frame(parent, bg=BACKGROUND)
        self.reading_frame.pack(fill="both", expand=True)
        self.reading_table = None
        self._rebuild_reading_columns()

    def _rebuild_reading_columns(self) -> None:
        """Rebuilt when the choice changes, since the columns change with it."""
        for child in self.reading_frame.winfo_children():
            child.destroy()

        choice = self.reading_device.get()
        grouped = choice in self.GROUPS

        columns = ["hour"]
        headings = ["Hour"]
        if grouped:
            columns.append("device")
            headings.append("Device")
        for key, label, unit, detailed in self._columns_for(choice):
            if detailed:
                for suffix, word in (("min", "low"), ("avg", "avg"), ("max", "high")):
                    columns.append(f"{key}_{suffix}")
                    headings.append(f"{label} {word} {unit}")
            else:
                columns.append(f"{key}_avg")
                headings.append(f"{label} {unit}")
        columns.append("samples")
        headings.append("Readings")

        self.reading_table = ttk.Treeview(self.reading_frame, columns=columns,
                                          show="headings")
        for column, heading in zip(columns, headings):
            self.reading_table.heading(column, text=heading)
            if column == "hour":
                self.reading_table.column(column, width=150, anchor="w", stretch=False)
            elif column == "device":
                self.reading_table.column(column, width=90, anchor="w", stretch=False)
            else:
                self.reading_table.column(column, width=125, anchor="e", stretch=False)
        scroll = ttk.Scrollbar(self.reading_frame, orient="vertical",
                               command=self.reading_table.yview)
        self.reading_table.configure(yscrollcommand=scroll.set)
        self.reading_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.refresh_readings()

    def refresh_readings(self) -> None:
        """Reload the hourly history, one row per hour and device."""
        if self.reading_table is None:
            return
        choice = self.reading_device.get()
        if not choice:
            return
        days = RANGES.get(self.reading_range.get(), 7)
        grouped = choice in self.GROUPS
        specs = self._columns_for(choice)

        # Typed times win over the preset range. Either box may be left empty:
        # only From means "since then", only To means "up to then".
        typed_from = self.reading_from.get().strip()
        typed_to = self.reading_to.get().strip()
        start = hour_key_from_local(typed_from)
        end = hour_key_from_local(typed_to, end_of_day=True)
        for typed, parsed in ((typed_from, start), (typed_to, end)):
            if typed and parsed is None:
                self.reading_table.delete(*self.reading_table.get_children())
                self.reading_count.configure(
                    text=f"cannot read {typed!r}, use 18.09.2026 14:30"
                )
                return
        custom = bool(start or end)
        if custom:
            start = start or "0000-01-01T00"
            end = end or "9999-12-31T23"

        rows = {}
        for device in self._selected_devices(choice):
            metrics = self._metric_map(device)
            for key, _label, _unit, _detailed in specs:
                metric = metrics.get(key)
                if not metric:
                    continue
                history = (
                    self.monitor.storage.history_between(device, metric, start, end)
                    if custom
                    else self.monitor.storage.history(device, metric, days)
                )
                for row in history:
                    slot = rows.setdefault((row["hour_utc"], device), {"samples": 0})
                    slot[key] = row
                    slot["samples"] = max(slot["samples"], row["samples"])

        self.reading_table.delete(*self.reading_table.get_children())
        # Newest hour first; inside an hour, devices in configuration order.
        order = {device.name: index
                 for index, device in enumerate(self.monitor.config.devices)}

        def position(name):
            # Configured devices first in config order, then racks by number.
            if name in order:
                return order[name]
            digits = "".join(ch for ch in name if ch.isdigit())
            return 1000 + int(digits) if digits else 10000

        for hour_utc, device in sorted(rows,
                                       key=lambda item: (item[0], -position(item[1])),
                                       reverse=True):
            slot = rows[(hour_utc, device)]
            values = [local_text(hour_utc + ":00:00Z")[:-3]]
            if grouped:
                values.append(device)
            for key, _label, _unit, detailed in specs:
                row = slot.get(key)
                keys = ("min_value", "avg_value", "max_value") if detailed else ("avg_value",)
                for name in keys:
                    values.append(f"{row[name]:.1f}" if row else "--")
            values.append(slot["samples"])
            self.reading_table.insert("", "end", values=values)

        hours = len({hour for hour, _device in rows})
        if not rows:
            first = self.monitor.storage.first_reading_hour()
            text = ("nothing recorded in that range; history starts "
                    + local_text(first + ":00:00Z")[:-3]) if first else \
                   "no readings recorded yet"
        else:
            text = (f"{len(rows)} rows over {hours} hours" if grouped
                    else f"{hours} hours")
            if custom:
                text += "  (typed times)"
        self.reading_count.configure(text=text)

    def _clear_times(self) -> None:
        """Back to the preset range."""
        self.reading_from.delete(0, "end")
        self.reading_to.delete(0, "end")
        self.refresh_readings()

    def _build_program_view(self, parent) -> None:
        controls = self._controls_row(parent)
        tk.Label(controls, text="Search", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(4, 4))
        self.program_search = tk.Entry(controls, width=24, bg=CARD, fg=TEXT,
                                       insertbackground=TEXT, relief="flat")
        self.program_search.pack(side="left", padx=(0, 12))
        self.program_search.bind("<Return>", lambda _event: self.refresh_program_log())

        tk.Button(controls, text="Apply", font=("Helvetica", 11),
                  command=self.refresh_program_log).pack(side="left")
        tk.Button(controls, text="Alarms only", font=("Helvetica", 11),
                  command=self._filter_alarms).pack(side="left", padx=8)
        self.program_path = tk.Label(controls, text="", bg=BACKGROUND, fg=TEXT_DIM,
                                     font=("Helvetica", 10))
        self.program_path.pack(side="right", padx=8)

        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.pack(fill="both", expand=True)
        self.program_text = tk.Text(frame, bg=CARD, fg=TEXT, relief="flat",
                                    font=("Menlo", 11), wrap="none",
                                    insertbackground=TEXT)
        scroll = ttk.Scrollbar(frame, orient="vertical",
                               command=self.program_text.yview)
        self.program_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.program_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _filter_alarms(self) -> None:
        self.program_search.delete(0, "end")
        self.program_search.insert(0, "ALARM")
        self.refresh_program_log()

    def refresh_events(self) -> None:
        """Reload the unit events table from the database."""
        device = self.event_device.get()
        days = RANGES.get(self.event_range.get(), 7)
        needle = self.event_search.get().strip().lower()

        rows = self.monitor.storage.recent_events(
            days, None if device == "All devices" else device
        )
        if needle:
            rows = [row for row in rows
                    if needle in f"{row['description']} {row['name']}".lower()]

        self.event_table.delete(*self.event_table.get_children())
        for row in rows[:2000]:
            self.event_table.insert("", "end", values=(
                local_text(row["event_utc"]),
                row["device"],
                row["description"] or row["name"],
                row["action"],
                "unit log" if row["source"] == "unit_log" else row["source"],
            ))
        shown = min(len(rows), 2000)
        self.event_count.configure(
            text=f"{shown} shown" + (f" of {len(rows)}" if len(rows) > shown else "")
        )

    def refresh_program_log(self) -> None:
        """Reload the tail of today's log file."""
        path = self.monitor.config.log_folder / "dcmonitor.log"
        needle = self.program_search.get().strip().lower()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            lines = [f"cannot read {path}: {error}"]

        if needle:
            lines = [line for line in lines if needle in line.lower()]
        lines = lines[-PROGRAM_LOG_LINES:]

        self.program_text.configure(state="normal")
        self.program_text.delete("1.0", "end")
        self.program_text.insert("1.0", "\n".join(lines))
        self.program_text.see("end")
        self.program_text.configure(state="disabled")
        self.program_path.configure(text=str(path))

    # ----------------------------------------------------------- floor map

    def _build_floor_map(self, parent) -> None:
        """Racks and AC units in their real positions, from site_map.toml."""
        self.floor_map = None
        header = self._controls_row(parent)
        # The switch: what the rack tiles show.
        self.map_mode = tk.StringVar(value="temperature")
        for mode, label in (("temperature", "Temperature"), ("power", "Power"),
                            ("breakers", "Breakers")):
            tk.Radiobutton(header, text=label, value=mode, variable=self.map_mode,
                           command=self._map_mode_changed, indicatoron=False,
                           font=("Helvetica", CONTROL_SIZE, "bold"), width=12,
                           bg=CARD, fg=TEXT_DETAIL, selectcolor="#37455c",
                           activebackground="#37455c", activeforeground=TEXT,
                           relief="flat", bd=0, padx=6, pady=4,
                           ).pack(side="left", padx=(4, 0))
        self.map_summary = tk.Label(header, text="", bg=BACKGROUND, fg=TEXT_DETAIL,
                                    font=("Helvetica", CONTROL_SIZE))
        self.map_summary.pack(side="left", padx=14)
        tk.Label(header, text="Click a rack or AC unit for details.", bg=BACKGROUND,
                 fg=TEXT_DIM, font=("Helvetica", CONTROL_SIZE)).pack(side="right", padx=8)

        try:
            layout = load_layout(self.monitor.config.site_map)
        except MapError as error:
            tk.Label(parent, text=f"Floor map not available: {error}", bg=BACKGROUND,
                     fg=TEXT_DIM, font=("Helvetica", 13)).pack(expand=True)
            return
        self.floor_map = FloorMap(parent, layout)
        # The same levels as the breaker alarm and warning, so the map's
        # colours match the alarm bar.
        settings = self.monitor.config.pdu_rules
        self.floor_map.on_accept = self._accept_change
        self.floor_map.on_maintenance = self._maintenance
        self.floor_map.on_devices = self._show_devices
        self.floor_map.breaker_alarm_pct = settings.get("breaker_alarm_pct")
        self.floor_map.breaker_warning_pct = settings.get("breaker_warning_pct")
        self.floor_map.breaker_highlight_pct = settings.get("breaker_highlight_pct")
        self.floor_map.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.refresh_floor_map()

    def refresh_floor_map(self) -> None:
        if self.floor_map is None:
            return
        active = {alarm["device"] for alarm in self.monitor.storage.active_alarms()}
        acs = {}
        for unit in self.monitor.config.ac_units:
            age, values = self._age_and_values(unit.name)
            temp = values.get("return_temp_c")
            status = self.monitor.states.get(unit.name, {}).get("unit_status", "")
            if unit.name in active:
                state = "alarm"
            elif age is None or age > STALE_AFTER_S:
                state = "stale"
            elif status == "Standby":
                state = "standby"
            elif status == "Unit Off":
                state = "off"
            else:
                state = "ok"
            acs[unit.name] = {"temp": temp, "state": state, "status": status}
        racks = dict(self.monitor.racks_view())
        self.floor_map.pdu_health = self.monitor.pdu_health
        self.floor_map.maintenance = self.monitor.alarms.maintenance
        # Circuit follower: its open messages per rack, and each circuit's normal.
        follow = {}
        for alarm in self.monitor.storage.active_alarms():
            if alarm["key"].startswith("follow_") and alarm["device"].startswith("Rack "):
                try:
                    number = int(alarm["device"].split()[1])
                except (IndexError, ValueError):
                    continue
                rule, key = alarm["key"][len("follow_"):].split(":", 1)
                follow.setdefault(number, []).append(
                    {"key": key, "rule": rule, "kind": alarm["kind"],
                     "description": alarm["description"]})
        self.floor_map.follow_alarms = follow
        if self.monitor.follower is not None:
            self.floor_map.normals = self.monitor.follower.normals
        if self.monitor.cabinet is not None:
            self.floor_map.device_counts = {
                rack: len(sheet.devices)
                for rack, sheet in self.monitor.cabinet.book.racks.items()}
        self._fill_devices()
        self.floor_map.show(racks, acs, self.monitor.rack_power, self.monitor.rack_breakers)

        mode = self.map_mode.get()
        if mode == "power":
            values = [(e["value"], r) for r, e in self.monitor.rack_power.items()
                      if e.get("value") is not None]
            if not values:
                self.map_summary.configure(text="Waiting for the first PDU power readings")
                return
            top, rack = max(values)
            self.map_summary.configure(
                text=f"{len(values)} racks, {sum(v for v, _ in values):.1f} kW in total, "
                     f"highest: rack {rack} at {top:.2f} kW")
            return
        if mode == "breakers":
            values = [(e["value"], r) for r, e in self.monitor.rack_breakers.items()
                      if e.get("value") is not None]
            if not values:
                self.map_summary.configure(text="Waiting for the first breaker readings")
                return
            top, rack = max(values)
            settings = self.monitor.config.pdu_rules
            percents = [c["percent"] for e in self.monitor.rack_breakers.values()
                        for c in e.get("circuits", []) if c.get("percent") is not None]
            counts = []
            for key, word in (("breaker_highlight_pct", ""), ("breaker_warning_pct", " warning"),
                              ("breaker_alarm_pct", " alarm")):
                level = settings.get(key)
                if level is not None:
                    counts.append(f"{sum(1 for p in percents if p >= level)} at "
                                  f"{level:g} %+{word}")
            self.map_summary.configure(
                text=f"{len(values)} racks with breakers, highest: rack {rack} at {top:.0f} %"
                     + (" \u00b7 circuits: " + ", ".join(counts) if counts else ""))
            return

        if not (self.monitor.config.pdu_sensors or self.monitor.config.pdus):
            self.map_summary.configure(text="No rack temperature source configured")
            return
        values = [(entry["value"], rack) for rack, entry in racks.items()
                  if entry.get("value") is not None and not entry.get("stale")]
        if not values:
            self.map_summary.configure(text="Waiting for the first PDU readings")
            return
        warmest, rack = max(values)
        self.map_summary.configure(
            text=f"{len(values)} racks reporting, warmest: rack {rack} at {warmest:.1f} \u00b0C"
        )

    def _map_mode_changed(self) -> None:
        if self.floor_map is not None:
            self.floor_map.set_mode(self.map_mode.get())
            self.refresh_floor_map()

    def _map_tick(self) -> None:
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "Floor map":
                self.refresh_floor_map()
        except tk.TclError:
            pass
        self.after(MAP_REFRESH_MS, self._map_tick)

    # -------------------------------------------------------------- graphs

    def _build_graphs(self, parent) -> None:
        """Two charts for one device: temperature and humidity, or UPS values."""
        controls = self._controls_row(parent)

        tk.Label(controls, text="Device", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(4, 4))
        self.graph_device = ttk.Combobox(
            controls, values=self._device_choices(False), state="readonly", width=14,
            postcommand=lambda: self.graph_device.configure(values=self._device_choices(False)),
        )
        self.graph_device.current(0)
        self.graph_device.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Range", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.graph_range = ttk.Combobox(controls, values=list(RANGES), state="readonly",
                                        width=14)
        self.graph_range.current(1)          # last 7 days
        self.graph_range.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Hourly values; hover a chart to read an hour.",
                 bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="right", padx=8)

        for widget in (self.graph_device, self.graph_range):
            widget.bind("<<ComboboxSelected>>", lambda _event: self.refresh_graphs())

        charts = tk.Frame(parent, bg=BACKGROUND)
        charts.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        charts.rowconfigure(0, weight=1)
        charts.rowconfigure(1, weight=1)
        charts.columnconfigure(0, weight=1)
        self.graph_top = LineChart(charts, "Temperature", "\u00b0C")
        self.graph_top.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self.graph_bottom = LineChart(charts, "Humidity", "%")
        self.graph_bottom.grid(row=1, column=0, sticky="nsew")
        self.refresh_graphs()

    def _history_points(self, device: str, metric: str, days: int):
        """(time, low, avg, high) per hour, times as UTC datetimes."""
        points = []
        for row in self.monitor.storage.history(device, metric, days):
            moment = datetime.strptime(row["hour_utc"], "%Y-%m-%dT%H").replace(
                tzinfo=timezone.utc) + timedelta(minutes=30)   # centre of the hour
            points.append((moment, row["min_value"], row["avg_value"], row["max_value"]))
        return points

    def refresh_graphs(self) -> None:
        device = self.graph_device.get()
        if not device:
            return
        days = RANGES.get(self.graph_range.get(), 7)
        metrics = self._metric_map(device)

        if self._is_generator(device):
            fuel = self._history_points(device, metrics["fuel"], days)
            battery = self._history_points(device, metrics["battery"], days)
            self.graph_top.title, self.graph_top.unit = "Fuel level", "%"
            self.graph_top.show(
                band=[(t, low, high) for t, low, _avg, high in fuel],
                lines=[("average", GRAPH_LINE, False, [(t, avg) for t, _l, avg, _h in fuel])],
            )
            self.graph_bottom.title, self.graph_bottom.unit = "Battery voltage", "V"
            self.graph_bottom.show(
                band=[(t, low, high) for t, low, _avg, high in battery],
                lines=[("average", GRAPH_LINE, False, [(t, avg) for t, _l, avg, _h in battery])],
            )
            return

        if self._is_ups(device):
            battery = self._history_points(device, metrics["battery_temp"], days)
            self.graph_top.title, self.graph_top.unit = "UPS temperature", "\u00b0C"
            self.graph_top.show(
                band=[(t, low, high) for t, low, _avg, high in battery],
                lines=[("average", GRAPH_LINE, False,
                        [(t, avg) for t, _low, avg, _high in battery])],
            )
            lines = []
            for index, key in enumerate(("load1", "load2", "load3")):
                load = self._history_points(device, metrics[key], days)
                lines.append((f"L{index + 1}", GRAPH_EXTRA[index], False,
                              [(t, avg) for t, _low, avg, _high in load]))
            self.graph_bottom.title, self.graph_bottom.unit = "Output load per phase", "%"
            self.graph_bottom.show(band=[], lines=lines)
            return

        if self._is_rack(device):
            rack = self._history_points(device, metrics["rack_temp"], days)
            self.graph_top.title, self.graph_top.unit = f"{device} inlet temperature", "\u00b0C"
            self.graph_top.show(
                band=[(t, low, high) for t, low, _avg, high in rack],
                lines=[("average", GRAPH_LINE, False,
                        [(t, avg) for t, _low, avg, _high in rack])],
            )
            power = self._history_points(device, metrics["rack_power"], days)
            self.graph_bottom.title, self.graph_bottom.unit = f"{device} power", "kW"
            self.graph_bottom.show(
                band=[(t, low, high) for t, low, _avg, high in power],
                lines=[("average", GRAPH_LINE, False,
                        [(t, avg) for t, _low, avg, _high in power])],
            )
            return

        temperature = self._history_points(device, metrics["temp"], days)
        setpoint = self._history_points(device, metrics["setpoint"], days)
        humidity = self._history_points(device, metrics["humidity"], days)
        is_new = metrics is self.AC_METRICS_NEW
        self.graph_top.title = "Return temperature" + (" (setpoint is supply)" if is_new else "")
        self.graph_top.unit = "\u00b0C"
        self.graph_top.show(
            band=[(t, low, high) for t, low, _avg, high in temperature],
            lines=[
                ("setpoint", GRAPH_REFERENCE, True,
                 [(t, avg) for t, _low, avg, _high in setpoint]),
                ("average", GRAPH_LINE, False,
                 [(t, avg) for t, _low, avg, _high in temperature]),
            ],
        )
        self.graph_bottom.title, self.graph_bottom.unit = "Return humidity", "%"
        self.graph_bottom.show(
            band=[(t, low, high) for t, low, _avg, high in humidity],
            lines=[("average", GRAPH_LINE, False,
                    [(t, avg) for t, _low, avg, _high in humidity])],
        )

    def _graphs_tick(self) -> None:
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "Graphs":
                self.refresh_graphs()
        except tk.TclError:
            pass
        self.after(GRAPHS_REFRESH_MS, self._graphs_tick)

    # -------------------------------------------------------------- alarms

    @staticmethod
    def _span(seconds: float) -> str:
        """45s, 12m 30s, 3h 05m, 2d 4h."""
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds}s"
        minutes, seconds = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h {minutes:02d}m"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"

    @staticmethod
    def _parse_utc(text):
        if not text:
            return None
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def _build_alarms(self, parent) -> None:
        """Alarm history on the left, counts per device on the right."""
        controls = self._controls_row(parent)

        tk.Label(controls, text="Device", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(4, 4))
        names = (["All devices", self.ALL_AC, self.ALL_UPS]
                 + [device.name for device in self.monitor.config.devices])
        self.alarm_device = ttk.Combobox(controls, values=names, state="readonly",
                                         width=16)
        self.alarm_device.current(0)
        self.alarm_device.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Range", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.alarm_range = ttk.Combobox(controls, values=list(RANGES), state="readonly",
                                        width=14)
        self.alarm_range.current(2)          # last 30 days
        self.alarm_range.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Show", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.alarm_state = ttk.Combobox(controls, values=["All", "Active", "Cleared"],
                                        state="readonly", width=10)
        self.alarm_state.current(0)
        self.alarm_state.pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Type", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=(0, 4))
        self.alarm_kind = ttk.Combobox(
            controls, values=["All types"] + list(ALARM_KIND_LABELS.values()),
            state="readonly", width=14,
        )
        self.alarm_kind.current(0)
        self.alarm_kind.pack(side="left", padx=(0, 12))

        self.alarm_count = tk.Label(controls, text="", bg=BACKGROUND, fg=TEXT_DIM,
                                    font=("Helvetica", 11))
        self.alarm_count.pack(side="right", padx=8)

        for widget in (self.alarm_device, self.alarm_range, self.alarm_state,
                       self.alarm_kind):
            widget.bind("<<ComboboxSelected>>", lambda _event: self.refresh_alarms())

        body = tk.Frame(parent, bg=BACKGROUND)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        history = tk.Frame(body, bg=BACKGROUND)
        history.pack(side="left", fill="both", expand=True)
        columns = ("raised", "cleared", "duration", "device", "kind", "description")
        headings = ("Started", "Ended", "Duration", "Device", "Type", "Description")
        widths = (150, 150, 90, 80, 110, 420)
        self.alarm_table = ttk.Treeview(history, columns=columns, show="headings")
        for column, heading, width in zip(columns, headings, widths):
            self.alarm_table.heading(column, text=heading)
            self.alarm_table.column(column, width=width,
                                    anchor="e" if column == "duration" else "w",
                                    stretch=column == "description")
        self.alarm_table.tag_configure("active", foreground=ACCENT_ALARM)
        # Double-click a row: go to that device (a rack opens on the floor map).
        kinds_by_label = {label: kind for kind, label in ALARM_KIND_LABELS.items()}

        def open_row(_event):
            row = self.alarm_table.focus()
            values = self.alarm_table.item(row, "values") if row else ()
            if len(values) >= 5:
                self._jump_to(values[3], kinds_by_label.get(values[4], ""))
        self.alarm_table.bind("<Double-1>", open_row)
        scroll = ttk.Scrollbar(history, orient="vertical",
                               command=self.alarm_table.yview)
        self.alarm_table.configure(yscrollcommand=scroll.set)
        self.alarm_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        summary = tk.Frame(body, bg=BACKGROUND)
        summary.pack(side="right", fill="y", padx=(10, 0))
        tk.Label(summary, text="PER DEVICE", bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(0, 4))
        columns = ("device", "count", "total", "longest")
        headings = ("Device", "Alarms", "Total time", "Longest")
        widths = (80, 60, 90, 90)
        self.alarm_summary = ttk.Treeview(summary, columns=columns, show="headings",
                                          height=16)
        for column, heading, width in zip(columns, headings, widths):
            self.alarm_summary.heading(column, text=heading)
            self.alarm_summary.column(column, width=width,
                                      anchor="w" if column == "device" else "e",
                                      stretch=False)
        self.alarm_summary.pack(fill="y", expand=True)

        self.refresh_alarms()

    def refresh_alarms(self) -> None:
        choice = self.alarm_device.get()
        days = RANGES.get(self.alarm_range.get(), 30)
        state = self.alarm_state.get()
        kind_label = self.alarm_kind.get()

        rows = self.monitor.storage.alarm_history(days)
        if choice == self.ALL_AC or choice == self.ALL_UPS:
            wanted = set(self._selected_devices(choice))
            rows = [row for row in rows if row["device"] in wanted]
        elif choice != "All devices":
            rows = [row for row in rows if row["device"] == choice]
        if state == "Active":
            rows = [row for row in rows if not row["cleared_utc"]]
        elif state == "Cleared":
            rows = [row for row in rows if row["cleared_utc"]]
        if kind_label != "All types":
            rows = [row for row in rows
                    if ALARM_KIND_LABELS.get(row["kind"], row["kind"]) == kind_label]

        now = utc_now()
        per_device = {}
        self.alarm_table.delete(*self.alarm_table.get_children())
        for row in rows:
            raised = self._parse_utc(row["raised_utc"])
            cleared = self._parse_utc(row["cleared_utc"])
            seconds = ((cleared or now) - raised).total_seconds() if raised else 0
            self.alarm_table.insert("", "end", tags=() if cleared else ("active",),
                                    values=(
                local_text(row["raised_utc"]),
                local_text(row["cleared_utc"]) if cleared else "still active",
                self._span(seconds),
                row["device"],
                ALARM_KIND_LABELS.get(row["kind"], row["kind"]),
                row["description"],
            ))
            stats = per_device.setdefault(row["device"], [0, 0.0, 0.0])
            stats[0] += 1
            stats[1] += seconds
            stats[2] = max(stats[2], seconds)

        self.alarm_summary.delete(*self.alarm_summary.get_children())
        order = [device.name for device in self.monitor.config.devices]
        for device in sorted(per_device, key=lambda name: order.index(name)
                             if name in order else len(order)):
            count, total, longest = per_device[device]
            self.alarm_summary.insert("", "end", values=(
                device, count, self._span(total), self._span(longest),
            ))

        active = sum(1 for row in rows if not row["cleared_utc"])
        self.alarm_count.configure(
            text=f"{len(rows)} alarms, {active} active" if rows else "no alarms in this range"
        )

    def _alarms_tick(self) -> None:
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "Alarms":
                self.refresh_alarms()
        except tk.TclError:
            pass
        self.after(ALARMS_REFRESH_MS, self._alarms_tick)

    # ---------------------------------------------------------------- jobs

    JOB_COLUMNS = (
        ("device", "Device", 90, "w"),
        ("job", "Job", 80, "w"),
        ("interval", "Every", 70, "e"),
        ("last_run", "Last run", 150, "w"),
        ("duration", "Took", 90, "e"),
        ("result", "Result", 80, "w"),
        ("failures", "Fails in a row", 110, "e"),
        ("next_run", "Next in", 80, "e"),
        ("rate", "OK 24h", 80, "e"),
        ("avg", "Avg 24h", 90, "e"),
        ("max", "Max 24h", 90, "e"),
        ("error", "Last error", 360, "w"),
    )

    def _build_jobs(self, parent) -> None:
        """Every polling job, live, with its last 24 hours next to it."""
        controls = self._controls_row(parent)
        tk.Label(controls,
                 text="Each row is one polling job. Slow or failing old units "
                      "show up here before they stop answering.",
                 bg=BACKGROUND, fg=TEXT_DIM,
                 font=("Helvetica", CONTROL_SIZE)).pack(side="left", padx=4)
        self.jobs_count = tk.Label(controls, text="", bg=BACKGROUND, fg=TEXT_DIM,
                                   font=("Helvetica", 11))
        self.jobs_count.pack(side="right", padx=8)

        frame = tk.Frame(parent, bg=BACKGROUND)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        columns = [column for column, _heading, _width, _anchor in self.JOB_COLUMNS]
        self.jobs_table = ttk.Treeview(frame, columns=columns, show="headings")
        for column, heading, width, anchor in self.JOB_COLUMNS:
            self.jobs_table.heading(column, text=heading)
            self.jobs_table.column(column, width=width, anchor=anchor,
                                   stretch=column == "error")
        self.jobs_table.tag_configure("failed", foreground=ACCENT_ALARM)
        self.jobs_table.tag_configure("slow", foreground="#e0a030")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.jobs_table.yview)
        self.jobs_table.configure(yscrollcommand=scroll.set)
        self.jobs_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # One row per job, created once and updated in place, so the table does
        # not flicker or lose its scroll position every two seconds.
        self.job_rows = {}
        for job in self.monitor.jobs:
            self.job_rows[(job.device, job.name)] = self.jobs_table.insert(
                "", "end", values=[job.device, job.name] + [""] * (len(columns) - 2)
            )
        self._summary_cache = {}
        self._summary_loaded = None
        self.refresh_jobs()

    @staticmethod
    def _duration(milliseconds) -> str:
        """8 ms stays readable as 8 ms; 41000 ms reads as 41.0s."""
        if milliseconds is None:
            return "--"
        if milliseconds < 1000:
            return f"{milliseconds:.0f} ms"
        return f"{milliseconds / 1000:.1f}s"

    def _poll_summary(self) -> dict:
        """24-hour numbers per job, re-read from the database once a minute."""
        now = utc_now()
        if self._summary_loaded and (now - self._summary_loaded).total_seconds() < 60:
            return self._summary_cache
        self._summary_cache = {
            (row["device"], row["job"]): row
            for row in self.monitor.storage.poll_summary(hours=24)
        }
        self._summary_loaded = now
        return self._summary_cache

    def refresh_jobs(self) -> None:
        now = utc_now()
        summary = self._poll_summary()
        failing = 0
        for job in self.monitor.jobs:
            row_id = self.job_rows.get((job.device, job.name))
            if row_id is None:
                continue

            if job.last_run is None:
                last_run = "not yet"
                next_run = "starting"
            else:
                last_run = local_text(job.last_run.strftime("%Y-%m-%dT%H:%M:%SZ"))
                remaining = job.interval - (now - job.last_run).total_seconds()
                next_run = "running" if remaining <= 0 else f"{remaining:.0f}s"

            stats = summary.get((job.device, job.name))
            if stats and stats["attempts"]:
                rate = f"{100 * stats['successes'] / stats['attempts']:.0f}%"
                average = self._duration(stats["avg_ms"])
                longest = self._duration(stats["max_ms"])
            else:
                rate = average = longest = "--"

            if job.last_ok is None:
                result, tags = "", ()
            elif job.last_ok:
                result = "OK"
                # A poll that used more than half its interval is a warning sign.
                slow = job.last_duration_ms > job.interval * 500
                tags = ("slow",) if slow else ()
            else:
                result, tags = "FAILED", ("failed",)
                failing += 1

            self.jobs_table.item(row_id, tags=tags, values=[
                job.device,
                job.name,
                f"{job.interval:g}s",
                last_run,
                self._duration(job.last_duration_ms) if job.last_run else "--",
                result,
                self.monitor.alarms.failure_count(job.device),
                next_run,
                rate,
                average,
                longest,
                (job.last_error or "").split(": ", 1)[-1],
            ])
        self.jobs_count.configure(
            text=f"{len(self.monitor.jobs)} jobs, {failing} failing" if failing
            else f"{len(self.monitor.jobs)} jobs, all OK"
        )

    def _ups_tab_tick(self) -> None:
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "UPS":
                self.refresh_ups_tab()
        except tk.TclError:
            pass
        self.after(UPS_TAB_REFRESH_MS, self._ups_tab_tick)

    def _jobs_tick(self) -> None:
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "Jobs":
                self.refresh_jobs()
        except tk.TclError:
            pass
        self.after(JOBS_REFRESH_MS, self._jobs_tick)

    def _logs_tick(self) -> None:
        """Reload whichever log view is open, so it stays live."""
        try:
            if self.tabs.tab(self.tabs.select(), "text") == "Logs":
                view = self.log_tabs.tab(self.log_tabs.select(), "text")
                if view == "Unit events":
                    self.refresh_events()
                elif view == "Readings":
                    self.refresh_readings()
                else:
                    self.refresh_program_log()
        except tk.TclError:
            pass
        self.after(LOG_REFRESH_MS, self._logs_tick)

    # ------------------------------------------------------------- updates

    def _drain_queue(self) -> None:
        changed = False
        try:
            while True:
                self.monitor.updates.get_nowait()
                changed = True
        except queue.Empty:
            pass
        if changed:
            self.refresh()
        self.after(QUEUE_INTERVAL_MS, self._drain_queue)

    def _tick(self) -> None:
        self.refresh()
        self.after(TICK_INTERVAL_MS, self._tick)

    def _age_and_values(self, device: str):
        values = dict(self.monitor.values.get(device, {}))
        updated = values.pop("updated_utc", None)
        age = None if updated is None else (utc_now() - updated).total_seconds()
        return age, values

    def refresh(self) -> None:
        alarms = self.monitor.storage.active_alarms()
        by_device = {}
        for alarm in alarms:
            by_device.setdefault(alarm["device"], []).append(alarm)

        for unit in self.monitor.config.ac_units:
            age, values = self._age_and_values(unit.name)
            states = self.monitor.states.get(unit.name, {})
            self.cards[unit.name].show(
                ac_content(unit, values, states, by_device.get(unit.name, []), age)
            )
        for unit in self.monitor.config.ups_units:
            age, values = self._age_and_values(unit.name)
            self.cards[unit.name].show(
                ups_content(unit, values, by_device.get(unit.name, []), age)
            )
        for unit in self.monitor.config.generators:
            age, values = self._age_and_values(unit.name)
            self.cards[unit.name].show(
                generator_content(unit, values, by_device.get(unit.name, []), age)
            )
        self._show_bar(alarms)
        self._fill_info()
        self._fill_devices()

    def _bar_text(self, prefix: str, items: list, suffix: str = "") -> str:
        """The bar's text, shortened to the space the buttons leave; the Info
        button lists everything."""
        text = prefix + "   |   ".join(
            f"{alarm['device']} {alarm['description']}" for alarm in items[:3]
        )
        if len(items) > 3:
            text += f"   (+{len(items) - 3} more)"
        text += suffix
        width = max(self.bar.winfo_width() - 480, 200)
        max_chars = max(int(width / 13), 20)
        return text if len(text) <= max_chars else text[:max_chars - 3] + "..."

    def _show_bar(self, alarms: list) -> None:
        real = [alarm for alarm in alarms if not is_notice(alarm)]
        notices = [alarm for alarm in alarms if is_notice(alarm)]

        # Notices: one soft sound per new notice, never repeated.
        current = {alarm["id"] for alarm in notices}
        if current - self.notified:
            play_notice_sound()
        self.notified = (self.notified & current) | current

        if real:
            suffix = f"   (+{len(notices)} notice(s))" if notices else ""
            self.bar.configure(bg=BAR_ALARM)
            self.bar_text.configure(bg=BAR_ALARM, text=self._bar_text(
                f"{len(real)} active alarm(s):  ", real, suffix))
            self.silence_button.configure(state="normal")
            # Silence stops the sound only; the bar and the red cards stay.
            self.sound_due = any(alarm["id"] not in self.silenced for alarm in real)
            return

        self.silenced.clear()
        self.sound_due = False
        self.silence_button.configure(state="disabled")
        if notices:
            self.bar.configure(bg=BAR_NOTICE)
            self.bar_text.configure(bg=BAR_NOTICE, text=self._bar_text(
                f"{len(notices)} notice(s):  ", notices))
            return
        self.bar.configure(bg=BAR_OK)
        self.bar_text.configure(bg=BAR_OK, text="All normal")

    # ---------------------------------------------------------- info window

    def _show_info(self) -> None:
        """The Info button: everything the top bar can only partly show,
        every open alarm and notice and the PDUs in maintenance, in one list.
        Double-click a row to open its place, as in the Alarms tab."""
        if self.info_window is not None and self.info_window.winfo_exists():
            self.info_window.lift()
            self._fill_info()
            return
        window = tk.Toplevel(self, bg=BACKGROUND)
        window.title("dcmonitor: everything open now")
        window.geometry("1100x520")
        self.info_window = window
        self.info_summary = tk.Label(window, text="", bg=BACKGROUND, fg=TEXT,
                                     font=("Helvetica", 14, "bold"), anchor="w")
        self.info_summary.pack(fill="x", padx=14, pady=(12, 6))
        frame = tk.Frame(window, bg=BACKGROUND)
        frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        columns = ("type", "device", "what", "since")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, title, width, stretch in (("type", "Type", 120, False),
                                              ("device", "Device", 140, False),
                                              ("what", "What", 600, True),
                                              ("since", "Since", 190, False)):
            tree.heading(column, text=title, anchor="w")
            tree.column(column, width=width, stretch=stretch, anchor="w")
        tree.tag_configure("alarm", foreground=ACCENT_ALARM)
        tree.tag_configure("notice", foreground=ACCENT_NOTICE)
        tree.tag_configure("maintenance", foreground=ACCENT_IDLE)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.bind("<Double-1>", self._info_jump)
        self.info_tree, self.info_rows, self.info_shown = tree, {}, None
        self._fill_info()

    def _fill_info(self) -> None:
        if self.info_window is None or not self.info_window.winfo_exists():
            return
        alarms = self.monitor.storage.active_alarms()
        real = [alarm for alarm in alarms if not is_notice(alarm)]
        notices = [alarm for alarm in alarms if is_notice(alarm)]
        held = self.monitor.alarms.maintenance
        rows = []          # (tag, values, (device, kind))
        for label, tag, items in (("Alarm", "alarm", real), ("Notice", "notice", notices)):
            for alarm in items:
                rows.append((tag, (label, alarm["device"], alarm["description"],
                                   local_text(alarm["raised_utc"])),
                             (alarm["device"], alarm["kind"])))
        for entry in held.values():
            device = f"Rack {entry['rack']}" if entry.get("rack") else "PDUs"
            rows.append(("maintenance", ("Maintenance", device,
                                         f"{entry['name']}: in maintenance, no alarms from this PDU",
                                         local_text(entry["since_utc"])), (device, "")))
        # Rebuilt only when something changed, so the scroll position and the
        # selected row survive the panel's refresh every few seconds.
        if rows != getattr(self, "info_shown", None):
            self.info_shown = rows
            tree = self.info_tree
            tree.delete(*tree.get_children())
            self.info_rows = {}
            for tag, values, target in rows:
                self.info_rows[tree.insert("", "end", tags=(tag,), values=values)] = target
        parts = [f"{len(real)} alarm(s)", f"{len(notices)} notice(s)",
                 f"{len(held)} PDU(s) in maintenance"]
        self.info_summary.configure(text="   \u00b7   ".join(parts))

    def _info_jump(self, _event) -> None:
        row = self.info_tree.focus()
        if row in self.info_rows:
            device, kind = self.info_rows[row]
            self._jump_to(device, kind)
            self.lift()

    # -------------------------------------------------------- devices window

    def _show_devices(self, rack: int) -> None:
        """The rack popup's Devices button: the rack drawn as on its sheet in
        the cabinet list (Excel on the share): U numbers down the left, each
        device as a block over its U's, its PDU outlets beside it with each
        outlet's current where the PDU measures it, else the current of the
        breaker it is on. One window; another rack's button shows that rack."""
        self.devices_rack = rack
        if self.devices_window is not None and self.devices_window.winfo_exists():
            self.devices_window.lift()
            self.devices_shown = None
            self._fill_devices()
            return
        window = tk.Toplevel(self, bg=BACKGROUND)
        # As tall as the visible screen allows (not under the taskbar), so
        # the whole rack fits; what does not fit scrolls.
        top, height = visible_screen(window)
        window.geometry(f"1300x{max(500, min(1100, height))}+40+{top}")
        self.devices_window = window
        self.devices_title = tk.Label(window, text="", bg=BACKGROUND, fg=TEXT,
                                      font=("Helvetica", 14, "bold"), anchor="w")
        self.devices_title.pack(fill="x", padx=14, pady=(12, 0))
        self.devices_note = tk.Label(window, text="", bg=BACKGROUND, fg=TEXT_DETAIL,
                                     font=("Helvetica", 11), anchor="w", justify="left")
        self.devices_note.pack(fill="x", padx=14, pady=(2, 8))
        # The note wraps to the window's width.
        self.devices_note.bind("<Configure>", lambda event: self.devices_note.configure(
            wraplength=max(200, event.width - 4)))
        frame = tk.Frame(window, bg=BACKGROUND)
        frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        # Right: the rack's circuits (C1, C2 ... amps), as in the rack popup.
        circuits = tk.Canvas(frame, bg=BACKGROUND, highlightthickness=0, bd=0, width=300)
        circuits.pack(side="right", fill="y", padx=(14, 0))
        self.devices_circuits = circuits
        canvas = tk.Canvas(frame, bg=BACKGROUND, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind("<Configure>", lambda _event: self._draw_rack_sheet(force=True))
        # The wheel scrolls the rack wherever the pointer is in this window.
        # On Windows the wheel goes to the window with the keyboard focus,
        # often the main one, so it is caught for the whole program and
        # used here only when the pointer is over this window.
        # Tk 9 (the Mac) sends a Magic Mouse or trackpad as <TouchpadScroll>.
        if not getattr(self, "_devices_wheel_bound", False):
            self.bind_all("<MouseWheel>", self._devices_wheel, add="+")
            try:
                self.bind_all("<TouchpadScroll>", self._devices_touchpad, add="+")
            except tk.TclError:          # Tk 8.6: no such event
                pass
            self._devices_wheel_bound = True
        self.devices_canvas, self.devices_shown = canvas, None
        self._fill_devices()

    def _over_devices(self, event) -> bool:
        """The pointer is over the Devices window (whichever has the focus)."""
        window = self.devices_window
        if window is None or not window.winfo_exists():
            return False
        try:
            over = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):      # e.g. over a menu or another program
            return False
        return over is not None and over.winfo_toplevel() is window

    def _scroll_devices(self, pixels: float) -> None:
        """Scroll the rack drawing by pixels (positive = down)."""
        canvas = self.devices_canvas
        region = str(canvas.cget("scrollregion")).split()
        total = float(region[3]) if len(region) == 4 else 0
        if total > canvas.winfo_height() and pixels:
            canvas.yview_moveto(canvas.yview()[0] + pixels / total)

    def _devices_wheel(self, event):
        """A mouse wheel: about 40 px a notch (Windows 120 per notch; the
        Mac's Tk 9 gives smaller steps)."""
        if not self._over_devices(event):
            return None
        if event.delta:
            step = event.delta / 3 if abs(event.delta) >= 120 else event.delta * 40
            self._scroll_devices(-step)
        return "break"

    def _devices_touchpad(self, event):
        """Tk 9's <TouchpadScroll> (Mac trackpad, Magic Mouse): the delta
        packs x and y; y in the low 16 bits, signed (tk::PreciseScrollDeltas)."""
        if not self._over_devices(event):
            return None
        low = event.delta & 0xFFFF
        delta_y = low if low < 0x8000 else low - 0x10000
        self._scroll_devices(-delta_y)
        return "break"

    def _fill_devices(self) -> None:
        if self.devices_window is None or not self.devices_window.winfo_exists():
            return
        rack = self.devices_rack
        data = self.monitor.rack_devices(rack) or {"found": False, "devices": [],
                                                    "title": "", "error": "",
                                                    "file_time": None, "u_max": None}
        self.devices_window.title(f"dcmonitor: rack {rack} devices")
        title = f"Rack {rack}" + (f"   {data['title']}" if data["title"] else "")
        self.devices_title.configure(text=title)
        notes = []
        if data["file_time"]:
            saved = datetime.fromtimestamp(data["file_time"]).strftime("%d.%m.%Y %H:%M")
            notes.append(f"Cabinet list saved {saved}. Amps: the outlet's own current. "
                         "Circuit: the breaker the outlet is on and that breaker's current. "
                         "From the PDUs every 30 s.")
        if data["error"]:
            notes.append(f"Cabinet list not readable now ({data['error']}): "
                         "showing the last copy read.")
        if not data["found"]:
            notes.append("This rack has no sheet in the cabinet list.")
        self.devices_note.configure(text="\n".join(notes),
                                    fg=ACCENT_NOTICE if data["error"] else TEXT_DETAIL)
        # Drawn again only when something changed (amps every 30 s), keeping
        # the scroll position.
        if data != self.devices_shown:
            self.devices_shown = data
            self._draw_rack_sheet()

    def _draw_rack_sheet(self, force: bool = False) -> None:
        """The rack as on its Excel sheet, one line per outlet in its own
        columns for easy reading: U | device | PDU outlet | amps | circuit |
        total. A U row grows when its device has more outlets than fit."""
        canvas, data = self.devices_canvas, self.devices_shown
        if data is None:
            return
        top_fraction = canvas.yview()[0]
        canvas.delete("all")
        width = max(canvas.winfo_width(), 760)
        size, line_h, base_h, header_h = 13, 22, 24, 30
        fixed = (("u", 52, "U"), ("outlet", 130, "PDU outlet"), ("amps", 96, "Amps"),
                 ("circuit", 116, "Circuit"), ("total", 100, "Total"))
        device_w = max(220, width - sum(w for _k, w, _t in fixed))
        columns, x = {}, 0
        for key, w, title in (fixed[0], ("device", device_w, data["title"] or "Device"),
                              *fixed[1:]):
            columns[key] = (x, w, title)
            x += w
        width = x
        devices = data["devices"]
        units = [u for d in devices for u in (d["u_top"], d["u_bottom"]) if u is not None]
        u_max = max([data.get("u_max") or 0] + units) or 42

        # One line per outlet (and one for a PDU cell not understood): PDU1's
        # outlets first, then PDU2's, each in outlet order (another rack's
        # PDU last), with a small gap between the PDUs.
        def lines_of(device):
            lines = []
            feeds = sorted(device["feeds"], key=lambda f: (not f.get("same_rack", True),
                                                           f["rack"], f["number"], f["outlet"]))
            for feed in feeds:
                where = "" if feed.get("same_rack", True) else f"R{feed['rack']} "
                outlet = f"{where}PDU{feed['number']}/{feed['outlet']}"
                if feed["amps"] is not None:
                    # A PDU not answering keeps its last value, dimmed.
                    amps = f"{feed['amps']:.2f} A"
                    amps_colour = TEXT_DIM if feed["note"] else TEXT
                else:
                    amps, amps_colour = feed["note"] or "--", TEXT_DIM
                circuit = ""
                if feed["breaker"]:
                    circuit = feed["breaker"] + ("" if feed["breaker_amps"] is None
                                                 else f"  {feed['breaker_amps']:.1f} A")
                group = (feed["rack"], feed["number"])
                gap = GROUP_GAP if lines and lines[-1][5] != group else 0
                lines.append((outlet, amps, amps_colour, circuit, gap, group))
            if not device["understood"]:
                lines.append((device["pdu_text"], "not understood", ACCENT_NOTICE, "",
                              GROUP_GAP if lines else 0, None))
            return lines

        def lines_height(lines):
            return len(lines) * line_h + sum(line[4] for line in lines)

        def look(name):
            """The operator's own equipment (OWN_EQUIPMENT) grey and cable
            management darker; everything else the customers' red."""
            letters = re.sub(r"[^A-Z]", "", name.upper())
            if letters == "CABLEMANAGEMENT":
                return SHEET_CABLES, TEXT_DIM, False
            if letters in OWN_EQUIPMENT:
                return SHEET_OWN, TEXT, True
            return SHEET_DEVICE, TEXT, True

        # Each U's height: what its device's outlet lines need (at least
        # min_h), then the window's free height shared out, so the whole rack
        # fits; only a rack that cannot fit even so scrolls.
        min_h = 18
        need = {u: min_h for u in range(1, u_max + 1)}
        for device in devices:
            if device["u_top"] is None:
                continue
            span = range(device["u_bottom"], device["u_top"] + 1)
            lines = lines_of(device)
            per_u = (lines_height(lines) + 6) / len(span) if lines else min_h
            for u in span:
                need[u] = max(need[u], per_u)
        room = canvas.winfo_height() - header_h - 4 - sum(need.values())
        if room > 0:
            # Free height to every row, up to base_h, small rows first.
            for u in need:
                need[u] = max(need[u], min(base_h, need[u] + room / len(need)))
        height = need
        top_of, y = {}, header_h
        for u in range(u_max, 0, -1):
            top_of[u] = y
            y += height[u]
        bottom = y

        def cell(key, y1, y2, fill):
            x1, w, _title = columns[key]
            canvas.create_rectangle(x1, y1, x1 + w, y2, fill=fill, outline=SHEET_GRID)

        def text(key, y, value, colour, bold=False, anchor="w", font_size=size):
            x1, w, _title = columns[key]
            x = {"w": x1 + 8, "e": x1 + w - 8, "center": x1 + w / 2}[anchor]
            font = ("Helvetica", font_size, "bold") if bold else ("Helvetica", font_size)
            return canvas.create_text(x, y, text=value, anchor=anchor, fill=colour, font=font)

        for key, (x1, w, title) in columns.items():
            canvas.create_rectangle(x1, 0, x1 + w, header_h, fill=SHEET_HEADER,
                                    outline=SHEET_GRID)
            item = text(key, header_h / 2, title, TEXT, bold=True, anchor="center")
            if canvas.bbox(item)[2] - canvas.bbox(item)[0] > w - 8:
                canvas.itemconfigure(item, font=("Helvetica", 10, "bold"))
        for u in range(u_max, 0, -1):
            y1, y2 = top_of[u], top_of[u] + height[u]
            fill = SHEET_ROW if u % 2 else SHEET_ROW_ALT
            cell("u", y1, y2, SHEET_U)
            text("u", (y1 + y2) / 2, str(u), TEXT, bold=True, anchor="center", font_size=11)
            for key in ("device", "outlet", "amps", "circuit", "total"):
                cell(key, y1, y2, fill)

        below = bottom + 12
        partial = False
        for device in devices:
            if device["u_top"] is None:
                need = max(base_h, lines_height(lines_of(device)) + 8)
                y1, y2 = below, below + need
                below += need + 4
            else:
                y1 = top_of[device["u_top"]]
                y2 = top_of[device["u_bottom"]] + height[device["u_bottom"]]
            fill, name_colour, bold = look(device["name"])
            cell("device", y1, y2, fill)
            x1, w, _title = columns["device"]
            weight = ("bold",) if bold else ()
            name = canvas.create_text(x1 + w / 2, (y1 + y2) / 2, text=device["name"],
                                      width=w - 14, justify="center", fill=name_colour,
                                      font=("Helvetica", size) + weight)
            if canvas.bbox(name)[3] - canvas.bbox(name)[1] > y2 - y1:
                canvas.itemconfigure(name, font=("Helvetica", 11) + weight)
            lines = lines_of(device)
            if not lines:
                continue
            for key in ("outlet", "amps", "circuit", "total"):
                cell(key, y1, y2, SHEET_PDU)
            y = (y1 + y2) / 2 - (lines_height(lines) - line_h) / 2
            for index, (outlet, amps, amps_colour, circuit, gap, _group) in enumerate(lines):
                if index:
                    y += line_h + gap
                if amps == "not understood":
                    # The cell as written, across the outlet columns.
                    x1 = columns["outlet"][0]
                    canvas.create_text(x1 + 8, y, anchor="w", text=f"{outlet}  (not understood)",
                                       fill=ACCENT_NOTICE, font=("Helvetica", 11))
                    continue
                text("outlet", y, outlet, TEXT)
                text("amps", y, amps, amps_colour, anchor="e",
                     font_size=size if amps_colour == TEXT else 11)
                text("circuit", y, circuit, TEXT_DETAIL)
            measured = [f["amps"] for f in device["feeds"] if f["amps"] is not None]
            if measured:
                total = f"{sum(measured):.2f} A"
                if len(measured) < len(device["feeds"]):
                    total += "*"
                    partial = True
                text("total", (y1 + y2) / 2, total, TEXT, bold=True, anchor="e")
        if partial:
            canvas.create_text(0, below + 8, anchor="w", fill=TEXT_DETAIL,
                               font=("Helvetica", 11),
                               text="* only the outlets whose PDU measures them")
            below += 26
        canvas.configure(scrollregion=(0, 0, width, below))
        if not force:
            canvas.yview_moveto(top_fraction)
        self._draw_rack_circuits(data.get("circuits", []))

    def _draw_rack_circuits(self, circuits: list) -> None:
        """The table right of the rack: each PDU's circuits with amps, limit
        and load, coloured by the breaker levels as on the map."""
        canvas = self.devices_circuits
        canvas.delete("all")
        rules = self.monitor.config.pdu_rules
        width = int(canvas.cget("width"))
        right = (None, width * 0.45, width * 0.72, width - 6)
        y = 0
        for x, w, label, anchor in ((0, width, "Circuits", "center"),):
            canvas.create_rectangle(x, y, x + w, y + 28, fill=SHEET_HEADER, outline=SHEET_GRID)
            canvas.create_text(x + w / 2, y + 14, text=label, fill=TEXT,
                               font=("Helvetica", 11, "bold"))
        y += 34
        if not circuits:
            canvas.create_text(6, y + 10, anchor="w", text="No PDU readings for this rack",
                               fill=TEXT_DIM, font=("Helvetica", 11))
            return
        for index, title in enumerate(("Circuit", "Amps", "Limit", "Load")):
            canvas.create_text(6 if index == 0 else right[index], y + 9, text=title,
                               anchor="w" if index == 0 else "e", fill=TEXT_DIM,
                               font=("Helvetica", 10))
        y += 22
        pdu = None
        for circuit in circuits:
            if circuit["pdu"] != pdu:
                pdu = circuit["pdu"]
                canvas.create_rectangle(0, y, width, y + 24, fill=SHEET_PDU, outline=SHEET_GRID)
                canvas.create_text(6, y + 12, anchor="w", text=pdu, fill=TEXT,
                                   font=("Helvetica", 11, "bold"))
                y += 26
            percent = circuit.get("percent")
            colour = TEXT
            for key, level_colour in (("breaker_alarm_pct", ACCENT_ALARM),
                                      ("breaker_warning_pct", ACCENT_NOTICE)):
                level = rules.get(key)
                if percent is not None and level is not None and percent >= level:
                    colour = level_colour
                    break
            limit = circuit.get("limit")
            if circuit.get("state") == "open":
                cells = (circuit["label"], "--", "--" if limit is None else f"{limit:g} A",
                         "TRIPPED")
                colours = (ACCENT_ALARM, TEXT_DIM, TEXT_DETAIL, ACCENT_ALARM)
            else:
                current = circuit.get("current")
                cells = (circuit["label"],
                         "--" if current is None else f"{current:.1f} A",
                         "--" if limit is None else f"{limit:g} A"
                         + ("*" if circuit.get("limit_from_config") else ""),
                         "--" if percent is None else f"{percent:.0f} %")
                colours = (TEXT, TEXT, TEXT_DETAIL, colour)
            for index, (cell, cell_colour) in enumerate(zip(cells, colours)):
                canvas.create_text(14 if index == 0 else right[index], y + 11, text=cell,
                                   anchor="w" if index == 0 else "e", fill=cell_colour,
                                   font=("Helvetica", 12, "bold") if index == 3 else ("Helvetica", 12))
            y += 24
        if any(c.get("limit_from_config") for c in circuits):
            canvas.create_text(6, y + 10, anchor="w", fill=TEXT_DETAIL,
                               font=("Helvetica", 10),
                               text="* limit from config: no rating given")

    def _sound_tick(self) -> None:
        if self.sound_due:
            play_alarm_sound()
        self.after(SOUND_INTERVAL_MS, self._sound_tick)

    # -------------------------------------------------------------- actions

    def _silence(self) -> None:
        """Stop the sound for the alarms standing right now.

        The alarms stay on screen until they actually clear. A new alarm has an
        id that is not in this set, so it sounds again.
        """
        self.silenced.update(
            alarm["id"] for alarm in self.monitor.storage.active_alarms()
        )
        self.sound_due = False
        # Stop the sound that is already playing, not only the next one.
        stop_alarm_sound()

    def _poll_all(self) -> None:
        threading.Thread(target=self.monitor.run_once, daemon=True).start()

    def _on_close(self) -> None:
        if messagebox.askokcancel(
            "Close dcmonitor",
            "Monitoring and alarms will stop. Close dcmonitor?",
        ):
            self.monitor.stop()
            self.destroy()


def run(monitor) -> None:
    monitor.start()
    panel = Panel(monitor)
    panel.mainloop()

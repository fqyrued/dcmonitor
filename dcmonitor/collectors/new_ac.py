"""Collector for AC-1 and AC-7..AC-10 (Vertiv iCOM).

Two unauthenticated endpoints on each unit's own IP, not the BMS IP:

    /anonymous/getvar.csv?name=...   live values, header name,id,desc,type,access,val
    /anonymous/alarms.cgi?action=getHistory   last 200 events as CSV

Requests longer than about 510 characters are dropped by the unit, so value
requests are kept short and chunked if ever extended.

Run directly to test one unit:
    python -m dcmonitor.collectors.new_ac <IP>
"""

import csv
import io
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Variable name on the unit -> metric name used in the database.
# These units control on supply air, so RetSetP_Act reads 0 and is not used.
VALUE_VARIABLES = {
    "RetPrb.Temp.Val": "return_temp_c",       # shown and alarmed on
    "SupPrb.Temp.Val": "supply_temp_c",
    "SupSetP_Act": "supply_setpoint_c",
    "RetPrb.Hum.Val": "return_humidity_pct",
}

# Flags read in the same request, turned into one readable unit status.
STATE_VARIABLES = {
    "Al_UnitOn.Active": "unit_on",
    "Al_Standby.Active": "standby",
    "Al_UnitOff.Active": "unit_off",
}


class CollectorError(Exception):
    """The unit did not answer, or answered with something unusable."""


@dataclass(frozen=True)
class UnitEvent:
    event_utc: str      # "YYYY-MM-DDTHH:MM:SSZ"
    event_id: int
    name: str
    action: str         # Start or Stop
    var1: str
    var2: str


def _get(url: str, timeout: float) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                raise CollectorError(f"HTTP {response.status}")
            return response.read().decode("latin-1")
    except urllib.error.HTTPError as error:
        raise CollectorError(f"HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise CollectorError(str(error)) from None


def build_value_url(ip: str, variables=None, max_query_chars: int = 480) -> str:
    """URL for a narrow getvar.csv request, refusing to exceed the length limit."""
    names = list(variables if variables is not None
                 else list(VALUE_VARIABLES) + list(STATE_VARIABLES))
    query = "&".join(f"name={name}" for name in names)
    if len(query) > max_query_chars:
        raise CollectorError(
            f"query is {len(query)} characters, limit is {max_query_chars}; "
            "split it into several requests"
        )
    return f"http://{ip}/anonymous/getvar.csv?{query}"


def parse_states(text: str) -> dict:
    """Turn the on/standby/off flags into one readable status."""
    flags = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        name = (row.get("name") or "").strip()
        if name in STATE_VARIABLES:
            flags[STATE_VARIABLES[name]] = (row.get("val") or "").strip() == "1"
    if not flags:
        return {}
    if flags.get("unit_on"):
        status = "Unit On"
    elif flags.get("standby"):
        status = "Standby"
    elif flags.get("unit_off"):
        status = "Unit Off"
    else:
        status = "Unknown"
    return {"unit_status": status}


def parse_values(text: str, variables=None) -> dict:
    """Turn a getvar.csv reply into {metric: float}."""
    wanted = dict(variables or VALUE_VARIABLES)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in reader.fieldnames:
        raise CollectorError("reply is not getvar.csv (no name column)")

    values = {}
    for row in reader:
        name = (row.get("name") or "").strip()
        if name not in wanted:
            continue
        raw = (row.get("val") or "").strip()
        try:
            values[wanted[name]] = float(raw)
        except ValueError:
            raise CollectorError(f"{name}: value {raw!r} is not a number") from None

    missing = set(wanted.values()) - set(values)
    if missing:
        raise CollectorError(f"missing from reply: {', '.join(sorted(missing))}")
    return values


def read_values(ip: str, timeout: float = 15.0, variables=None,
                max_query_chars: int = 480) -> tuple:
    """One getvar.csv request giving (values, states)."""
    url = build_value_url(ip, variables, max_query_chars)
    text = _get(url, timeout)
    return parse_values(text, variables), parse_states(text)


def _to_utc_text(raw: str, utc_offset_hours: float = 0.0) -> str:
    """Convert a unit timestamp such as 2026-09-17T11:59:43+00:00 to UTC.

    The +00:00 label is not trusted. At this site the units' clocks run on
    local time while still labelling it +00:00, so the wall-clock part is
    taken as local time and shifted by the configured offset.
    """
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise CollectorError(f"unreadable event time {raw!r}") from None
    wall_clock = moment.replace(tzinfo=None)
    utc = wall_clock - timedelta(hours=utc_offset_hours)
    return utc.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_events(text: str, utc_offset_hours: float = 0.0) -> list:
    """Turn the event log CSV into UnitEvent objects, newest first."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "TIME" not in reader.fieldnames:
        raise CollectorError("reply is not the event log (no TIME column)")

    events = []
    for row in reader:
        if not (row.get("TIME") or "").strip():
            continue
        raw_id = (row.get("ID") or "").strip()
        try:
            event_id = int(raw_id)
        except ValueError:
            event_id = -1
        events.append(
            UnitEvent(
                event_utc=_to_utc_text(row["TIME"], utc_offset_hours),
                event_id=event_id,
                name=(row.get("NAME") or "").strip(),
                action=(row.get("EVENT") or "").strip(),
                var1=(row.get("VAR1") or "").strip(),
                var2=(row.get("VAR2") or "").strip(),
            )
        )
    return events


def read_events(ip: str, timeout: float = 15.0, utc_offset_hours: float = 0.0) -> list:
    """Read the unit's event log. The unit keeps its last 200 entries."""
    url = f"http://{ip}/anonymous/alarms.cgi?action=getHistory"
    return parse_events(_get(url, timeout), utc_offset_hours)


def main() -> int:
    if len(sys.argv) not in (2, 4):
        print("Usage: python -m dcmonitor.collectors.new_ac <IP> [--offset HOURS]")
        return 2
    ip = sys.argv[1]
    # Same default as the program: the units' clocks run on UTC+3.
    offset = float(sys.argv[3]) if len(sys.argv) == 4 else 3.0

    print("values:")
    try:
        values, states = read_values(ip)
        for metric, value in values.items():
            print(f"  {metric:22} {value}")
        for name, state in states.items():
            print(f"  {name:22} {state}")
    except CollectorError as error:
        print(f"  ERROR: {error}")

    print("events (newest 10):")
    try:
        events = read_events(ip, utc_offset_hours=offset)
        print(f"  {len(events)} entries in the log")
        for event in events[:10]:
            print(f"  {event.event_utc}  id={event.event_id:<4} {event.action:<5} "
                  f"{event.name}")
    except CollectorError as error:
        print(f"  ERROR: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

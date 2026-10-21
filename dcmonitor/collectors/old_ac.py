"""Collector for AC-2..AC-6 (Emerson iCOM, Boa/0.93.15).

Two endpoints, both unauthenticated:

    /tmp/statusreport.xml   event log, ~51KB, slow to GET (~15s)
    /index.html             live values, ~3KB, Item ids 361, 356, 379

Change detection follows the ESP32: a HEAD request returns Last-Modified,
which only moves when a new event is written, so the 51KB body is fetched
only after it changes. Three of these units stall for a fixed ~30 s at
random, which is why the default timeout is 35 s.

Both formats below were confirmed against a real unit:

    <sren>
      <name>Event: Power On</name><eid>40</eid><uid>0</uid>
      <type>Message</type><time>2026-08-29T11:35:05</time>
    </sren>

    <Item id="361" name="LocTemp">
      <Label>Return Air Temperature</Label>
      <Value valueType="393216" offset="0.000" gain="1.000" precision="1">23.6</Value>
      <Unit>&#176;C</Unit>
    </Item>

An Item whose Value is "---" has no reading at the moment; it is reported as
unavailable rather than failing the whole request.

Run directly to test one unit:
    python -m dcmonitor.collectors.old_ac <IP>
    python -m dcmonitor.collectors.old_ac <IP> --dump
"""

import html
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Item id -> metric name used in the database (numbers).
VALUE_ITEMS = {
    "361": "return_temp_c",
    "356": "return_setpoint_c",
    "379": "return_humidity_pct",
}

# Item id -> state name (text such as "Unit On", "On", "Off").
# All of these come from the same index.html request, so reading them costs
# nothing extra. Only unit_status is shown on the card for now.
STATE_ITEMS = {
    "354": "unit_status",
    "1110": "fan",
    "1111": "cooling",
    "1112": "freecooling",
    "1113": "hot_water",
    "1114": "heater",
    "1115": "humidifier",
    "1116": "dehumidification",
    "1118": "maintenance",
}

EVENT_BLOCK = re.compile(r"<sren\b[^>]*>(.*?)</sren>", re.IGNORECASE | re.DOTALL)
VALUE_ELEMENT = re.compile(r"<Value\b[^>]*>(.*?)</Value>", re.IGNORECASE | re.DOTALL)
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

EVENT_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)


class CollectorError(Exception):
    """The unit did not answer, or answered with something unusable."""


@dataclass(frozen=True)
class UnitEvent:
    event_utc: str      # "YYYY-MM-DDTHH:MM:SSZ"
    name: str
    kind: str           # the unit's own type field: Alarm, Message, ...
    eid: str
    uid: str
    raw_time: str

    @property
    def is_alarm(self) -> bool:
        return self.kind.strip().lower() == "alarm"


def _child(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(1)).strip() if match else ""


def _request(url: str, method: str, timeout: float):
    request = urllib.request.Request(url, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise CollectorError(f"HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise CollectorError(str(error)) from None


def last_modified(ip: str, timeout: float = 35.0) -> str:
    """HEAD the event file and return its Last-Modified header.

    Cheap call used on the fast timer. The header only moves when the unit
    writes a new event, so the 51KB body is fetched only when it changes.
    """
    with _request(f"http://{ip}/tmp/statusreport.xml", "HEAD", timeout) as response:
        value = response.headers.get("Last-Modified")
    if not value:
        raise CollectorError("no Last-Modified header in the reply")
    return value


def fetch_events_xml(ip: str, timeout: float = 35.0) -> str:
    """GET the full event file. About 51KB and slow, so call it sparingly."""
    with _request(f"http://{ip}/tmp/statusreport.xml", "GET", timeout) as response:
        return response.read().decode("latin-1")


def _event_time(raw: str, utc_offset_hours: float = 0.0) -> str:
    """Convert the unit's clock to UTC.

    These units keep local time with no zone marker: at this site a log line
    reading 12:46 means 12:46 local time, which is 09:46 UTC. The offset
    comes from config, so it does not depend on the timezone of the machine
    running dcmonitor.
    """
    text = raw.strip()
    for pattern in EVENT_TIME_FORMATS:
        try:
            moment = datetime.strptime(text, pattern)
        except ValueError:
            continue
        moment = moment - timedelta(hours=utc_offset_hours)
        return moment.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise CollectorError(f"unreadable event time {raw!r}")


def parse_events(xml: str, utc_offset_hours: float = 0.0) -> list:
    """Pull the event blocks out of statusreport.xml, newest first."""
    events = []
    for block in EVENT_BLOCK.findall(xml):
        raw_time = _child(block, "time")
        if not raw_time:
            continue
        events.append(
            UnitEvent(
                event_utc=_event_time(raw_time, utc_offset_hours),
                name=_child(block, "name"),
                kind=_child(block, "type"),
                eid=_child(block, "eid"),
                uid=_child(block, "uid"),
                raw_time=raw_time,
            )
        )
    if not events and "<sren" in xml.lower():
        raise CollectorError("found sren blocks but no readable time field")
    return sorted(events, key=lambda event: event.event_utc, reverse=True)


def read_events(ip: str, timeout: float = 35.0, utc_offset_hours: float = 0.0) -> list:
    return parse_events(fetch_events_xml(ip, timeout), utc_offset_hours)


def fetch_index(ip: str, timeout: float = 35.0) -> str:
    with _request(f"http://{ip}/index.html", "GET", timeout) as response:
        return response.read().decode("latin-1")


def _item_text(text: str, item_id: str):
    """The Value text of one Item, or None if the Item is not in the page."""
    block = re.search(
        r"<Item\b[^>]*\bid\s*=\s*\"" + re.escape(item_id) + r"\"[^>]*>(.*?)</Item>",
        text, re.IGNORECASE | re.DOTALL,
    )
    if not block:
        return None
    value_element = VALUE_ELEMENT.search(block.group(1))
    return html.unescape(value_element.group(1)).strip() if value_element else ""


def parse_states(text: str, items=None) -> dict:
    """Read the text state items, skipping any that show no reading."""
    states = {}
    for item_id, name in (items or STATE_ITEMS).items():
        raw = _item_text(text, item_id)
        if raw and raw != "---":
            states[name] = raw
    return states


def parse_values(text: str, items=None) -> tuple:
    """Read the wanted Item ids out of index.html.

    Returns (values, unavailable):
        values       {metric: float} for items holding a number
        unavailable  metrics whose Item exists but shows no reading ("---")
    Raises CollectorError if an Item id is not in the page at all.
    """
    wanted = dict(items or VALUE_ITEMS)
    values, unavailable, absent = {}, [], []

    for item_id, metric in wanted.items():
        block = re.search(
            r"<Item\b[^>]*\bid\s*=\s*\"" + re.escape(item_id) + r"\"[^>]*>(.*?)</Item>",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not block:
            absent.append(f"id {item_id} ({metric})")
            continue
        value_element = VALUE_ELEMENT.search(block.group(1))
        raw = html.unescape(value_element.group(1)).strip() if value_element else ""
        number = NUMBER.search(raw)
        if number:
            values[metric] = float(number.group(0).replace(",", "."))
        else:
            unavailable.append(metric)

    if absent:
        raise CollectorError("not found in index.html: " + ", ".join(absent))
    return values, unavailable


def read_values(ip: str, timeout: float = 35.0, items=None) -> tuple:
    """One index.html request giving (values, states, unavailable)."""
    text = fetch_index(ip, timeout)
    values, unavailable = parse_values(text, items)
    return values, parse_states(text), unavailable


def _dump(ip: str) -> None:
    """Print raw material, for checking the parsers against a real unit."""
    print("--- HEAD /tmp/statusreport.xml")
    try:
        print(f"Last-Modified: {last_modified(ip)}")
    except CollectorError as error:
        print(f"ERROR: {error}")

    print("\n--- first 2 event blocks from /tmp/statusreport.xml")
    try:
        xml = fetch_events_xml(ip)
        print(f"({len(xml)} characters received)")
        for block in re.findall(r"<sren\b.*?</sren>", xml, re.IGNORECASE | re.DOTALL)[:2]:
            print(block)
    except CollectorError as error:
        print(f"ERROR: {error}")

    print("\n--- Item elements from /index.html")
    try:
        text = fetch_index(ip)
        print(f"({len(text)} characters received)")
        for item_id in VALUE_ITEMS:
            block = re.search(
                r"<Item\b[^>]*\bid\s*=\s*\"" + item_id + r"\"[^>]*>.*?</Item>",
                text, re.IGNORECASE | re.DOTALL,
            )
            print(block.group(0) if block else f"id {item_id}: NOT FOUND")
    except CollectorError as error:
        print(f"ERROR: {error}")


def list_items(ip: str, timeout: float = 35.0) -> list:
    """Every Item on index.html as (id, label, value), for finding fields."""
    text = fetch_index(ip, timeout)
    found = []
    for block in re.findall(r"<Item\b[^>]*>.*?</Item>", text, re.IGNORECASE | re.DOTALL):
        item_id = re.search(r'\bid\s*=\s*"([^"]*)"', block)
        label = re.search(r"<Label\b[^>]*>(.*?)</Label>", block, re.IGNORECASE | re.DOTALL)
        value = VALUE_ELEMENT.search(block)
        found.append((
            item_id.group(1) if item_id else "?",
            html.unescape(label.group(1)).strip() if label else "",
            html.unescape(value.group(1)).strip() if value else "",
        ))
    return found


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m dcmonitor.collectors.old_ac <IP> [--dump|--items] "
              "[--offset HOURS]")
        return 2
    ip = sys.argv[1]
    # Same default as the program: the units' clocks run on UTC+3.
    offset = 3.0
    if "--offset" in sys.argv[2:]:
        offset = float(sys.argv[sys.argv.index("--offset") + 1])

    if "--dump" in sys.argv[2:]:
        _dump(ip)
        return 0

    if "--items" in sys.argv[2:]:
        try:
            for item_id, label, value in list_items(ip):
                print(f"  id {item_id:<6} {label:<40} {value}")
        except CollectorError as error:
            print(f"ERROR: {error}")
        return 0

    print("values:")
    try:
        values, states, unavailable = read_values(ip)
        for metric, value in values.items():
            print(f"  {metric:22} {value}")
        for metric in unavailable:
            print(f"  {metric:22} no reading on the unit")
        for name, state in states.items():
            print(f"  {name:22} {state}")
    except CollectorError as error:
        print(f"  ERROR: {error}")

    print("events (newest 5):")
    try:
        events = read_events(ip, utc_offset_hours=offset)
        alarms = sum(1 for event in events if event.is_alarm)
        print(f"  {len(events)} entries in the file, {alarms} of type Alarm")
        for event in events[:5]:
            print(f"  {event.event_utc}  {event.kind:<10} {event.name}")
    except CollectorError as error:
        print(f"  ERROR: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

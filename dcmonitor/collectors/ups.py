"""Collector for UPS 1..5 (Riello with NetMan 204/208 cards).

Standard UPS MIB over SNMP v1, community from config. All values come back in
a single GET, which the NetMan agent accepts and answers in about 8 ms.

The alarm table (.1.3.6.1.2.1.33.1.6.2) only holds rows while an alarm is
active, so it cannot be read with GET. upsAlarmsPresent gives the count, and
the count going above zero is what the program reacts to. Reading the table
itself needs GETNEXT, which snmp_v1 does not support yet.

Run directly to test one UPS:
    python -m dcmonitor.collectors.ups <IP> [community]
"""

import sys

try:
    from ..snmp_v1 import SnmpError, SnmpTimeout, snmp_get, snmp_walk
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, SnmpTimeout, snmp_get, snmp_walk

OID_NAME = "1.3.6.1.2.1.33.1.1.5.0"
OID_BATTERY_CHARGE = "1.3.6.1.2.1.33.1.2.4.0"
OID_BATTERY_TEMP = "1.3.6.1.2.1.33.1.2.7.0"
OID_OUTPUT_SOURCE = "1.3.6.1.2.1.33.1.4.1.0"
OID_ALARMS_PRESENT = "1.3.6.1.2.1.33.1.6.1.0"
OID_LOAD = {
    1: "1.3.6.1.2.1.33.1.4.4.1.5.1",
    2: "1.3.6.1.2.1.33.1.4.4.1.5.2",
    3: "1.3.6.1.2.1.33.1.4.4.1.5.3",
}

# Output table and frequency (RFC 1628 upsOutput group). Checked against the
# NetMan web page's OUTPUT table: 230/230/229 V, 486 and 717 for 48.6 A and
# 71.7 A, 500 for 50.0 Hz. Current and frequency come in tenths.
OID_FREQUENCY = "1.3.6.1.2.1.33.1.4.2.0"
OID_VOLTAGE = {phase: f"1.3.6.1.2.1.33.1.4.4.1.2.{phase}" for phase in (1, 2, 3)}
OID_CURRENT = {phase: f"1.3.6.1.2.1.33.1.4.4.1.3.{phase}" for phase in (1, 2, 3)}

# Input voltage per phase (RFC 1628 upsInputTable, upsInputVoltage, RMS V).
# Checked against the NetMan INPUT page before use; see CLAUDE.md 6.3.
OID_INPUT_VOLTAGE = {phase: f"1.3.6.1.2.1.33.1.3.3.1.3.{phase}" for phase in (1, 2, 3)}

# upsEstimatedMinutesRemaining: 81 on UPS 5 = autonomy 01:21 on the NetMan.
# In the fast read, since it drops quickly on battery.
OID_MINUTES_REMAINING = "1.3.6.1.2.1.33.1.2.3.0"

# upsOutputSource values from the standard UPS MIB.
OUTPUT_SOURCE = {
    1: "other",
    2: "none",
    3: "normal",
    4: "bypass",
    5: "battery",
    6: "booster",
    7: "reducer",
}

ALL_OIDS = [
    OID_NAME,
    OID_BATTERY_CHARGE,
    OID_BATTERY_TEMP,
    OID_OUTPUT_SOURCE,
    OID_ALARMS_PRESENT,
    *OID_LOAD.values(),
]

# Read in the same GET, but optional: an agent without one answers
# noSuchName naming it, and the UPS is read again without it. The OIDs an
# agent lacks are remembered per UPS, so this costs one extra request once.
EXTRA_OIDS = [OID_FREQUENCY, *OID_VOLTAGE.values(), *OID_CURRENT.values(),
              *OID_INPUT_VOLTAGE.values(), OID_MINUTES_REMAINING]
_missing = {}     # (ip, kind) -> extra OIDs that agent answered noSuchName for


# upsAlarmDescr column of the alarm table: one row per active alarm, whose
# value is the OID of the alarm type.
OID_ALARM_DESCR = "1.3.6.1.2.1.33.1.6.2.1.2"
WELL_KNOWN_PREFIX = "1.3.6.1.2.1.33.1.6.3."

# upsWellKnownAlarms from RFC 1628, numbered as in the RFC.
WELL_KNOWN_ALARMS = {
    1: "Battery bad: replacement needed",
    2: "On battery",
    3: "Low battery: remaining run time at or below the limit",
    4: "Depleted battery: cannot carry the load on a mains failure",
    5: "Temperature out of tolerance",
    6: "Input out of tolerance",
    7: "Output out of tolerance",
    8: "Output overload",
    9: "On bypass",
    10: "Bypass out of tolerance",
    11: "Output off as requested",
    12: "UPS off as requested",
    13: "Charger failed",
    14: "UPS output off",
    15: "UPS system off",
    16: "Fan failure",
    17: "Fuse failure",
    18: "General fault",
    19: "Diagnostic test failed",
    20: "Communications lost between agent and UPS",
    21: "Awaiting power: output off until mains returns",
    22: "Shutdown pending",
    23: "Shutdown imminent",
    24: "Test in progress",
}

# Already covered as real alarms from the output source, so not repeated.
COVERED_ELSEWHERE = {2, 9}


class CollectorError(Exception):
    """The UPS did not answer, or answered with something unusable."""


def _number(values: dict, oid: str, label: str) -> int:
    value = values.get(oid)
    if not isinstance(value, int):
        raise CollectorError(f"{label}: expected a number, got {value!r}")
    return value


def _extra_values(values: dict) -> dict:
    """Output voltage, current and frequency, input voltage and battery
    minutes remaining as metrics; missing ones skipped."""
    metrics = {}
    minutes = values.get(OID_MINUTES_REMAINING)
    if isinstance(minutes, int):
        metrics["battery_minutes_remaining"] = minutes
    frequency = values.get(OID_FREQUENCY)
    if isinstance(frequency, int):
        metrics["output_frequency_hz"] = frequency / 10.0
    for phase in (1, 2, 3):
        voltage = values.get(OID_VOLTAGE[phase])
        if isinstance(voltage, int):
            metrics[f"output_voltage_v_l{phase}"] = voltage
        current = values.get(OID_CURRENT[phase])
        if isinstance(current, int):
            metrics[f"output_current_a_l{phase}"] = current / 10.0
        voltage = values.get(OID_INPUT_VOLTAGE[phase])
        if isinstance(voltage, int):
            metrics[f"input_voltage_v_l{phase}"] = voltage
    return metrics


def _get(ip: str, community: str, timeout: float, retries: int,
         core: list = ALL_OIDS, extras: list = EXTRA_OIDS, kind: str = "values") -> dict:
    """One GET of the core and extra OIDs, dropping extras the agent lacks.

    With no core OIDs (the details read) and every extra missing, returns {}.
    """
    missing = _missing.setdefault((ip, kind), set())
    while True:
        oids = core + [oid for oid in extras if oid not in missing]
        if not oids:
            return {}
        try:
            return snmp_get(ip, oids, community=community,
                            timeout=timeout, retries=retries)
        except SnmpTimeout:
            raise
        except SnmpError as error:
            text = str(error)
            if "noSuchName" not in text or len(oids) == len(core):
                raise
            named = [oid for oid in oids if text.endswith(f" on {oid}")]
            if named and named[0] in extras:
                missing.add(named[0])
            elif named:
                raise              # a core OID is missing: a real fault
            else:
                missing.update(extras)   # not told which: drop them all


def read(ip: str, community: str = "public", timeout: float = 2.0,
         retries: int = 2) -> dict:
    """Read one UPS. Returns metrics, output source and alarm count."""
    try:
        values = _get(ip, community, timeout, retries)
    except SnmpError as error:
        raise CollectorError(str(error)) from None

    name = values.get(OID_NAME)
    if isinstance(name, bytes):
        name = name.decode("latin-1").strip()

    source_code = _number(values, OID_OUTPUT_SOURCE, "output source")

    result = {
        "reported_name": name,
        "metrics": {
            "battery_charge_pct": _number(values, OID_BATTERY_CHARGE, "battery charge"),
            "battery_temp_c": _number(values, OID_BATTERY_TEMP, "UPS temperature"),
        },
        "output_source_code": source_code,
        "output_source": OUTPUT_SOURCE.get(source_code, f"unknown ({source_code})"),
        "on_battery": source_code == 5,
        "on_bypass": source_code == 4,
        "alarms_present": _number(values, OID_ALARMS_PRESENT, "alarms present"),
    }
    for phase, oid in OID_LOAD.items():
        result["metrics"][f"output_load_pct_l{phase}"] = _number(
            values, oid, f"output load phase {phase}"
        )
    result["metrics"].update(_extra_values(values))
    return result


def read_alarms(ip: str, community: str = "public", timeout: float = 2.0,
                retries: int = 2) -> list:
    """The UPS's active alarms from its alarm table: [(code, description)].

    code is the well-known alarm number from RFC 1628, or the full OID for a
    manufacturer-specific alarm.
    """
    try:
        rows = snmp_walk(ip, OID_ALARM_DESCR, community=community,
                         timeout=timeout, retries=retries)
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    alarms = []
    for value in rows.values():
        oid = str(value or "")
        if oid.startswith(WELL_KNOWN_PREFIX):
            try:
                number = int(oid[len(WELL_KNOWN_PREFIX):].split(".")[0])
            except ValueError:
                number = None
            if number is not None:
                alarms.append((number, WELL_KNOWN_ALARMS.get(number, f"Alarm type {number}")))
                continue
        alarms.append((oid, f"Manufacturer alarm {oid}"))
    return alarms


# ------------------------------------------------------------------ details
# Everything else the NetMan answers with a real value, for the UPS tab. Read
# on a slower job. Checked on UPS 5 (30 Sep 2026) against the NetMan DATA and
# SYSTEM OVERVIEW pages: battery voltage 5100 = 510.0 V, bypass 226/226/227 V and 500 = 50.0 Hz, input current
# and frequency in tenths. Battery and bypass current read 0 (tenths per the
# RFC). The test (.7) and config (.9) groups hold only zeros on the NetMan
# and are not read.
DETAIL_TEXT = {
    "1.3.6.1.2.1.1.1.0": "agent",                  # sysDescr, "Netman 204"
    "1.3.6.1.2.1.33.1.1.1.0": "manufacturer",
    "1.3.6.1.2.1.33.1.1.2.0": "model",
    "1.3.6.1.2.1.33.1.1.3.0": "ups_software",
    "1.3.6.1.2.1.33.1.1.4.0": "agent_software",
    "1.3.6.1.2.1.33.1.1.5.0": "ups_name",
}
OID_UPTIME = "1.3.6.1.2.1.1.3.0"                   # sysUpTime, hundredths of s
OID_BATTERY_STATUS = "1.3.6.1.2.1.33.1.2.1.0"
OID_SHUTDOWN_DELAY = "1.3.6.1.2.1.33.1.8.2.0"      # -1 = no shutdown counting down
# metric name -> (OID, divisor)
DETAIL_NUMBERS = {
    "seconds_on_battery": ("1.3.6.1.2.1.33.1.2.2.0", 1),
    "battery_voltage_v": ("1.3.6.1.2.1.33.1.2.5.0", 10),
    "battery_current_a": ("1.3.6.1.2.1.33.1.2.6.0", 10),
    "input_line_bads": ("1.3.6.1.2.1.33.1.3.1.0", 1),
    "bypass_frequency_hz": ("1.3.6.1.2.1.33.1.5.1.0", 10),
}
for _phase in (1, 2, 3):
    DETAIL_NUMBERS[f"input_frequency_hz_l{_phase}"] = (f"1.3.6.1.2.1.33.1.3.3.1.2.{_phase}", 10)
    DETAIL_NUMBERS[f"input_current_a_l{_phase}"] = (f"1.3.6.1.2.1.33.1.3.3.1.4.{_phase}", 10)
    DETAIL_NUMBERS[f"bypass_voltage_v_l{_phase}"] = (f"1.3.6.1.2.1.33.1.5.3.1.2.{_phase}", 1)
    DETAIL_NUMBERS[f"bypass_current_a_l{_phase}"] = (f"1.3.6.1.2.1.33.1.5.3.1.3.{_phase}", 10)
DETAIL_OIDS = [*DETAIL_TEXT, OID_UPTIME, OID_BATTERY_STATUS, OID_SHUTDOWN_DELAY,
               *(oid for oid, _divisor in DETAIL_NUMBERS.values())]

# upsBatteryStatus from RFC 1628.
BATTERY_STATUS = {1: "unknown", 2: "normal", 3: "low", 4: "depleted"}


def read_details(ip: str, community: str = "public", timeout: float = 2.0,
                 retries: int = 2) -> dict:
    """The UPS tab's slower values: {"metrics": {...}, "states": {...}}.

    metrics are numbers and go into the history like any reading; states are
    text (names, versions, statuses) and are only kept in memory.
    """
    try:
        values = _get(ip, community, timeout, retries,
                      core=[], extras=DETAIL_OIDS, kind="details")
    except SnmpError as error:
        raise CollectorError(str(error)) from None

    states = {}
    for oid, key in DETAIL_TEXT.items():
        value = values.get(oid)
        if isinstance(value, bytes):
            value = value.decode("latin-1").strip()
            if value:
                states[key] = value
    uptime = values.get(OID_UPTIME)
    if isinstance(uptime, int):
        states["agent_uptime_s"] = uptime // 100
    status = values.get(OID_BATTERY_STATUS)
    if isinstance(status, int):
        states["battery_status"] = BATTERY_STATUS.get(status, f"code {status}")
    delay = values.get(OID_SHUTDOWN_DELAY)
    if isinstance(delay, int):
        states["shutdown_countdown"] = "none" if delay < 0 else f"{delay} s"

    metrics = {}
    for metric, (oid, divisor) in DETAIL_NUMBERS.items():
        value = values.get(oid)
        if isinstance(value, int):
            metrics[metric] = value / divisor if divisor != 1 else value
    return {"metrics": metrics, "states": states}


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m dcmonitor.collectors.ups <IP> [community]")
        return 2
    ip = sys.argv[1]
    community = sys.argv[2] if len(sys.argv) == 3 else "public"

    try:
        data = read(ip, community=community)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1

    print(f"  name            {data['reported_name']}")
    print(f"  output source   {data['output_source']}")
    print(f"  alarms present  {data['alarms_present']}")
    for metric, value in data["metrics"].items():
        print(f"  {metric:22} {value}")
    try:
        alarms = read_alarms(ip, community=community)
    except CollectorError as error:
        print(f"  alarm table: ERROR {error}")
        return 0
    print(f"  alarm table     {len(alarms)} active")
    for code, text in alarms:
        print(f"    {code}: {text}")
    try:
        details = read_details(ip, community=community)
    except CollectorError as error:
        print(f"  details: ERROR {error}")
        return 0
    for key, value in details["states"].items():
        print(f"  {key:22} {value}")
    for metric, value in details["metrics"].items():
        print(f"  {metric:22} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Load and validate config.toml.

Every site-specific value lives in config.toml. This module reads it with the
standard library tomllib, checks that required values exist and are sane, and
returns plain objects the rest of the program uses. A bad config fails here,
at startup, with a message naming the exact key.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER = "<IP>"
VALID_GENERATIONS = ("old", "new")


class ConfigError(Exception):
    """config.toml is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class AcUnit:
    name: str
    generation: str   # "old" or "new"
    ip: str

    @property
    def is_new(self) -> bool:
        return self.generation == "new"


@dataclass(frozen=True)
class UpsUnit:
    name: str
    ip: str


@dataclass(frozen=True)
class GeneratorUnit:
    name: str
    ip: str
    unit: int              # Modbus unit ID, as used by DSE Scada Suite


@dataclass(frozen=True)
class Config:
    path: Path
    folder: Path
    database: Path
    log_folder: Path
    utc_offset_hours: float
    polling: dict
    retention: dict
    thresholds: dict
    informational_ids: frozenset
    snmp_community: str
    ac_units: tuple
    ups_units: tuple
    devices: tuple = field(default=())
    old_ac_utc_offset_hours: float = 3.0
    new_ac_utc_offset_hours: float = 3.0
    site_map: Path | None = None
    generators: tuple = ()
    pdu_sensors: dict | None = None      # directly read PDU probes, if listed
    pdus: dict | None = None             # PDUs read directly over SNMP ([pdus])
    pdu_rules: dict = field(default_factory=dict)   # breaker levels, follower, rack alarm
    cabinet_list: dict | None = None     # the Excel list of devices per rack


REQUIRED_POLLING = (
    "old_ac_event_interval_s",
    "old_ac_values_interval_s",
    "old_ac_timeout_s",
    "new_ac_values_interval_s",
    "new_ac_events_interval_s",
    "new_ac_timeout_s",
    "new_ac_max_query_chars",
    "ups_interval_s",
    "ups_timeout_s",
    "ups_retries",
    "unreachable_after_failures",
)

REQUIRED_RETENTION = (
    "readings_days",
    "alarms_days",
    "unit_events_days",
    "poll_log_days",
)

REQUIRED_THRESHOLDS = (
    "ac_alarm_above_setpoint_c",
    "ac_clear_above_setpoint_c",
    "ups_battery_temp_alarm_c",
    "ups_battery_charge_alarm_below_pct",
)


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] section")
    return value


def _numbers(section: dict, keys: tuple, section_name: str) -> dict:
    out = {}
    for key in keys:
        value = section.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"[{section_name}] {key} must be a number")
        if value <= 0:
            raise ConfigError(f"[{section_name}] {key} must be greater than 0")
        out[key] = value
    return out


def _devices(data: dict, key: str, label: str) -> list:
    entries = data.get(key)
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"no [[{key}]] entries found")
    seen = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[{key}]] entry {index} is not a table")
        name = entry.get("name")
        ip = entry.get("ip")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[{key}]] entry {index} has no name")
        if not isinstance(ip, str) or not ip.strip():
            raise ConfigError(f"{label} {name}: no ip")
        if ip.strip() == PLACEHOLDER:
            raise ConfigError(f"{label} {name}: ip is still the {PLACEHOLDER} placeholder")
        if name in seen:
            raise ConfigError(f"{label} {name}: duplicate name")
        seen.add(name)
    return entries


# Rules for the PDU readings. In [pdus] since 2 Oct 2026; they were in
# [poweriq].
PDU_RULE_KEYS = ("default_breaker_limit_a", "breaker_alarm_pct", "breaker_warning_pct",
                 "breaker_highlight_pct", "follow_drop_min_a", "follow_change_pct",
                 "follow_change_min_a", "follow_normal_days", "follow_stale_after_s",
                 "follow_readings", "rack_temp_alarm_c")


def _pdu_rules(data: dict) -> dict:
    """Breaker levels, the circuit follower and the rack temperature alarm,
    from [pdus]. Absent keys (or no [pdus]) take the defaults below."""
    section = data.get("pdus") or {}
    if not isinstance(section, dict):
        raise ConfigError("[pdus] must be a table")
    result = {}
    # Limit for circuits whose PDU gives no rating (Geist breakers).
    default_limit = section.get("default_breaker_limit_a")
    if default_limit in (None, ""):
        result["default_breaker_limit_a"] = None
    elif isinstance(default_limit, (int, float)) and not isinstance(default_limit, bool) \
            and default_limit > 0:
        result["default_breaker_limit_a"] = float(default_limit)
    else:
        raise ConfigError("[pdus] default_breaker_limit_a must be amps, or empty")

    # Breaker alarm: percent of each breaker's limit. Off until a number is set.
    breaker = section.get("breaker_alarm_pct")
    if breaker in (None, ""):
        result["breaker_alarm_pct"] = None
    elif isinstance(breaker, (int, float)) and not isinstance(breaker, bool) and 0 < breaker <= 100:
        result["breaker_alarm_pct"] = float(breaker)
    else:
        raise ConfigError("[pdus] breaker_alarm_pct must be a percent from 1 to 100, or empty")

    # Two lower breaker levels. Absent means the defaults below, so older
    # config files need no change; "" switches a level off.
    #   breaker_warning_pct    amber notice, one soft sound (default 80)
    #   breaker_highlight_pct  colour on the floor map only, no sound (default 45)
    for key, default in (("breaker_warning_pct", 80.0), ("breaker_highlight_pct", 45.0)):
        value = section.get(key, default)
        if value == "":
            result[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 100:
            result[key] = float(value)
        else:
            raise ConfigError(f"[pdus] {key} must be a percent from 1 to 100, or empty")

    # The "consumes more / less" notices were removed (5 Oct 2026);
    # a line still setting them would do nothing, so it stops the start.
    gone = [key for key in ("follow_change_pct", "follow_change_min_a") if key in section]
    if gone:
        raise ConfigError(f"[pdus] {', '.join(gone)}: no longer used (the consumption "
                          "notices were removed); delete the line(s)")

    # Circuit follower (dcmonitor/follower.py). Absent means the default,
    # "" switches the drop alarm and the no-reading notice off.
    for key, default in (("follow_drop_min_a", 0.1),):
        value = section.get(key, default)
        if value == "":
            result[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            result[key] = float(value)
        else:
            raise ConfigError(f"[pdus] {key} must be a number greater than 0, or empty")
    days = section.get("follow_normal_days", 7)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 28:
        raise ConfigError("[pdus] follow_normal_days must be a whole number of days, 1 to 28")
    result["follow_normal_days"] = days
    stale = section.get("follow_stale_after_s", 10800)
    if not isinstance(stale, (int, float)) or isinstance(stale, bool) or stale < 600:
        raise ConfigError("[pdus] follow_stale_after_s must be seconds, 600 or more")
    result["follow_stale_after_s"] = float(stale)
    readings = section.get("follow_readings", 2)
    if not isinstance(readings, int) or isinstance(readings, bool) or not 1 <= readings <= 10:
        raise ConfigError("[pdus] follow_readings must be a whole number from 1 to 10")
    result["follow_readings"] = readings

    # The rack temperature alarm stays off until a number is set here.
    alarm = section.get("rack_temp_alarm_c")
    if alarm in (None, ""):
        result["rack_temp_alarm_c"] = None
    elif isinstance(alarm, (int, float)) and not isinstance(alarm, bool):
        result["rack_temp_alarm_c"] = float(alarm)
    else:
        raise ConfigError("[pdus] rack_temp_alarm_c must be a number or empty")
    return result


def _check_poweriq(data: dict) -> None:
    """dcmonitor no longer uses Power IQ (2 Oct 2026) and ignores [poweriq],
    which can be deleted. A rule line left there from before the move to
    [pdus] would be ignored without a word, so it stops the start."""
    section = data.get("poweriq")
    if not isinstance(section, dict):
        return
    moved = [key for key in PDU_RULE_KEYS if key in section]
    if moved:
        raise ConfigError(f"[poweriq] {', '.join(moved)}: moved to [pdus]; "
                          "move the line(s) there, value unchanged")


def _pdu_sensors(data: dict) -> dict | None:
    """Optional [pdu_sensors] settings and [[pdu_sensor]] list.

    Single probes on PDUs that are not in pdus.toml (not read directly).
    Each entry names the rack, the PDU's IP and the probe number; the read
    community is shared unless an entry sets its own.
    """
    entries = data.get("pdu_sensor", [])
    if not entries:
        return None
    section = data.get("pdu_sensors", {})
    if not isinstance(section, dict):
        raise ConfigError("[pdu_sensors] must be a table")
    community = section.get("community")

    settings = {}
    for key, default in (("interval_s", 300), ("timeout_s", 3)):
        value = section.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"[pdu_sensors] {key} must be a number greater than 0")
        settings[key] = float(value)

    parsed, seen = [], set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[pdu_sensor]] entry {index} is not a table")
        rack = entry.get("rack")
        if not isinstance(rack, int) or isinstance(rack, bool) or rack <= 0:
            raise ConfigError(f"[[pdu_sensor]] entry {index}: rack must be a rack number")
        ip = entry.get("ip")
        if not isinstance(ip, str) or not ip.strip() or ip.strip() == PLACEHOLDER:
            raise ConfigError(f"[[pdu_sensor]] rack {rack}: ip is missing or still a placeholder")
        # No probe and no serial: dcmonitor picks the available probe itself.
        probe = entry.get("probe")
        if probe is not None and (not isinstance(probe, int) or isinstance(probe, bool)
                                  or probe <= 0):
            raise ConfigError(f"[[pdu_sensor]] rack {rack}: probe must be 1 or more")
        serial = entry.get("serial")
        if serial is not None and (not isinstance(serial, str) or not serial.strip()):
            raise ConfigError(f"[[pdu_sensor]] rack {rack}: serial must be the probe's Device ID")
        own = entry.get("community", community)
        if not isinstance(own, str) or not own or ("<" in own and ">" in own):
            raise ConfigError(f"[[pdu_sensor]] rack {rack}: no read community set "
                              "(set [pdu_sensors] community, or community on this entry)")
        key = (ip.strip(), probe, (serial or "").strip().upper())
        if key in seen:
            raise ConfigError(f"[[pdu_sensor]] rack {rack}: that IP and probe are listed twice")
        seen.add(key)
        parsed.append({"rack": rack, "ip": ip.strip(), "probe": probe,
                       "serial": serial.strip() if serial else None, "community": own})
    settings["entries"] = tuple(parsed)
    return settings


PDU_FAMILIES = ("raritan_px2", "raritan_px1", "geist", "apc", "unknown")


def _pdus(data: dict, folder: Path, pdu_sensors: dict | None) -> dict | None:
    """Optional [pdus] section: PDUs read directly over SNMP, listed in
    pdus.toml (kept by hand; first written from Power IQ). Absent = off.

    The read community is [pdus] community, or else the one the
    [[pdu_sensor]] entries use (one community for all PDUs on site). A PDU
    set up differently has its own community = "..." line in pdus.toml.
    """
    section = data.get("pdus")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("[pdus] must be a table")
    settings = {}
    for key, default in (("interval_s", 30), ("timeout_s", 3)):
        value = section.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"[pdus] {key} must be a number greater than 0")
        settings[key] = float(value)
    # A PDU that does not answer: alarm after this many failed reads in a
    # row. 3 = 1.5 min at 30 s (9 Oct 2026; was 1, the first, from
    # 2 Oct).
    failures = section.get("alarm_after_failures", 3)
    if not isinstance(failures, int) or isinstance(failures, bool) or not 1 <= failures <= 10:
        raise ConfigError("[pdus] alarm_after_failures must be a whole number from 1 to 10")
    settings["alarm_after_failures"] = failures

    community = section.get("community")
    if community is None and pdu_sensors:
        used = [entry["community"] for entry in pdu_sensors["entries"]]
        community = max(set(used), key=used.count) if used else None
    if not isinstance(community, str) or not community or ("<" in community and ">" in community):
        raise ConfigError("[pdus] community is not set (and no [[pdu_sensor]] community to use)")
    settings["community"] = community

    name = section.get("file", "pdus.toml")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("[pdus] file must be a file name")
    path = (folder / name).resolve()
    if not path.is_file():
        raise ConfigError(f"[pdus] file not found: {path} (the PDU list, next to config.toml)")
    try:
        with path.open("rb") as handle:
            listed = tomllib.load(handle).get("pdu", [])
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None

    entries, seen = [], set()
    for index, entry in enumerate(listed, start=1):
        pdu_name = entry.get("name") if isinstance(entry, dict) else None
        where = f"{path.name}: [[pdu]] {pdu_name or index}"
        if not isinstance(pdu_name, str) or not pdu_name.strip():
            raise ConfigError(f"{where}: name is missing")
        ip = entry.get("ip")
        if not isinstance(ip, str) or not ip.strip() or ip.strip() == PLACEHOLDER:
            raise ConfigError(f"{where}: ip is missing")
        pdu_id = entry.get("id")
        if not isinstance(pdu_id, int) or isinstance(pdu_id, bool):
            raise ConfigError(f"{where}: id is missing (any number not used by another PDU)")
        rack = entry.get("rack", 0)
        if not isinstance(rack, int) or isinstance(rack, bool) or rack < 0:
            raise ConfigError(f"{where}: rack must be a rack number, 0 if none")
        family = entry.get("family")
        if family not in PDU_FAMILIES:
            raise ConfigError(f"{where}: family must be one of {', '.join(PDU_FAMILIES)}")
        if "via_poweriq" in entry:
            raise ConfigError(f"{where}: via_poweriq is no longer used: delete the line "
                              "(a PDU that should raise no alarms is put in maintenance "
                              "from the rack's popup on the floor map)")
        own = entry.get("community", community)
        if not isinstance(own, str) or not own or ("<" in own and ">" in own):
            raise ConfigError(f"{where}: community must be the PDU's read community")
        if pdu_id in seen:
            raise ConfigError(f"{where}: id {pdu_id} is listed twice")
        seen.add(pdu_id)
        entries.append({"name": pdu_name.strip(), "id": pdu_id, "ip": ip.strip(),
                        "rack": rack or None, "family": family,
                        "model": str(entry.get("model") or ""), "community": own,
                        "own_community": "community" in entry})
    settings["entries"] = tuple(entries)
    settings["file"] = path
    return settings


def _cabinet_list(data: dict, folder: Path) -> dict | None:
    """[cabinet_list]: the Excel workbook of devices and PDU outlets per rack
    (dcmonitor/cabinet_list.py), read only. Absent = not used."""
    section = data.get("cabinet_list")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("[cabinet_list] must be a section")
    unknown = sorted(set(section) - {"file", "check_interval_s"})
    if unknown:
        raise ConfigError(f"[cabinet_list] unknown setting(s): {', '.join(unknown)}")
    file = section.get("file")
    if not isinstance(file, str) or not file.strip():
        raise ConfigError("[cabinet_list] file must be the workbook's path, e.g. "
                          "file = '\\\\<server>\\<share>\\<folder>\\<name>.xlsx' "
                          "(single quotes keep the backslashes)")
    if not file.strip().lower().endswith(".xlsx"):
        raise ConfigError("[cabinet_list] file must be an .xlsx workbook "
                          "(the old .xls format cannot be read)")
    interval = section.get("check_interval_s", 5)
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or not 1 <= interval <= 3600):
        raise ConfigError("[cabinet_list] check_interval_s must be 1 to 3600 seconds")
    file = file.strip()
    if not file.startswith("\\\\") and not Path(file).is_absolute():
        file = str((folder / file).resolve())
    return {"file": file, "check_interval_s": float(interval)}


def load(path: str | Path = "config.toml") -> Config:
    """Read and validate config.toml. Raises ConfigError with a clear reason."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None

    folder = path.parent

    storage = _section(data, "storage")
    database_name = storage.get("database")
    log_folder_name = storage.get("log_folder")
    if not isinstance(database_name, str) or not database_name.strip():
        raise ConfigError("[storage] database must be a file name")
    if not isinstance(log_folder_name, str) or not log_folder_name.strip():
        raise ConfigError("[storage] log_folder must be a folder name")

    polling_section = _section(data, "polling")
    polling = _numbers(polling_section, REQUIRED_POLLING, "polling")

    # Both AC generations keep local time on their clocks. Optional; this site
    # is UTC+3, which is the default when a line is missing.
    offsets = {}
    for key in ("old_ac_clock_utc_offset_hours", "new_ac_clock_utc_offset_hours"):
        value = polling_section.get(key, 3.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not -14 <= value <= 14:
            raise ConfigError(f"[polling] {key} must be a number from -14 to 14")
        offsets[key] = float(value)
    retention = _numbers(_section(data, "retention"), REQUIRED_RETENTION, "retention")
    # Per-circuit amps for the circuit follower; 30 days when absent.
    history = _section(data, "retention").get("circuit_history_days", 30)
    if not isinstance(history, int) or isinstance(history, bool) or history < 2:
        raise ConfigError("[retention] circuit_history_days must be a whole number of days, 2 or more")
    retention["circuit_history_days"] = history
    thresholds = _numbers(_section(data, "thresholds"), REQUIRED_THRESHOLDS, "thresholds")

    if thresholds["ac_clear_above_setpoint_c"] > thresholds["ac_alarm_above_setpoint_c"]:
        raise ConfigError(
            "[thresholds] ac_clear_above_setpoint_c must not be higher than "
            "ac_alarm_above_setpoint_c, otherwise an alarm can never clear"
        )

    # Seconds the temperature must stay at the limit before alarming. Optional;
    # 0 means alarm immediately.
    threshold_section = _section(data, "thresholds")
    delay = threshold_section.get("ac_alarm_delay_s", 60)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        raise ConfigError("[thresholds] ac_alarm_delay_s must be 0 or more seconds")
    thresholds["ac_alarm_delay_s"] = float(delay)

    # The new units alarm on a fixed return temperature, since their setpoint
    # is a supply setpoint and does not suit return air. Optional, default 35.
    new_limit = threshold_section.get("new_ac_return_alarm_c", 35)
    if not isinstance(new_limit, (int, float)) or isinstance(new_limit, bool) or new_limit <= 0:
        raise ConfigError("[thresholds] new_ac_return_alarm_c must be a number greater than 0")
    thresholds["new_ac_return_alarm_c"] = float(new_limit)

    display = data.get("display", {})
    if not isinstance(display, dict):
        raise ConfigError("[display] must be a table")
    offset = display.get("utc_offset_hours", 3)
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        raise ConfigError("[display] utc_offset_hours must be a number")
    if not -14 <= offset <= 14:
        raise ConfigError("[display] utc_offset_hours must be between -14 and 14")

    events = data.get("events", {})
    if not isinstance(events, dict):
        raise ConfigError("[events] must be a table")
    raw_ids = events.get("informational_ids", [])
    if not isinstance(raw_ids, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in raw_ids
    ):
        raise ConfigError("[events] informational_ids must be a list of numbers")

    snmp = _section(data, "snmp")
    community = snmp.get("community")
    if not isinstance(community, str) or not community:
        raise ConfigError("[snmp] community must be a string")

    ac_entries = _devices(data, "ac", "AC")
    ac_units = []
    for entry in ac_entries:
        generation = entry.get("generation")
        if generation not in VALID_GENERATIONS:
            raise ConfigError(
                f"AC {entry['name']}: generation must be "
                f"{' or '.join(VALID_GENERATIONS)}"
            )
        ac_units.append(
            AcUnit(name=entry["name"].strip(), generation=generation, ip=entry["ip"].strip())
        )

    ups_entries = _devices(data, "ups", "UPS")
    ups_units = [
        UpsUnit(name=entry["name"].strip(), ip=entry["ip"].strip())
        for entry in ups_entries
    ]

    # Generators are optional: no [[generator]] entries means none are polled.
    generators = []
    for index, entry in enumerate(data.get("generator", []), start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[generator]] entry {index} is not a table")
        name, ip = entry.get("name"), entry.get("ip")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[generator]] entry {index} has no name")
        if not isinstance(ip, str) or not ip.strip() or ip.strip() == PLACEHOLDER:
            raise ConfigError(f"generator {name}: ip is missing or still a placeholder")
        unit = entry.get("unit", 1)
        if not isinstance(unit, int) or isinstance(unit, bool) or not 0 <= unit <= 255:
            raise ConfigError(f"generator {name}: unit must be a number from 0 to 255")
        generators.append(GeneratorUnit(name=name.strip(), ip=ip.strip(), unit=unit))

    # The UPS tab's slower read; absent means every 60 s, so older config
    # files need no change.
    interval = polling_section.get("ups_details_interval_s", 60)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("[polling] ups_details_interval_s must be a number greater than 0")
    polling["ups_details_interval_s"] = float(interval)

    interval = polling_section.get("generator_interval_s", 30)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ConfigError("[polling] generator_interval_s must be a number greater than 0")
    polling["generator_interval_s"] = float(interval)

    # Generator alarms stay off until a number is set, like the rack alarm.
    for key in ("generator_fuel_low_pct", "generator_battery_low_v"):
        value = threshold_section.get(key)
        if value in (None, ""):
            thresholds[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            thresholds[key] = float(value)
        else:
            raise ConfigError(f"[thresholds] {key} must be a number or empty")

    # UPS input voltage: on at 190 V when the line is absent, so older config
    # files need no change; an empty value switches it off.
    key = "ups_input_voltage_alarm_below_v"
    value = threshold_section.get(key, 190)
    if value == "":
        thresholds[key] = None
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        thresholds[key] = float(value)
    else:
        raise ConfigError(f"[thresholds] {key} must be a number greater than 0, or empty")

    names = ([unit.name for unit in ac_units] + [unit.name for unit in ups_units]
             + [unit.name for unit in generators])
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(f"device names used twice: {', '.join(sorted(duplicates))}")

    pdu_sensors = _pdu_sensors(data)
    _check_poweriq(data)
    return Config(
        path=path,
        folder=folder,
        database=(folder / database_name).resolve(),
        log_folder=(folder / log_folder_name).resolve(),
        utc_offset_hours=offset,
        polling={key: value for key, value in polling.items()},
        retention={key: int(value) for key, value in retention.items()},
        thresholds=thresholds,
        informational_ids=frozenset(raw_ids),
        snmp_community=community,
        ac_units=tuple(ac_units),
        ups_units=tuple(ups_units),
        devices=tuple(ac_units) + tuple(ups_units) + tuple(generators),
        old_ac_utc_offset_hours=offsets["old_ac_clock_utc_offset_hours"],
        new_ac_utc_offset_hours=offsets["new_ac_clock_utc_offset_hours"],
        site_map=(folder / str(storage.get("site_map", "site_map.toml"))).resolve(),
        generators=tuple(generators),
        pdu_sensors=pdu_sensors,
        pdus=_pdus(data, folder, pdu_sensors),
        pdu_rules=_pdu_rules(data),
        cabinet_list=_cabinet_list(data, folder),
    )


if __name__ == "__main__":
    import sys

    try:
        config = load(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    except ConfigError as error:
        print(f"config error: {error}")
        raise SystemExit(1)

    print(f"config: {config.path}")
    print(f"database: {config.database}")
    print(f"logs: {config.log_folder}")
    print(f"display offset: UTC{config.utc_offset_hours:+g}")
    print(f"AC units: {len(config.ac_units)}", end=" ")
    print("(" + ", ".join(f"{u.name}/{u.generation}" for u in config.ac_units) + ")")
    print(f"UPS units: {len(config.ups_units)}")
    print(f"generators: {len(config.generators)}", end=" ")
    print("(" + ", ".join(f"{g.name}/unit {g.unit}" for g in config.generators) + ")")
    fuel = config.thresholds.get("generator_fuel_low_pct")
    battery = config.thresholds.get("generator_battery_low_v")
    print(f"generator alarms: fuel {'off' if fuel is None else f'below {fuel:g} %'}, "
          f"battery {'off' if battery is None else f'below {battery:g} V'}")
    low_input = config.thresholds.get("ups_input_voltage_alarm_below_v")
    print(f"UPS input voltage alarm: "
          f"{'off' if low_input is None else f'below {low_input:g} V'}")
    print(f"informational event IDs: {sorted(config.informational_ids)}")
    if config.pdu_sensors:
        racks = sorted({e["rack"] for e in config.pdu_sensors["entries"]})
        print(f"PDU probes read directly: {len(config.pdu_sensors['entries'])} "
              f"for racks {racks}")
    else:
        print("PDU probes read directly: none listed")
    if config.pdus:
        families = {}
        for entry in config.pdus["entries"]:
            families[entry["family"]] = families.get(entry["family"], 0) + 1
        print(f"PDUs ({config.pdus['file'].name}): "
              + ", ".join(f"{k} {v}" for k, v in sorted(families.items()))
              + f"; read every {config.pdus['interval_s']:g}s")
        rules = config.pdu_rules
        level = lambda key, unit: "off" if rules[key] is None else f"{rules[key]:g}{unit}"
        print(f"  breaker alarm {level('breaker_alarm_pct', ' %')}, warning "
              f"{level('breaker_warning_pct', ' %')}, highlight {level('breaker_highlight_pct', ' %')}, "
              f"default limit {level('default_breaker_limit_a', ' A')}, rack temperature alarm "
              f"{level('rack_temp_alarm_c', ' C')}")
        own = [e["name"] for e in config.pdus["entries"] if e["own_community"]]
        if own:
            print(f"  with their own community (not shown): {', '.join(own)}")
    else:
        print("PDUs read directly: [pdus] not set")
    print(f"site map: {config.site_map} ({'found' if config.site_map.is_file() else 'missing'})")
    if config.cabinet_list:
        # Only the file's name: the path holds the server's address.
        name = config.cabinet_list["file"].replace("\\", "/").rsplit("/", 1)[-1]
        print(f"cabinet list: {name}, checked every "
              f"{config.cabinet_list['check_interval_s']:g}s")
    else:
        print("cabinet list: [cabinet_list] not set")

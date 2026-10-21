"""Alarm rules for dcmonitor.

One place decides what counts as an alarm, so the collectors only fetch data
and the panel only displays it. Every alarm has a key that identifies the same
condition over time, so a condition that stays true does not open a second row
and the matching clear finds the right one.

Keys in use:
    on_battery, on_bypass, battery_temp, battery_charge, input_voltage_low,
    ups_alarms
    temperature
    event:<id>:<name>      new AC units, raised by Start and cleared by Stop
    unit_alarm             old AC units, cleared by any newer event
    unreachable            any device, after repeated poll failures
    breaker:<pdu>:<C1>, breaker_warning:..., breaker_trip:...,
    pdu_unreachable:<pdu>, pdu_health:<pdu>, follow_<rule>:<pdu>:<label>
                           one PDU's alarms, <pdu> = its id in pdus.toml
                           (pdu_health: Power IQ's notices, before 2 Oct 2026)

A PDU in maintenance (put there from the panel, stored in the database)
raises none of its alarms; its open ones are closed when it goes in.
"""

import threading
from datetime import datetime, timezone

try:
    from .event_descriptions import describe
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.event_descriptions import describe

ALARM_KIND_UPS = "ups"
ALARM_KIND_TEMPERATURE = "temperature"
ALARM_KIND_UNIT_EVENT = "unit_event"
ALARM_KIND_REACHABILITY = "reachability"
ALARM_KIND_RACK = "rack_temperature"
ALARM_KIND_BREAKER_TRIP = "breaker_trip"

# Notices: the device's own warnings. Amber, one soft sound, never the
# repeating alarm sound.
ALARM_KIND_NOTICE_UPS = "ups_notice"
ALARM_KIND_NOTICE_BREAKER = "breaker_notice"
# pdu_health was Power IQ's PDU health notice (removed 2 Oct 2026); kept so
# old rows still show as notices.
NOTICE_KINDS = {ALARM_KIND_NOTICE_UPS, "pdu_health", ALARM_KIND_NOTICE_BREAKER,
                "circuit_change"}   # circuit_drop (follower.py) is a real alarm

# Alarm keys that belong to one PDU: "<prefix>:<pdu id>[:...]".
PDU_KEY_PREFIXES = ("breaker", "breaker_warning", "breaker_trip", "pdu_unreachable",
                    "pdu_health")


def pdu_of_key(key: str) -> int | None:
    """The PDU id an alarm key belongs to, or None for any other alarm."""
    prefix, _, rest = key.partition(":")
    if prefix in PDU_KEY_PREFIXES or prefix.startswith("follow_"):
        try:
            return int(rest.split(":", 1)[0])
        except ValueError:
            return None
    return None


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class AlarmEngine:
    """Applies the rules and records the result through Storage.

    on_change(action, device, description) is called for every alarm raised or
    cleared, so the panel can sound the buzzer and refresh without polling the
    database.
    """

    def __init__(self, storage, config, on_change=None):
        self.storage = storage
        self.config = config
        self.on_change = on_change
        self._failures = {}
        self._hot_since = {}         # device -> when it first reached the limit
        # PDU id -> {"name", "rack", "since_utc"}: PDUs in maintenance. The
        # lock keeps a poll from raising an alarm between the check and the
        # moment a PDU goes into maintenance.
        self.maintenance = storage.maintenance()
        self._maintenance_lock = threading.Lock()

    # ------------------------------------------------------------- helpers

    def _raise(self, device: str, kind: str, key: str, description: str,
               moment=None) -> None:
        with self._maintenance_lock:
            if pdu_of_key(key) in self.maintenance:
                return
            raised = self.storage.raise_alarm(device, kind, key, description, moment)
        if raised:
            self._notify("raised", device, description)

    def _clear(self, device: str, key: str, description: str = "",
               moment=None) -> None:
        if self.storage.clear_alarm(device, key, moment):
            self._notify("cleared", device, description or key)

    def _notify(self, action: str, device: str, description: str) -> None:
        if self.on_change:
            self.on_change(action, device, description)

    def _threshold(self, name: str) -> float:
        return self.config.thresholds[name]

    # ----------------------------------------------------------------- UPS

    def evaluate_ups(self, device: str, data: dict) -> None:
        """Rules 1 to 5 against one read() result from the UPS collector."""
        if data["on_battery"]:
            self._raise(device, ALARM_KIND_UPS, "on_battery",
                        "UPS running on battery, mains power lost")
        else:
            self._clear(device, "on_battery", "UPS back on mains power")

        if data["on_bypass"]:
            self._raise(device, ALARM_KIND_UPS, "on_bypass",
                        "UPS running on bypass, load not protected")
        else:
            self._clear(device, "on_bypass", "UPS off bypass")

        temperature = data["metrics"]["battery_temp_c"]
        limit = self._threshold("ups_battery_temp_alarm_c")
        if temperature >= limit:
            self._raise(device, ALARM_KIND_UPS, "battery_temp",
                        f"UPS temperature {temperature} C, limit {limit:g} C")
        else:
            self._clear(device, "battery_temp",
                        f"UPS temperature back to {temperature} C")

        charge = data["metrics"]["battery_charge_pct"]
        floor = self._threshold("ups_battery_charge_alarm_below_pct")
        if charge < floor:
            self._raise(device, ALARM_KIND_UPS, "battery_charge",
                        f"Battery charge {charge}%, below {floor:g}%")
        else:
            self._clear(device, "battery_charge", f"Battery charge back to {charge}%")

        # Input voltage: any phase below the limit. Off while the threshold is
        # empty; left as it is when the UPS gave no input voltages this poll.
        low_limit = self.config.thresholds.get("ups_input_voltage_alarm_below_v")
        inputs = {phase: data["metrics"].get(f"input_voltage_v_l{phase}")
                  for phase in (1, 2, 3)}
        inputs = {phase: value for phase, value in inputs.items() if value is not None}
        if low_limit is None:
            self._clear(device, "input_voltage_low", "Input voltage alarm switched off")
        elif inputs:
            low = [f"L{phase} {value:g} V" for phase, value in inputs.items()
                   if value < low_limit]
            if low:
                # Short on purpose: it has to fit a card's footer.
                self._raise(device, ALARM_KIND_UPS, "input_voltage_low",
                            f"Input {', '.join(low)}, below {low_limit:g} V")
            else:
                self._clear(device, "input_voltage_low",
                            "Input voltage back to "
                            + "/".join(f"{value:g}" for value in inputs.values()) + " V")

        # The UPS's own alarm count used to raise a loud alarm here. Its
        # alarms are now notices, per alarm type, from evaluate_ups_notices.
        self._clear(device, "ups_alarms", "Replaced by per-alarm notices")

    # ------------------------------------------------------- temperature

    def _delayed_temperature(self, device, temperature, alarm_at, clear_at,
                             description, moment):
        """Raise only after the limit has held for the configured delay.

        A start-up spike when a unit leaves standby lasts well under a minute
        and raises nothing; a real cooling failure lasts far longer and still
        does. Clearing is immediate. The alarm's start time is when the
        temperature first reached the limit, so its duration is the real one.
        """
        delay = float(self.config.thresholds.get("ac_alarm_delay_s", 60))
        if temperature >= alarm_at:
            since = self._hot_since.setdefault(device, moment)
            if (moment - since).total_seconds() >= delay:
                self._raise(device, ALARM_KIND_TEMPERATURE, "temperature",
                            description, since)
        elif temperature < clear_at:
            self._hot_since.pop(device, None)
            self._clear(device, "temperature",
                        f"Temperature back to {temperature:.1f} C", moment)

    def evaluate_temperature(self, device: str, temperature: float,
                             setpoint: float, moment=None) -> None:
        """Old units: return temperature against setpoint + margin."""
        moment = moment or datetime.now(timezone.utc)
        margin = self._threshold("ac_alarm_above_setpoint_c")
        self._delayed_temperature(
            device, temperature,
            alarm_at=setpoint + margin,
            clear_at=setpoint + self._threshold("ac_clear_above_setpoint_c"),
            description=(f"Temperature {temperature:.1f} C at or above "
                         f"setpoint {setpoint:g} C plus {margin:g} C"),
            moment=moment,
        )

    def evaluate_return_limit(self, device: str, temperature: float,
                              moment=None) -> None:
        """New units: return temperature against a fixed limit."""
        moment = moment or datetime.now(timezone.utc)
        limit = float(self.config.thresholds.get("new_ac_return_alarm_c", 35))
        self._delayed_temperature(
            device, temperature, alarm_at=limit, clear_at=limit,
            description=f"Return temperature {temperature:.1f} C, limit {limit:g} C",
            moment=moment,
        )

    # ---------------------------------------------------- new AC events

    def evaluate_new_ac_event(self, device: str, event) -> None:
        """Rule 6. Start raises, Stop clears, informational IDs stay silent.

        Called once per event that was not already in the database, so an
        event is never acted on twice.
        """
        description = describe(event.event_id, event.name)
        key = f"event:{event.event_id}:{event.name}"
        informational = event.event_id in self.config.informational_ids

        if event.action.lower() == "stop":
            self._clear(device, key, f"{description} ended", _parse(event.event_utc))
            return
        if informational:
            return
        self._raise(device, ALARM_KIND_UNIT_EVENT, key, description,
                    _parse(event.event_utc))

    # ---------------------------------------------------- old AC events

    def evaluate_old_ac_events(self, device: str, events: list) -> None:
        """Rule 9. Events of type Alarm raise; any newer event clears.

        These units write one line per event and never a matching clear, so the
        next event of any type, whatever its type, is what says the alarm is
        over.

        Takes the events that were new in this poll, oldest first, so an alarm
        followed by a later event in the same batch ends up cleared.
        """
        for event in sorted(events, key=lambda item: item.event_utc):
            moment = _parse(event.event_utc)
            if event.is_alarm:
                self._raise(device, ALARM_KIND_UNIT_EVENT, "unit_alarm",
                            event.name, moment)
            else:
                self._clear(device, "unit_alarm",
                            f"Cleared by later event: {event.name}", moment)

    # ---------------------------------------------------------- generators

    def evaluate_generator(self, device: str, values: dict) -> None:
        """Low fuel and low battery. Each is off while its threshold is empty."""
        # A threshold switched off also closes any alarm it left open.
        fuel_limit = self.config.thresholds.get("generator_fuel_low_pct")
        fuel = values.get("fuel_level_pct")
        if fuel_limit is None:
            self._clear(device, "fuel_low", "Low-fuel alarm switched off")
        elif fuel is not None:
            if fuel < fuel_limit:
                self._raise(device, "generator", "fuel_low",
                            f"Fuel level {fuel:g} %, below {fuel_limit:g} %")
            else:
                self._clear(device, "fuel_low", f"Fuel level back to {fuel:g} %")

        battery_limit = self.config.thresholds.get("generator_battery_low_v")
        battery = values.get("battery_v")
        if battery_limit is None:
            self._clear(device, "battery_low", "Low-battery alarm switched off")
        elif battery is not None:
            if battery < battery_limit:
                self._raise(device, "generator", "battery_low",
                            f"Battery {battery:.1f} V, below {battery_limit:g} V")
            else:
                self._clear(device, "battery_low", f"Battery back to {battery:.1f} V")

    # ------------------------------------------------------------ breakers

    def evaluate_breaker(self, device: str, key: str, label: str,
                         percent: float, current: float, limit: float) -> None:
        """A breaker's load against two [pdus] levels.

        At or above breaker_alarm_pct (90 on site): real alarm, repeating
        sound. At or above breaker_warning_pct (80): amber notice, one soft
        sound. The warning is closed when the alarm takes over, and both are
        closed below the warning level or when their level is switched off.
        The lower highlight level only colours the floor map.
        """
        settings = getattr(self.config, "pdu_rules", None) or {}
        alarm_at = settings.get("breaker_alarm_pct")
        warning_at = settings.get("breaker_warning_pct")
        warning_key = "breaker_warning:" + key.split(":", 1)[-1]
        loaded = f"{label} at {percent:.0f} % ({current:.1f} of {limit:g} A)"

        if alarm_at is not None and percent >= alarm_at:
            self._raise(device, "breaker", key, f"{loaded}, alarm at {alarm_at:g} %")
            self._clear(device, warning_key, "Breaker warning: now an alarm")
            return
        if alarm_at is None:
            self._clear(device, key, "Breaker alarm switched off")
        else:
            self._clear(device, key, f"{label} back to {percent:.0f} %")

        if warning_at is not None and percent >= warning_at:
            self._raise(device, ALARM_KIND_NOTICE_BREAKER, warning_key,
                        f"{loaded}, warning from {warning_at:g} %")
        elif warning_at is None:
            self._clear(device, warning_key, "Breaker warning switched off")
        else:
            self._clear(device, warning_key, f"{label} back to {percent:.0f} %")

    def evaluate_breaker_trip(self, device: str, key: str, label: str,
                              state: str | None) -> None:
        """A breaker the PDU reports open: tripped (or switched off by hand).
        Real alarm, no delay. Read from Raritan PX2/PX3 PDUs, whose breakers
        have a trip sensor (closed / open). Unknown state leaves it as it is."""
        if state == "open":
            self._raise(device, ALARM_KIND_BREAKER_TRIP, key,
                        f"{label} tripped: the breaker is open")
        elif state == "closed":
            self._clear(device, key, f"{label} closed again")

    def pdu_poll(self, device: str, key: str, name: str, ok: bool,
                 error: str = "", limit: int | None = None) -> bool:
        """Reachability of one PDU read directly, inside the PDU job, with the
        PDU's own key so each PDU has its own alarm: raised after `limit`
        failed reads in a row ([pdus] alarm_after_failures, 3 by default;
        else the devices' unreachable_after_failures). True while it counts
        as not answering."""
        if ok:
            self._failures.pop(key, None)
            self._clear(device, key, f"{name} answering again")
            return False
        if limit is None:
            limit = int(self.config.polling["unreachable_after_failures"])
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= limit:
            reason = error.split(": ", 1)[-1].strip() if error else ""
            self._raise(device, ALARM_KIND_REACHABILITY, key,
                        f"{name} not answering" + (f" ({reason})" if reason else ""))
            return True
        return False

    # ---------------------------------------------------- PDU maintenance

    def start_maintenance(self, pdu_id: int, name: str, rack: int | None) -> int:
        """A technician put a PDU in maintenance: close its open alarms and
        raise none until it is ended. Returns how many were closed."""
        with self._maintenance_lock:
            self.storage.start_maintenance(pdu_id, name, rack)
            self.maintenance = self.storage.maintenance()
            closed = [alarm for alarm in self.storage.active_alarms()
                      if pdu_of_key(alarm["key"]) == pdu_id]
            for alarm in closed:
                self.storage.clear_alarm(alarm["device"], alarm["key"])
        for alarm in closed:
            self._notify("cleared", alarm["device"],
                         f"{alarm['description']}: {name} put in maintenance")
        return len(closed)

    def end_maintenance(self, pdu_id: int) -> bool:
        """The PDU's alarms work again from its next reading."""
        with self._maintenance_lock:
            ended = self.storage.end_maintenance(pdu_id)
            self.maintenance = self.storage.maintenance()
        return ended

    # ---------------------------------------------------------- UPS notices

    def evaluate_ups_notices(self, device: str, alarms, count: int) -> None:
        """The UPS's own alarm table, as notices: soft sound once, amber.

        alarms is [(code, description)] from the table, or None if the table
        could not be read, in which case only the count is shown.
        """
        from dcmonitor.collectors.ups import COVERED_ELSEWHERE  # local: avoids a cycle
        wanted = {}
        if alarms is None:
            if count > 0:
                wanted["ups_notice:count"] = f"UPS reports {count} alarm(s)"
        else:
            for code, text in alarms:
                if code in COVERED_ELSEWHERE:
                    continue
                wanted[f"ups_notice:{code}"] = text
        for key, text in wanted.items():
            self._raise(device, ALARM_KIND_NOTICE_UPS, key, text)
        for alarm in self.storage.active_alarms():
            if (alarm["device"] == device and alarm["kind"] == ALARM_KIND_NOTICE_UPS
                    and alarm["key"] not in wanted):
                self._clear(device, alarm["key"], f"{alarm['description']}: cleared")

    # ---------------------------------------------------- rack temperature

    def evaluate_rack(self, device: str, temperature: float, moment=None) -> None:
        """Rack temperature, from the PDUs' probes.

        Switched off while [pdus] rack_temp_alarm_c is empty: the readings
        are stored and shown, but nothing is raised. No delay.
        """
        settings = getattr(self.config, "pdu_rules", None) or {}
        limit = settings.get("rack_temp_alarm_c")
        if limit is None:
            self._clear(device, "rack_temp", "Rack temperature alarm switched off")
            return
        if temperature >= limit:
            self._raise(device, ALARM_KIND_RACK, "rack_temp",
                        f"Rack temperature {temperature:.1f} C, limit {limit:g} C", moment)
        else:
            self._clear(device, "rack_temp",
                        f"Rack temperature back to {temperature:.1f} C", moment)

    # ------------------------------------------------------ reachability

    def poll_succeeded(self, device: str) -> None:
        """Rule 10. Reset the failure count and clear an unreachable alarm.

        The clear runs on every success, not only when failures were counted in
        memory. The alarm lives in the database, so after a restart the count
        starts at zero while the alarm is still open; gating on the count left
        such alarms open for good.
        """
        self._failures.pop(device, None)
        self._clear(device, "unreachable", "Device answering again")

    def poll_failed(self, device: str, error: str = "") -> None:
        """Rule 10. Raise after the configured number of failures in a row."""
        limit = int(self.config.polling["unreachable_after_failures"])
        self._failures[device] = self._failures.get(device, 0) + 1
        if self._failures[device] >= limit:
            # "CollectorError: timed out" reads as "timed out" on screen.
            reason = error.split(": ", 1)[-1].strip() if error else ""
            detail = f" ({reason})" if reason else ""
            self._raise(device, ALARM_KIND_REACHABILITY, "unreachable",
                        f"Not answering{detail}")

    def failure_count(self, device: str) -> int:
        return self._failures.get(device, 0)


if __name__ == "__main__":
    import tempfile
    from dataclasses import dataclass
    from pathlib import Path

    from dcmonitor.storage import Storage

    @dataclass
    class FakeConfig:
        thresholds: dict
        polling: dict
        informational_ids: frozenset

    @dataclass
    class FakeOldEvent:
        event_utc: str
        name: str
        kind: str

        @property
        def is_alarm(self) -> bool:
            return self.kind.lower() == "alarm"

    @dataclass
    class FakeNewEvent:
        event_utc: str
        event_id: int
        name: str
        action: str

    config = FakeConfig(
        thresholds={
            "ac_alarm_above_setpoint_c": 3,
            "ac_clear_above_setpoint_c": 3,
            "ac_alarm_delay_s": 60,
            "new_ac_return_alarm_c": 35,
            "ups_battery_temp_alarm_c": 30,
            "ups_battery_charge_alarm_below_pct": 70,
            "ups_input_voltage_alarm_below_v": 190,
        },
        polling={"unreachable_after_failures": 5},
        informational_ids=frozenset({152, 153, 154, 155, 156, 206, 212}),
    )

    with tempfile.TemporaryDirectory() as folder:
        store = Storage(Path(folder) / "test.db")
        engine = AlarmEngine(store, config,
                             on_change=lambda a, d, t: print(f"  {a:8} {d:6} {t}"))

        print("UPS on battery:")
        engine.evaluate_ups("UPS 2", {
            "on_battery": True, "on_bypass": False, "alarms_present": 1,
            "metrics": {"battery_temp_c": 26, "battery_charge_pct": 100},
        })
        print("UPS back to normal:")
        engine.evaluate_ups("UPS 2", {
            "on_battery": False, "on_bypass": False, "alarms_present": 0,
            "metrics": {"battery_temp_c": 26, "battery_charge_pct": 100},
        })

        print("UPS input L2 sags to 185 V, then recovers:")
        ok_metrics = {"battery_temp_c": 26, "battery_charge_pct": 100}
        for inputs in ((224, 185, 224), (224, 186, 224), (224, 225, 224)):
            engine.evaluate_ups("UPS 1", {
                "on_battery": False, "on_bypass": False, "alarms_present": 0,
                "metrics": {**ok_metrics, **{f"input_voltage_v_l{n}": v
                                             for n, v in zip((1, 2, 3), inputs)}},
            })
        print("UPS with no input voltages (older agent): nothing happens")
        engine.evaluate_ups("UPS 1", {"on_battery": False, "on_bypass": False,
                                      "alarms_present": 0, "metrics": ok_metrics})

        from datetime import timedelta as _td
        start = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        print("26.5 C for 30 s, setpoint 23, delay 60 s (a start-up spike):")
        engine.evaluate_temperature("AC-4", 26.5, 23.0, start)
        engine.evaluate_temperature("AC-4", 26.5, 23.0, start + _td(seconds=30))
        engine.evaluate_temperature("AC-4", 24.0, 23.0, start + _td(seconds=45))
        print("26.8 C held for 75 s (a real failure):")
        engine.evaluate_temperature("AC-4", 26.8, 23.0, start + _td(minutes=5))
        engine.evaluate_temperature("AC-4", 26.8, 23.0, start + _td(minutes=5, seconds=30))
        engine.evaluate_temperature("AC-4", 26.8, 23.0, start + _td(minutes=6, seconds=15))
        print("new unit return 35.4 C held for 70 s against the fixed 35:")
        engine.evaluate_return_limit("AC-7", 35.4, start)
        engine.evaluate_return_limit("AC-7", 35.4, start + _td(seconds=70))
        engine.evaluate_return_limit("AC-7", 34.8, start + _td(seconds=90))
        print("back to 25.9 C (below setpoint + 3):")
        engine.evaluate_temperature("AC-4", 25.9, 23.0, start + _td(minutes=9))

        print("new AC informational event:")
        engine.evaluate_new_ac_event("AC-7", FakeNewEvent(
            "2026-09-18T12:00:04Z", 155, "Al_UnitOn.Active", "Start"))
        print("new AC real event and its stop:")
        engine.evaluate_new_ac_event("AC-7", FakeNewEvent(
            "2026-09-18T12:05:00Z", 66, "Al_CloggedFilt.Active", "Start"))
        engine.evaluate_new_ac_event("AC-7", FakeNewEvent(
            "2026-09-18T12:30:00Z", 66, "Al_CloggedFilt.Active", "Stop"))

        print("old AC alarm then a later message:")
        engine.evaluate_old_ac_events("AC-3", [
            FakeOldEvent("2026-09-18T10:00:00Z", "High Return Temperature", "Alarm"),
        ])
        engine.evaluate_old_ac_events("AC-3", [
            FakeOldEvent("2026-09-18T10:20:00Z", "Event: Power On", "Message"),
        ])

        print("five failed polls then a success:")
        for _ in range(5):
            engine.poll_failed("AC-5", "timed out")
        engine.poll_succeeded("AC-5")

        print("open alarms at the end:", store.active_alarms())
        store.close()

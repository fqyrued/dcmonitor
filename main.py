"""dcmonitor entry point.

Runs one job per device task on its own thread, so a stalled unit delays only
itself. Jobs belonging to the same device share a lock, so a unit never gets
two requests at the same time. Results go into SQLite through Storage and into
the alarm rules through AlarmEngine, and every change is pushed onto a queue
the panel will read later.

    python main.py              open the panel and keep monitoring
    python main.py --once       one pass over all devices, then exit
    python main.py --console    keep running, print changes in the terminal
"""

import argparse
import logging
import logging.handlers
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from dcmonitor import config as config_module
from dcmonitor.alarms import AlarmEngine
from dcmonitor.cabinet_list import CabinetList
from dcmonitor.collectors import (apc, generator, geist, new_ac, old_ac, pdu_sensor,
                                  raritan_px1, raritan_px2, ups)
from dcmonitor.follower import Breaker, Follower, PduPower
from dcmonitor.storage import Storage, to_text, utc_now

log = logging.getLogger("dcmonitor")

# PDU families (pdus.toml "family") and their readers. Every PDU on site is
# read directly; Power IQ is no longer used (2 Oct 2026).
DIRECT_READERS = {"raritan_px2": raritan_px2, "raritan_px1": raritan_px1, "geist": geist,
                  "apc": apc}


def setup_logging(folder, keep_days: int, to_console: bool) -> None:
    """Daily log files kept for keep_days, plus optional console output."""
    folder.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()

    file_handler = logging.handlers.TimedRotatingFileHandler(
        folder / "dcmonitor.log", when="midnight", backupCount=keep_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    log.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(console)


class Job:
    """One repeating task against one device."""

    def __init__(self, device: str, name: str, interval: float, run, lock):
        self.device = device
        self.name = name
        self.interval = interval
        self.run = run
        self.lock = lock
        self.last_run = None
        self.last_ok = None
        self.last_duration_ms = 0
        self.last_error = ""

    @property
    def label(self) -> str:
        return f"{self.device} {self.name}"


class Monitor:
    """Owns the devices, the jobs, the database and the alarm rules."""

    def __init__(self, config):
        self.config = config
        self.storage = Storage(config.database)
        self.updates = queue.Queue()
        self.alarms = AlarmEngine(self.storage, config, on_change=self._on_alarm)
        self.values = {}                 # device -> {metric: value}
        self.states = {}                 # device -> {name: text state}
        self.direct_racks = {}           # rack number -> reading, from a [[pdu_sensor]] probe
        self.rack_power = {}             # rack number -> kW and per-PDU lines
        self.rack_breakers = {}          # rack number -> highest breaker % and lines
        self.pdu_health = {}             # rack number -> PDUs that do not answer
        self._power_seen = {}            # rack number -> newest reading time stored
        self._power_records = {}         # PDU id -> power record
        self._power_lock = threading.Lock()
        self._direct_down = {}           # rack -> PDUs read directly that do not answer
        self._outlets = {}               # PDU id -> its OutletReadings (Raritan only)
        # The Excel list of devices per rack, on the file share ([cabinet_list]).
        self.cabinet = (CabinetList(config.cabinet_list["file"])
                        if config.cabinet_list else None)
        self.pdu_temps = {}              # rack number -> temperature from a PDU read directly
        self._layouts = {}               # PDU id -> (layout, when read)
        self._direct_entries = []        # pdus.toml entries read directly
        self._probe_entries = []         # [[pdu_sensor]] entries on PDUs not read directly
        self._locks = {}                 # device -> lock shared by its jobs
        self._primed = set()             # devices whose event history is loaded
        self._high_water = {}            # device -> newest event time judged
        self._last_modified = {}         # old units, for change detection
        self._stop = threading.Event()
        # The circuit follower watches every circuit and PDU inlet of the
        # PDUs read directly; its settings are in [pdus].
        self.follower = (Follower(self.storage, self.alarms, config.pdu_rules)
                         if config.pdus else None)
        self._close_power_iq_alarms()
        self.jobs = self._build_jobs()

    def _close_power_iq_alarms(self) -> None:
        """Power IQ was removed (2 Oct 2026). Its PDU health notices and its
        own "not answering" alarm, if open in the database, would never be
        closed by anything now, so they are closed once here."""
        for alarm in self.storage.active_alarms():
            if alarm["kind"] == "pdu_health" or alarm["device"] == "Power IQ":
                self.alarms._clear(alarm["device"], alarm["key"],
                                   "Power IQ is no longer used by dcmonitor")

    # ------------------------------------------------------------- wiring

    def _lock_for(self, device: str):
        return self._locks.setdefault(device, threading.Lock())

    def _build_jobs(self) -> list:
        polling = self.config.polling
        jobs = []
        for unit in self.config.ac_units:
            lock = self._lock_for(unit.name)
            if unit.is_new:
                jobs.append(Job(unit.name, "values", polling["new_ac_values_interval_s"],
                                lambda u=unit: self._new_ac_values(u), lock))
                jobs.append(Job(unit.name, "events", polling["new_ac_events_interval_s"],
                                lambda u=unit: self._new_ac_events(u), lock))
            else:
                jobs.append(Job(unit.name, "values", polling["old_ac_values_interval_s"],
                                lambda u=unit: self._old_ac_values(u), lock))
                jobs.append(Job(unit.name, "events", polling["old_ac_event_interval_s"],
                                lambda u=unit: self._old_ac_events(u), lock))
        for unit in self.config.ups_units:
            jobs.append(Job(unit.name, "snmp", polling["ups_interval_s"],
                            lambda u=unit: self._ups(u), self._lock_for(unit.name)))
            jobs.append(Job(unit.name, "details", polling["ups_details_interval_s"],
                            lambda u=unit: self._ups_details(u), self._lock_for(unit.name)))

        for unit in self.config.generators:
            jobs.append(Job(unit.name, "modbus", polling["generator_interval_s"],
                            lambda u=unit: self._generator(u), self._lock_for(unit.name)))

        # Raritan PX2/PX3, PX1, Geist and APC PDUs from pdus.toml, read
        # directly over SNMP.
        if self.config.pdus:
            self._direct_entries = [entry for entry in self.config.pdus["entries"]
                                    if entry["family"] in DIRECT_READERS]
            if self._direct_entries:
                jobs.append(Job("PDUs", "snmp", self.config.pdus["interval_s"],
                                self._pdus_direct, self._lock_for("PDUs")))

        # [[pdu_sensor]] probes, except on PDUs read directly: those PDUs'
        # probes come with their readings, so reading them again is skipped.
        probes = self.config.pdu_sensors
        if probes:
            direct_ips = {entry["ip"] for entry in self._direct_entries}
            self._probe_entries = [entry for entry in probes["entries"]
                                   if entry["ip"] not in direct_ips]
            if self._probe_entries:
                jobs.append(Job("PDU probes", "snmp", probes["interval_s"],
                                self._pdu_probes, self._lock_for("PDU probes")))

        return jobs

    def _on_alarm(self, action: str, device: str, description: str) -> None:
        log.warning("ALARM %s: %s %s", action, device, description)
        self.updates.put({"type": "alarm", "action": action,
                          "device": device, "description": description})

    def _store_values(self, device: str, values: dict) -> None:
        moment = utc_now()
        for metric, value in values.items():
            self.storage.add_sample(device, metric, value, moment)
        current = self.values.setdefault(device, {})
        current.update(values)
        current["updated_utc"] = moment
        self.updates.put({"type": "values", "device": device, "values": dict(values)})

    def _event_cutoff(self) -> str:
        """Events older than this are not stored; retention would drop them."""
        days = int(self.config.retention["unit_events_days"])
        return to_text(utc_now() - timedelta(days=days))

    def _judge(self, device: str, events: list) -> list:
        """Events newer than anything seen before for this device.

        The high-water mark is what keeps an old entry from raising an alarm
        if it is ever stored again, for example after retention removed it and
        the unit served it once more.
        """
        mark = self._high_water.get(device, "")
        newer = [event for event in events if event.event_utc > mark]
        if events:
            self._high_water[device] = max(
                mark, max(event.event_utc for event in events)
            )
        return newer

    def _store_states(self, device: str, states: dict) -> None:
        """Text states such as unit on or standby. Not stored in the database."""
        if not states:
            return
        self.states.setdefault(device, {}).update(states)
        self.updates.put({"type": "states", "device": device, "states": dict(states)})

    def _store_events(self, device: str, events: list, source: str) -> list:
        """Store events, returning only the ones not seen before."""
        fresh = []
        cutoff = self._event_cutoff()
        for event in events:
            if event.event_utc < cutoff:
                continue
            event_id = getattr(event, "event_id", -1)
            action = getattr(event, "action", None) or getattr(event, "kind", "")
            description = getattr(event, "description", "") or event.name
            if self.storage.add_unit_event(
                device=device, event_utc=event.event_utc, event_id=event_id,
                name=event.name, action=action, description=description,
                source=source,
                var1=getattr(event, "var1", None), var2=getattr(event, "var2", None),
            ):
                fresh.append(event)
        return fresh

    # ----------------------------------------------------------- the jobs

    def _new_ac_values(self, unit) -> str:
        polling = self.config.polling
        values, states = new_ac.read_values(
            unit.ip, timeout=polling["new_ac_timeout_s"],
            max_query_chars=int(polling["new_ac_max_query_chars"]),
        )
        self._store_values(unit.name, values)
        self._store_states(unit.name, states)
        if "return_temp_c" in values:
            self.alarms.evaluate_return_limit(unit.name, values["return_temp_c"])
        text = ", ".join(f"{k}={v:g}" for k, v in values.items())
        if states.get("unit_status"):
            text += f", {states['unit_status']}"
        return text

    def _new_ac_events(self, unit) -> str:
        events = new_ac.read_events(unit.ip,
                                    timeout=self.config.polling["new_ac_timeout_s"],
                                    utc_offset_hours=self.config.new_ac_utc_offset_hours)
        fresh = self._store_events(unit.name, events, source="unit_log")
        judged = self._judge(unit.name, events)
        if unit.name not in self._primed:
            self._primed.add(unit.name)
            return f"{len(events)} in log, {len(fresh)} stored as history"
        for event in sorted(judged, key=lambda item: item.event_utc):
            self.alarms.evaluate_new_ac_event(unit.name, event)
        return f"{len(events)} in log, {len(fresh)} stored, {len(judged)} new"

    def _old_ac_values(self, unit) -> str:
        values, states, unavailable = old_ac.read_values(
            unit.ip, timeout=self.config.polling["old_ac_timeout_s"]
        )
        self._store_values(unit.name, values)
        self._store_states(unit.name, states)
        if "return_temp_c" in values and "return_setpoint_c" in values:
            self.alarms.evaluate_temperature(
                unit.name, values["return_temp_c"], values["return_setpoint_c"]
            )
        text = ", ".join(f"{k}={v:g}" for k, v in values.items())
        if states.get("unit_status"):
            text += f", {states['unit_status']}"
        if unavailable:
            text += f" (no reading: {', '.join(unavailable)})"
        return text

    def _old_ac_events(self, unit) -> str:
        """HEAD first; the 51KB body is fetched only when Last-Modified moves."""
        timeout = self.config.polling["old_ac_timeout_s"]
        stamp = old_ac.last_modified(unit.ip, timeout=timeout)
        known = self._last_modified.get(unit.name)
        if known == stamp and unit.name in self._primed:
            return f"unchanged ({stamp})"

        events = old_ac.read_events(unit.ip, timeout=timeout,
                                    utc_offset_hours=self.config.old_ac_utc_offset_hours)
        self._last_modified[unit.name] = stamp
        fresh = self._store_events(unit.name, events, source="unit_log")
        judged = self._judge(unit.name, events)
        if unit.name not in self._primed:
            self._primed.add(unit.name)
            return f"{len(events)} in file, {len(fresh)} stored as history"
        self.alarms.evaluate_old_ac_events(unit.name, judged)
        return f"{len(events)} in file, {len(fresh)} stored, {len(judged)} new"

    def _generator(self, unit) -> str:
        """Fuel level and battery voltage, the two values confirmed on site."""
        values = generator.read(unit.ip, unit=unit.unit)
        self._store_values(unit.name, values)
        self.alarms.evaluate_generator(unit.name, values)
        return ", ".join(f"{k}={v:g}" for k, v in values.items())

    # ---------------------------------------------------------- PDU power

    def _power_record(self, source: str, pdu, reading_utc, stale: bool,
                      states: dict, time_format: str) -> dict:
        """One PDU's power and circuits, as read from the PDU."""
        when = reading_utc.astimezone().strftime(time_format) if reading_utc else "?"
        circuits = [{
            "pdu": pdu.name, "label": breaker.label, "current": breaker.current,
            "key": Follower.key_of(pdu.pdu_id, breaker.label),
            "limit": breaker.limit, "percent": breaker.percent,
            "limit_from_config": breaker.limit_from_config, "time": when,
            "state": states.get(breaker.label),
        } for breaker in pdu.breakers]
        return {"source": source, "rack": pdu.rack, "name": pdu.name, "kw": pdu.kw,
                "pdu_id": pdu.pdu_id,
                "key": Follower.key_of(pdu.pdu_id, "inlet"),
                "inlet_current": pdu.inlet_current, "inlet_limit": pdu.inlet_limit,
                "when": when, "time": reading_utc, "stale": stale, "circuits": circuits}

    def _publish_power(self):
        """Rebuild the per-rack power and breaker views from every PDU record
        read directly, store each rack's values once
        per new reading, and tell the panel. Returns the highest breaker %."""
        with self._power_lock:
            records = sorted(self._power_records.values(),
                             key=lambda r: (r["rack"], r["name"].upper()))
            power, breakers = {}, {}
            for record in records:
                rack = power.setdefault(record["rack"], {"value": None, "lines": [],
                                                         "pdus": [], "stale": False,
                                                         "time": None})
                if record["kw"] is not None:
                    rack["value"] = (rack["value"] or 0) + record["kw"]
                inlet = ""
                if record["inlet_current"] is not None and record["inlet_limit"]:
                    inlet = (f", inlet {record['inlet_current']:.1f} / "
                             f"{record['inlet_limit']:g} A")
                kw_text = (f"{record['kw']:.2f} kW" if record["kw"] is not None
                           else "no power reading")
                rack["lines"].append(f"{record['name']}: {kw_text}{inlet}  ({record['when']})")
                # The same PDU as a record, for the floor map's popup.
                rack["pdus"].append({"pdu": record["name"], "kw": record["kw"],
                                     "pdu_id": record["pdu_id"],
                                     "time": record["when"], "key": record["key"],
                                     "source": record["source"],
                                     "inlet_current": record["inlet_current"],
                                     "inlet_limit": record["inlet_limit"],
                                     "stale": record["stale"]})
                rack["stale"] = rack["stale"] or record["stale"]
                rack["time"] = max(filter(None, [rack["time"], record["time"]]), default=None)

                for circuit in record["circuits"]:
                    entry = breakers.setdefault(record["rack"], {"value": None, "lines": [],
                                                                 "circuits": [], "stale": False})
                    entry["stale"] = entry["stale"] or record["stale"]
                    entry["circuits"].append(circuit)
                    if circuit["percent"] is None:
                        entry["lines"].append(f"{record['name']} {circuit['label']}: no reading")
                        continue
                    entry["value"] = max(entry["value"] or 0, circuit["percent"])
                    note = "  (limit from config)" if circuit["limit_from_config"] else ""
                    entry["lines"].append(
                        f"{record['name']} {circuit['label']}: {circuit['current']:.1f} / "
                        f"{circuit['limit']:g} A  ({circuit['percent']:.0f} %){note}")

            # Store each rack's values once per new reading.
            for number, entry in power.items():
                newest = entry.pop("time")
                if newest is None or self._power_seen.get(number) == newest:
                    continue
                self._power_seen[number] = newest
                if entry["value"] is not None:
                    self.storage.add_sample(f"Rack {number}", "rack_power_kw",
                                            entry["value"], newest)
                top = breakers.get(number, {}).get("value")
                if top is not None:
                    self.storage.add_sample(f"Rack {number}", "rack_breaker_max_pct", top, newest)

            self.rack_power, self.rack_breakers = power, breakers
            # A PDU in maintenance is marked as such on the map, not as a problem.
            health = {rack: [e for e in entries if e["pdu_id"] not in self.alarms.maintenance]
                      for rack, entries in self._direct_down.items()}
            self.pdu_health = {rack: entries for rack, entries in health.items() if entries}
        self.updates.put({"type": "racks"})
        return max((e["value"] for e in breakers.values() if e["value"] is not None),
                   default=None)

    # --------------------------------------------------- PDUs read directly

    def _read_pdu(self, entry: dict):
        """(entry, reading or None, error) for one PDU read directly, by the
        reader of its family. The layout is read once a day, and again after
        any failure."""
        settings = self.config.pdus
        reader = DIRECT_READERS[entry["family"]]
        # APC cards answer for both PDUs of a linked pair: they pick theirs by name.
        by_name = {"name": entry["name"]} if getattr(reader, "BY_NAME", False) else {}
        layout, since = self._layouts.get(entry["id"], (None, 0.0))
        try:
            if layout is None or time.monotonic() - since > 24 * 3600:
                layout = reader.describe(entry["ip"], entry["community"],
                                         timeout=settings["timeout_s"], **by_name)
                self._layouts[entry["id"]] = (layout, time.monotonic())
            return entry, reader.read(entry["ip"], entry["community"], layout,
                                      timeout=settings["timeout_s"]), ""
        except Exception as error:         # one PDU must not stop the others
            self._layouts.pop(entry["id"], None)
            # The PDU is named everywhere it is shown; its address is left out.
            reason = str(error).replace(entry["ip"] + ": ", "").replace(entry["ip"], "PDU")
            return entry, None, f"{type(error).__name__}: {reason}"

    def _pdus_direct(self) -> str:
        """Every Raritan PX2/PX3, PX1, Geist and APC PDU in pdus.toml, read over
        SNMP (read only), 8 at a time: power, breakers with their trip state
        where the PDU has one, temperatures."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._read_pdu, self._direct_entries))
        failed = [(entry, error) for entry, reading, error in results if reading is None]
        if len(failed) == len(results):
            raise raritan_px2.CollectorError(
                f"no PDU answered, first: {failed[0][0]['name']}: {failed[0][1]}")

        now = utc_now()
        default_limit = self.config.pdu_rules["default_breaker_limit_a"]
        records, followed, temps, down, outlets = {}, [], {}, {}, {}
        for entry, reading, error in results:
            name, rack, pdu_id = entry["name"], entry["rack"], entry["id"]
            device = f"Rack {rack}" if rack else "PDUs"
            key = f"pdu_unreachable:{pdu_id}"
            if reading is None:
                if self.alarms.pdu_poll(device, key, name, False, error,
                                        self.config.pdus["alarm_after_failures"]) and rack:
                    reason = error.split(": ", 1)[-1]
                    down.setdefault(rack, []).append({
                        "pdu": name, "pdu_id": pdu_id, "overall": "Not answering",
                        "connectivity": "SNMP",
                        "problem": True, "text": f"{name}: not answering ({reason})"})
                old = self._power_records.get(pdu_id)
                if old is not None:
                    records[pdu_id] = dict(old, stale=True)
                if pdu_id in self._outlets:
                    outlets[pdu_id] = self._outlets[pdu_id]
                continue
            self.alarms.pdu_poll(device, key, name, True)
            if rack is None:
                continue
            outlets[pdu_id] = reading.outlets

            # PX1 PDUs label their breakers CB1, CB2; Power IQ did and the panel
            # call them C1, C2, and so do the alarm and history keys. Keep C.
            labels = [re.sub(r"^CB(\d+)$", r"C\1", b.label) for b in reading.breakers]
            # Geist PDUs give no breaker rating: the configured limit is used
            # and marked.
            breakers = []
            for label, b in zip(labels, reading.breakers):
                limit, from_config = b.rated, False
                if limit is None and default_limit:
                    limit, from_config = default_limit, True
                breakers.append(Breaker(
                    label, b.current, limit,
                    b.current / limit * 100 if b.current is not None and limit else None,
                    from_config))
            breakers = tuple(breakers)
            pdu = PduPower(
                pdu_id=pdu_id, name=name, rack=rack,
                kw=None if reading.watts is None else reading.watts / 1000,
                inlet_current=reading.amps, inlet_limit=reading.inlet_rated,
                breakers=breakers, reading_utc=now)
            # Read just now, so the reading time is now, whatever the PDU's clock says.
            followed.append((pdu, now, False))
            states = {label: b.state for label, b in zip(labels, reading.breakers)}
            records[pdu_id] = self._power_record("PDU", pdu, now, False, states, "%H:%M:%S")
            for breaker in breakers:
                if breaker.percent is not None:
                    self.alarms.evaluate_breaker(
                        device, f"breaker:{pdu_id}:{breaker.label}",
                        f"{name} {breaker.label}", breaker.percent,
                        breaker.current, breaker.limit)
                self.alarms.evaluate_breaker_trip(
                    device, f"breaker_trip:{pdu_id}:{breaker.label}",
                    f"{name} {breaker.label}", states.get(breaker.label))

            # A rack shows its warmest working probe.
            for _serial, label, celsius in reading.temperatures:
                current = temps.get(rack)
                if current is None or (celsius is not None and
                                       (current["value"] is None or celsius > current["value"])):
                    temps[rack] = {"source": f"{name} ({label})", "value": celsius,
                                   "low": None, "high": None,
                                   "time": now.astimezone().strftime("%d.%m.%Y %H:%M:%S"),
                                   "stale": False}

        # A rack whose PDUs did not answer keeps its last temperature, marked stale.
        for entry, _error in failed:
            rack = entry["rack"]
            if rack and rack not in temps and rack in self.pdu_temps:
                temps[rack] = dict(self.pdu_temps[rack], stale=True)
        for rack, entry in temps.items():
            if entry["value"] is not None and not entry["stale"]:
                self.storage.add_sample(f"Rack {rack}", "rack_temp_c", entry["value"], now)
                self.alarms.evaluate_rack(f"Rack {rack}", entry["value"])
        self.pdu_temps = temps

        self._power_records = records
        self._direct_down = down
        self._outlets = outlets
        highest = self._publish_power()
        if self.follower is not None:
            self.follower.observe(followed, now)
        tripped = sum(1 for r in records.values() for c in r["circuits"]
                      if c.get("state") == "open")
        text = (f"{len(results) - len(failed)}/{len(results)} PDUs, "
                f"{sum(len(r['circuits']) for r in records.values())} breakers")
        if highest is not None:
            text += f", highest {highest:.0f} %"
        if tripped:
            text += f", {tripped} TRIPPED"
        if failed:
            text += f", not answering: {', '.join(e['name'] for e, _ in failed[:5])}"
        held = self.alarms.maintenance
        if held:
            text += f", in maintenance: {', '.join(e['name'] for e in held.values())}"
        return text

    # ---------------------------------------------------- PDU maintenance

    def _pdu_named(self, pdu_id: int):
        """(name, rack) of a PDU, from pdus.toml or else its power record."""
        for entry in (self.config.pdus or {}).get("entries", ()):
            if entry["id"] == pdu_id:
                return entry["name"], entry["rack"]
        with self._power_lock:
            record = self._power_records.get(pdu_id)
            if record is not None:
                return record["name"], record["rack"]
        return f"PDU {pdu_id}", None

    def start_maintenance(self, pdu_id: int) -> str:
        """From the panel: the PDU raises no alarms until it is ended; its
        open alarms close now. It is still read and shown."""
        name, rack = self._pdu_named(pdu_id)
        closed = self.alarms.start_maintenance(pdu_id, name, rack)
        log.info("%s put in maintenance from the panel (%d alarm(s) closed)", name, closed)
        self._publish_power()
        return name

    def end_maintenance(self, pdu_id: int) -> str:
        """From the panel: the PDU's alarms work again from its next reading."""
        name, _rack = self._pdu_named(pdu_id)
        if self.alarms.end_maintenance(pdu_id):
            log.info("%s maintenance ended from the panel", name)
        self._publish_power()
        return name

    def racks_view(self) -> dict:
        """Rack readings for the panel.

        A PDU read directly (pdus.toml) wins while it has a value; a
        [[pdu_sensor]] probe fills racks it does not cover.
        """
        merged = dict(self.direct_racks)
        for rack, entry in self.pdu_temps.items():
            current = merged.get(rack)
            if (entry["value"] is not None and not entry["stale"]) or current is None \
                    or (current.get("value") is None and entry["value"] is not None):
                merged[rack] = entry
        return merged

    def _pdu_probes(self) -> str:
        """Read every listed PDU probe, one request each."""
        settings = self.config.pdu_sensors
        now = utc_now()
        racks, failed = {}, []
        for item in self._probe_entries:
            try:
                value = pdu_sensor.read(item["ip"], item["community"], item["probe"],
                                        timeout=settings["timeout_s"],
                                        serial=item.get("serial"))
            except pdu_sensor.CollectorError as error:
                failed.append(f"rack {item['rack']}: {error}")
                racks.setdefault(item["rack"], {"source": "PDU", "value": None,
                                                "stale": False})
                continue
            self.storage.add_sample(f"Rack {item['rack']}", "rack_temp_c", value, now)
            current = racks.get(item["rack"])
            if current is None or current["value"] is None or value > current["value"]:
                racks[item["rack"]] = {
                    "source": "PDU", "value": value, "low": None, "high": None,
                    "time": now.astimezone().strftime("%d.%m.%Y %H:%M"), "stale": False,
                }
        if len(failed) == len(self._probe_entries):
            raise pdu_sensor.CollectorError(f"every probe failed, first: {failed[0]}")

        self.direct_racks = racks
        # Alarm only where no PDU read directly has a value, so one rack is
        # never judged twice from two sources.
        for rack, entry in racks.items():
            own = self.pdu_temps.get(rack) or {}
            if entry["value"] is not None and own.get("value") is None:
                self.alarms.evaluate_rack(f"Rack {rack}", entry["value"])
        self.updates.put({"type": "racks"})
        read = sum(1 for entry in racks.values() if entry["value"] is not None)
        text = f"{read}/{len(self._probe_entries)} probes read"
        if failed:
            text += f", failed: {'; '.join(failed[:3])}"
        return text

    def _ups(self, unit) -> str:
        polling = self.config.polling
        data = ups.read(unit.ip, community=self.config.snmp_community,
                        timeout=polling["ups_timeout_s"],
                        retries=int(polling["ups_retries"]))
        self._store_values(unit.name, data["metrics"])
        self._store_states(unit.name, {"output_source": data["output_source"],
                                       "alarms_present": data["alarms_present"]})
        self.alarms.evaluate_ups(unit.name, data)
        # The alarm table is walked only while the UPS says it has alarms,
        # so a healthy UPS costs no extra requests.
        table = []
        if data["alarms_present"] > 0:
            try:
                table = ups.read_alarms(unit.ip, community=self.config.snmp_community,
                                        timeout=polling["ups_timeout_s"],
                                        retries=int(polling["ups_retries"]))
            except ups.CollectorError:
                table = None
        self.alarms.evaluate_ups_notices(unit.name, table, data["alarms_present"])
        metrics = data["metrics"]
        return (f"{data['output_source']}, "
                f"{metrics['battery_charge_pct']}%, "
                f"{metrics['battery_temp_c']}C, "
                f"load {metrics['output_load_pct_l1']}/"
                f"{metrics['output_load_pct_l2']}/"
                f"{metrics['output_load_pct_l3']}%, "
                + "/".join(f"{metrics.get(f'output_current_a_l{phase}', '--')}"
                           for phase in (1, 2, 3)) + " A")

    def _ups_details(self, unit) -> str:
        """The UPS tab's slower values: names, versions, battery, bypass."""
        polling = self.config.polling
        data = ups.read_details(unit.ip, community=self.config.snmp_community,
                                timeout=polling["ups_timeout_s"],
                                retries=int(polling["ups_retries"]))
        if data["metrics"]:
            self._store_values(unit.name, data["metrics"])
        self._store_states(unit.name, data["states"])
        return f"{len(data['metrics'])} values, {len(data['states'])} texts"

    # --------------------------------------------------------- execution

    def run_job(self, job: Job) -> None:
        """Run one job once, recording timing, errors and reachability."""
        started = time.monotonic()
        with job.lock:
            try:
                detail = job.run()
                ok, error = True, ""
            except Exception as failure:          # collector or parse failure
                detail, ok, error = "", False, f"{type(failure).__name__}: {failure}"
        duration_ms = int((time.monotonic() - started) * 1000)

        job.last_run = utc_now()
        job.last_ok = ok
        job.last_duration_ms = duration_ms
        job.last_error = error
        self.storage.log_poll(job.device, job.name, ok, duration_ms, error or None)

        if ok:
            self.alarms.poll_succeeded(job.device)
            log.info("%-18s %5d ms  %s", job.label, duration_ms, detail)
        else:
            self.alarms.poll_failed(job.device, error)
            log.error("%-18s %5d ms  FAILED %s", job.label, duration_ms, error)

    def run_once(self) -> None:
        """One pass over every job, all devices in parallel."""
        threads = [threading.Thread(target=self.run_job, args=(job,), daemon=True)
                   for job in self.jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _loop(self, job: Job) -> None:
        while not self._stop.is_set():
            self.run_job(job)
            self._stop.wait(job.interval)

    def start(self) -> None:
        log.info("starting %d jobs over %d devices",
                 len(self.jobs), len(self.config.devices))
        self._threads = [
            threading.Thread(target=self._loop, args=(job,), daemon=True,
                             name=job.label)
            for job in self.jobs
        ]
        for thread in self._threads:
            thread.start()
        self._housekeeping = threading.Thread(target=self._purge_loop, daemon=True,
                                              name="retention")
        self._housekeeping.start()
        if self.cabinet is not None:
            threading.Thread(target=self._cabinet_loop, daemon=True,
                             name="cabinet list").start()

    def stop(self) -> None:
        self._stop.set()

    def _purge_loop(self) -> None:
        """Delete rows past their retention period, once a day."""
        while not self._stop.is_set():
            try:
                removed = self.storage.purge(self.config.retention)
                if any(removed.values()):
                    log.info("retention: removed %s", removed)
            except Exception as failure:
                log.error("retention failed: %s", failure)
            self._stop.wait(24 * 3600)

    # -------------------------------------------------------- cabinet list

    def _cabinet_loop(self) -> None:
        """The cabinet list on the share: its time and size are checked every
        check_interval_s (5 s), the workbook is read again only when they
        change. Read only. Not a job: a share that is down is logged and shown
        in the Devices window, never an alarm."""
        interval = self.config.cabinet_list["check_interval_s"]
        last_error = ""
        while not self._stop.is_set():
            try:
                if self.cabinet.check():
                    book = self.cabinet.book
                    devices = [d for sheet in book.racks.values() for d in sheet.devices]
                    unclear = sorted({sheet.rack for sheet in book.racks.values()
                                      for d in sheet.devices if not d.understood})
                    log.info("cabinet list read: %d rack sheets, %d devices%s%s",
                             len(book.racks), len(devices),
                             f", PDU cells not understood on racks {unclear}" if unclear else "",
                             f", rack sheets without the header: {sorted(book.skipped)}"
                             if book.skipped else "")
                    self.updates.put({"type": "racks"})
            except Exception as failure:         # the loop must not stop
                self.cabinet.error = f"{type(failure).__name__}: {failure}"
            if self.cabinet.error != last_error:
                if self.cabinet.error:
                    log.warning("cabinet list not read: %s", self.cabinet.error)
                else:
                    log.info("cabinet list readable again")
                last_error = self.cabinet.error
            self._stop.wait(interval)

    def rack_devices(self, rack: int) -> dict | None:
        """The rack's devices from the cabinet list, each PDU outlet with its
        current where the PDU meters outlets (PX3-5466V, PX1, PX2-3120), else
        the current of the breaker it is on (other PX2/PX3, which say which
        breaker each outlet is on). Geist and APC PDUs give neither. None when
        there is no cabinet list."""
        if self.cabinet is None:
            return None
        sheet = self.cabinet.book.racks.get(rack)
        result = {"found": sheet is not None, "title": sheet.title if sheet else "",
                  "u_max": sheet.u_max if sheet else None,
                  "devices": [], "file_time": self.cabinet.file_time,
                  "error": self.cabinet.error}
        # The rack's circuits, for the table beside the drawing: each PDU's
        # breakers (or phases) as in the rack popup, with the devices on each
        # where the PDU says which breaker an outlet is on.
        result["circuits"] = [dict(c) for c in
                              (self.rack_breakers.get(rack) or {}).get("circuits", [])]
        if sheet is None:
            return result
        # "PDU1" on rack R's sheet = rack R's PDU whose name ends in PDU01;
        # "RACK1 PDU1" = rack 1's.
        pdus = {}
        for entry in self._direct_entries:
            match = re.search(r"PDU\D*0*(\d+)\s*$", entry["name"], re.IGNORECASE)
            if entry["rack"] is not None and match:
                pdus.setdefault((entry["rack"], int(match.group(1))), entry)
        records = self._power_records
        for device in sheet.devices:
            feeds = []
            for on_rack, number, outlet in device.feeds:
                entry = pdus.get((on_rack, number))
                other = "" if on_rack == rack else f"rack {on_rack} "
                # Notes are short: they sit in the window's Amps column.
                feed = {"pdu": entry["name"] if entry else f"{other}PDU{number:02d}",
                        "rack": on_rack, "number": number, "same_rack": on_rack == rack,
                        "outlet": outlet, "amps": None, "breaker": None,
                        "breaker_amps": None, "note": ""}
                if entry is None:
                    feed["note"] = "not read"      # not in pdus.toml (no address)
                else:
                    readings = {o.number: o for o in self._outlets.get(entry["id"], ())}
                    record = records.get(entry["id"]) or {}
                    found = readings.get(outlet)
                    if found is not None:
                        feed["amps"], feed["breaker"] = found.amps, found.breaker
                        circuit = next((c for c in record.get("circuits", [])
                                        if c["label"] == found.breaker), None)
                        if circuit is not None:
                            feed["breaker_amps"] = circuit["current"]
                    elif readings:
                        feed["note"] = "no such outlet"
                    elif entry["family"] in ("geist", "apc"):
                        feed["note"] = "no meter"
                    else:
                        feed["note"] = "no reading yet"
                    if record.get("stale"):
                        feed["note"] = "PDU down"
                feeds.append(feed)
            result["devices"].append({
                "name": device.name, "u_top": device.u_top, "u_bottom": device.u_bottom,
                "pdu_text": device.pdu_text, "understood": device.understood,
                "feeds": feeds})
        return result

    def status_lines(self) -> list:
        """One line per device for the console summary."""
        lines = []
        for device in self.config.devices:
            values = self.values.get(device.name, {})
            updated = values.get("updated_utc")
            age = f"{(utc_now() - updated).total_seconds():.0f}s ago" if updated else "no data"
            readings = ", ".join(
                f"{metric}={value:g}"
                for metric, value in values.items()
                if metric != "updated_utc"
            )
            failures = self.alarms.failure_count(device.name)
            warning = f"  [{failures} failed polls]" if failures else ""
            lines.append(f"  {device.name:<7} {age:>9}  {readings}{warning}")
        return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="dcmonitor")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true",
                        help="poll every device once, then exit")
    parser.add_argument("--console", action="store_true",
                        help="keep polling and print to the terminal, no window")
    parser.add_argument("--status-every", type=int, default=60,
                        help="seconds between console summaries (default 60)")
    args = parser.parse_args()

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as error:
        print(f"config error: {error}")
        return 1

    keep_days = int(config.retention["poll_log_days"])
    setup_logging(config.log_folder, keep_days, to_console=True)

    monitor = Monitor(config)
    try:
        if not args.once and not args.console:
            from dcmonitor.panel import run as run_panel
            run_panel(monitor)
            return 0

        if args.once:
            started = time.monotonic()
            monitor.run_once()
            print(f"\none pass finished in {time.monotonic() - started:.1f}s")
            print("current values:")
            print("\n".join(monitor.status_lines()))
            active = monitor.storage.active_alarms()
            print(f"active alarms: {len(active)}")
            for alarm in active:
                print(f"  {alarm['device']:<7} {alarm['description']}")
            return 0

        monitor.start()
        print("running, press Ctrl+C to stop")
        while True:
            time.sleep(args.status_every)
            print(f"\n--- {datetime.now(timezone.utc).astimezone():%H:%M:%S}")
            print("\n".join(monitor.status_lines()))
            active = monitor.storage.active_alarms()
            print(f"active alarms: {len(active)}")
            for alarm in active:
                print(f"  {alarm['device']:<7} {alarm['description']}")
    except KeyboardInterrupt:
        print("\nstopping")
        return 0
    finally:
        # Stop the workers first, then close the database. Closing it while
        # threads are mid-poll is what produced a wall of tracebacks before.
        monitor.stop()
        time.sleep(0.2)
        monitor.storage.close()


if __name__ == "__main__":
    sys.exit(main())

"""Circuit follower: a circuit that normally carries load and drops to zero
or gives no reading.

Each new reading of a PDU (every 30 s, read directly) is stored per circuit
(C1, C2 ... or the lines L1..L3 of PDUs without breakers) and per inlet, as
readings with the metric "amps:<pdu id>:<label>" (kept circuit_history_days,
30 by default). A circuit's normal is its average over follow_normal_days (7)
before the last two hours, once at least MIN_HOURS hours exist. Each new
reading is then judged:

    dropped to zero  normal >= follow_drop_min_a (0.1 A), i.e. the circuit
                     carries load and is watched, and now zero (below
                     ZERO_A, shown as 0.0 A), on follow_readings (2) new
                     readings in a row
                     -> real alarm (kind circuit_drop): a trip, or lost load.
                     On Geist and APC PDUs, which report no trip state, this
                     is the only sign of a tripped breaker.
    no reading       normal >= follow_drop_min_a and the PDU gives no value
                     for the circuit, on follow_readings new readings in a
                     row -> notice (kind circuit_change)

A PDU without a new reading for follow_stale_after_s (3 hours) raises a "no
new reading" notice (kind circuit_change).

The notices for a circuit consuming more or less than its normal were
removed on 5 Oct 2026 (too noisy); open ones are closed once at start.

While a message is open the circuit's normal is frozen (follow_state table),
so a circuit that stays at 0 A does not slowly become "normal" and silence
itself. accept() takes the value now as the new normal (a customer removed
equipment, say): it closes the alarm and uses that value for the same
follow_normal_days, after which the average has caught up.

A PDU in maintenance (alarms.py) still has its readings stored, but is not
judged, so it raises nothing.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from .storage import FOLLOW_METRIC_PREFIX, hour_key, to_text
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.storage import FOLLOW_METRIC_PREFIX, hour_key, to_text

KIND_DROP = "circuit_drop"          # real alarm
KIND_CHANGE = "circuit_change"      # notice: no reading
MIN_HOURS = 6                        # hours of history before a normal is trusted
ZERO_A = 0.05                        # below this a circuit reads zero (shown as 0.0 A).
                                     # Not exactly 0: an empty Geist inlet reads 0.03 A
                                     # (the PDU's own draw, seen 2-9 Oct 2026).
RULES = ("drop", "noread")           # alarm keys follow_<rule>:<pdu id>:<label>
REMOVED_RULES = ("more", "less")


@dataclass(frozen=True)
class Breaker:
    """One circuit of a PDU: a breaker (C1, C2 ...) or, on PDUs without
    breakers, a phase (L1, L2, L3)."""
    label: str
    current: float | None      # amps
    limit: float | None        # amps
    percent: float | None      # current as a share of the limit
    limit_from_config: bool = False   # True when the PDU gives no rating


@dataclass(frozen=True)
class PduPower:
    """One PDU's reading, as main.py builds it from a direct read."""
    pdu_id: int                # the id in pdus.toml (Power IQ's id, kept as the key)
    name: str
    rack: int | None
    kw: float | None
    inlet_current: float | None
    inlet_limit: float | None
    breakers: tuple
    reading_utc: datetime | None


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Follower:
    def __init__(self, storage, alarms, settings: dict):
        self.storage = storage
        self.alarms = alarms
        self.drop_min = settings.get("follow_drop_min_a", 0.1)
        self.needed = int(settings.get("follow_readings", 2))
        self.window = timedelta(days=settings.get("follow_normal_days", 7))
        self.stale_after = settings.get("follow_stale_after_s", 10800)
        self.normals = {}        # key -> normal amps, for the popup
        self._latest = {}        # key -> (device, label, current)
        self._strikes = {}       # (key, rule) -> new readings in a row
        self._seen = {}          # pdu id -> reading time last judged
        self._rolling = (None, {})   # ((start, end), averages): the window only
                                     # moves once an hour, so one query per hour
        self._lock = threading.Lock()
        self._close_removed_notices()

    def _close_removed_notices(self) -> None:
        """The consumption notices were removed (5 Oct 2026). Any still open
        in the database would never close, so they are closed once here."""
        for alarm in self.storage.active_alarms():
            if alarm["key"].startswith(tuple(f"follow_{rule}:" for rule in REMOVED_RULES)):
                self.alarms._clear(alarm["device"], alarm["key"],
                                   "Consumption notices are no longer used")

    @staticmethod
    def key_of(pdu_id: int, label: str) -> str:
        return f"{pdu_id}:{label}"

    def _normal(self, key: str, rolling: dict, states: dict, now) -> float | None:
        state = states.get(key)
        if state and state["mode"] == "frozen":
            return state["normal"]
        if state and state["mode"] == "accepted":
            if now - _parse(state["since_utc"]) < self.window:
                return state["normal"]
            self.storage.delete_follow_state(key)
        average, hours = rolling.get(FOLLOW_METRIC_PREFIX + key, (None, 0))
        return average if hours >= MIN_HOURS else None

    # -------------------------------------------------------------- judge

    def observe(self, pdus: list, now, explained: set = frozenset()) -> None:
        """pdus: [(PduPower, reading time or None, stale)] from one PDU round.
        explained: PDU ids whose gap is already explained elsewhere; no "no
        new reading" notice for them."""
        with self._lock:
            self._observe(pdus, now, explained)

    def _observe(self, pdus, now, explained) -> None:
        start = hour_key(now - timedelta(hours=2) - self.window)
        end = hour_key(now - timedelta(hours=2))
        if self._rolling[0] != (start, end):
            self._rolling = ((start, end),
                             self.storage.averages(FOLLOW_METRIC_PREFIX, start, end))
        rolling = self._rolling[1]
        states = self.storage.follow_states()
        active = {alarm["key"] for alarm in self.storage.active_alarms()
                  if alarm["key"].startswith("follow_")}

        for pdu, reading_utc, _stale in pdus:
            if pdu.rack is None:
                continue
            device = f"Rack {pdu.rack}"
            # The inlet is always followed; one that never had a reading has
            # no normal and is never judged.
            items = [(breaker.label, f"{pdu.name} {breaker.label}", breaker.current)
                     for breaker in pdu.breakers]
            items.append(("inlet", f"{pdu.name} inlet", pdu.inlet_current))
            # A PDU in maintenance: its readings are kept, nothing is judged.
            muted = pdu.pdu_id in self.alarms.maintenance

            # A PDU that stopped giving new readings.
            stale_key = self._alarm_key("stale", f"{pdu.pdu_id}:pdu")
            when = reading_utc.astimezone().strftime("%d.%m %H:%M") if reading_utc else "never"
            behind = (reading_utc is not None
                      and (now - reading_utc).total_seconds() > self.stale_after)
            if muted:
                pass
            elif behind and pdu.pdu_id not in explained:
                self.alarms._raise(device, KIND_CHANGE, stale_key,
                                   f"{pdu.name}: no new reading since {when}")
            else:
                self.alarms._clear(device, stale_key,
                                   f"{pdu.name}: the gap is explained" if behind
                                   else f"{pdu.name}: readings again")

            new = (reading_utc is not None and not behind
                   and self._seen.get(pdu.pdu_id) != reading_utc)
            if new:
                self._seen[pdu.pdu_id] = reading_utc
                for label, _name, current in items:
                    if current is not None:
                        self.storage.add_sample(device, FOLLOW_METRIC_PREFIX
                                                + self.key_of(pdu.pdu_id, label),
                                                current, reading_utc)

            for label, name, current in items:
                key = self.key_of(pdu.pdu_id, label)
                normal = self._normal(key, rolling, states, now)
                self.normals[key] = normal
                self._latest[key] = (device, name, current)
                if not new or normal is None or muted:
                    continue
                self._judge(device, key, name, normal, current, states, active)

    def _alarm_key(self, rule: str, key: str) -> str:
        return f"follow_{rule}:{key}"

    def _judge(self, device, key, name, normal, current, states, active) -> None:
        open_rules = {rule for rule in RULES if self._alarm_key(rule, key) in active}
        text = f"{name}: normally {normal:.1f} A, now "
        expected = self.drop_min is not None and normal >= self.drop_min
        if current is None:
            # No value: the drop alarm, if open, stays as it is.
            verdict = {"noread": (expected, text + "no reading")}
        else:
            now_text = f"{current:.1f} A"
            dropped = expected and current < ZERO_A
            if "drop" in open_rules and not dropped:
                # Once open, it closes only when the circuit is clearly back:
                # half its normal, at most 0.5 A.
                dropped = current < max(ZERO_A, min(0.5, normal * 0.5))
            verdict = {"drop": (dropped, text + now_text + ", dropped to zero"
                                if dropped else f"{name}: back to {now_text}"),
                       "noread": (False, f"{name}: reading again, {now_text}")}

        for rule, (condition, description) in verdict.items():
            alarm_key = self._alarm_key(rule, key)
            if rule in open_rules:
                if not condition:
                    self.alarms._clear(device, alarm_key, description)
                    open_rules.discard(rule)
                self._strikes.pop((key, rule), None)
                continue
            if not condition:
                self._strikes.pop((key, rule), None)
                continue
            self._strikes[(key, rule)] = self._strikes.get((key, rule), 0) + 1
            if self._strikes[(key, rule)] >= self.needed:
                kind = KIND_DROP if rule == "drop" else KIND_CHANGE
                self.alarms._raise(device, kind, alarm_key, description)
                open_rules.add(rule)

        # Freeze the normal while anything is open; unfreeze when all closed.
        state = states.get(key)
        if open_rules and not (state and state["mode"] == "frozen"):
            self.storage.set_follow_state(key, normal, "frozen", to_text(datetime.now(timezone.utc)))
        elif not open_rules and state and state["mode"] == "frozen":
            self.storage.delete_follow_state(key)

    # ------------------------------------------------------------- accept

    def accept(self, key: str) -> str:
        """Take the circuit's value now as its new normal; close its messages.
        Returns a short text for the log."""
        with self._lock:
            device, name, current = self._latest.get(key, (None, key, None))
            if device is None:
                for alarm in self.storage.active_alarms():
                    if alarm["key"] in {self._alarm_key(rule, key) for rule in RULES}:
                        device = alarm["device"]
            value = current if current is not None else 0.0
            for rule in RULES:
                if device:
                    self.alarms._clear(device, self._alarm_key(rule, key),
                                       f"{name}: {value:.1f} A accepted as the new normal")
                self._strikes.pop((key, rule), None)
            self.storage.set_follow_state(key, value, "accepted",
                                          to_text(datetime.now(timezone.utc)))
            self.normals[key] = value
            return f"{name}: {value:.1f} A accepted as the new normal"

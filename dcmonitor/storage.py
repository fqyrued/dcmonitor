"""SQLite storage for dcmonitor.

All database access goes through this module. One connection is used, guarded
by a lock, so collector threads never touch SQLite at the same time.

Tables:
    readings      hourly min / average / max per device and metric
    alarms        one row per alarm, with raised and cleared times
    unit_events   event log entries read from the units
    poll_log      one row per poll attempt, for reachability and timing
    follow_state  circuit follower normals (frozen or accepted)
    pdu_maintenance  PDUs put in maintenance from the panel, with history

All times are stored in UTC as "YYYY-MM-DDTHH:MM:SSZ" strings, which sort
correctly as text. The panel converts them to local time for display.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    hour_utc   TEXT NOT NULL,          -- "YYYY-MM-DDTHH"
    device     TEXT NOT NULL,
    metric     TEXT NOT NULL,
    min_value  REAL NOT NULL,
    max_value  REAL NOT NULL,
    sum_value  REAL NOT NULL,
    samples    INTEGER NOT NULL,
    PRIMARY KEY (hour_utc, device, metric)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY,
    device      TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- unit_event, temperature, ups_battery...
    key         TEXT NOT NULL,         -- identifies the same alarm over time
    description TEXT NOT NULL,
    raised_utc  TEXT NOT NULL,
    cleared_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarms_open ON alarms (cleared_utc, device);
CREATE INDEX IF NOT EXISTS idx_alarms_time ON alarms (raised_utc);

CREATE TABLE IF NOT EXISTS unit_events (
    device      TEXT NOT NULL,
    event_utc   TEXT NOT NULL,
    event_id    INTEGER NOT NULL,      -- -1 when the unit gives no ID
    name        TEXT NOT NULL,
    action      TEXT NOT NULL,         -- Start, Stop, or a type from old units
    description TEXT NOT NULL,
    source      TEXT NOT NULL,         -- unit_log or detected
    var1        TEXT,
    var2        TEXT,
    PRIMARY KEY (device, event_utc, event_id, name, action)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_events_time ON unit_events (event_utc);

CREATE TABLE IF NOT EXISTS poll_log (
    id          INTEGER PRIMARY KEY,
    ts_utc      TEXT NOT NULL,
    device      TEXT NOT NULL,
    job         TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_time ON poll_log (ts_utc);
CREATE INDEX IF NOT EXISTS idx_poll_device ON poll_log (device, ts_utc);

-- Circuit follower: a circuit's normal while a change is open (frozen), or
-- the value the user accepted as the new normal (accepted, for 24 hours).
CREATE TABLE IF NOT EXISTS follow_state (
    key        TEXT PRIMARY KEY,       -- "<pdu id>:<C1|L1|inlet>"
    normal     REAL NOT NULL,          -- amps
    mode       TEXT NOT NULL,          -- frozen or accepted
    since_utc  TEXT NOT NULL
) WITHOUT ROWID;

-- PDUs a technician put in maintenance from the panel: while a row has no
-- ended_utc, the PDU's alarms are closed and none are raised.
CREATE TABLE IF NOT EXISTS pdu_maintenance (
    id         INTEGER PRIMARY KEY,
    pdu_id     INTEGER NOT NULL,       -- Power IQ id, as in pdus.toml
    name       TEXT NOT NULL,
    rack       INTEGER,
    since_utc  TEXT NOT NULL,
    ended_utc  TEXT
);
"""

# Readings whose metric starts with this are per-circuit and per-inlet amps
# for the circuit follower; they are kept for a shorter time (see purge).
FOLLOW_METRIC_PREFIX = "amps:"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_text(moment: datetime) -> str:
    """UTC timestamp as a sortable string."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


class Storage:
    """Thread-safe SQLite access. Create once, share between threads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # WAL keeps reads (the panel) from blocking writes (the collectors).
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        """Close the database.

        Collector threads may still be finishing a poll when this is called,
        so every method below returns quietly once this flag is set rather
        than raising "Cannot operate on a closed database" in each thread.
        """
        with self._lock:
            self._closed = True
            self._db.close()

    # ------------------------------------------------------------ readings

    def add_sample(self, device: str, metric: str, value: float,
                   moment: datetime | None = None) -> None:
        """Fold one measurement into its hour's min / max / average row.

        Writing on every sample rather than once an hour means nothing is lost
        if the program stops mid-hour, while still keeping one row per hour.
        """
        moment = moment or utc_now()
        if self._closed:
            return
        with self._lock:
            self._db.execute(
                """
                INSERT INTO readings
                    (hour_utc, device, metric, min_value, max_value, sum_value, samples)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (hour_utc, device, metric) DO UPDATE SET
                    min_value = MIN(min_value, excluded.min_value),
                    max_value = MAX(max_value, excluded.max_value),
                    sum_value = sum_value + excluded.sum_value,
                    samples   = samples + 1
                """,
                (hour_key(moment), device, metric, value, value, value),
            )
            self._db.commit()

    def history_between(self, device: str, metric: str,
                        start_hour: str, end_hour: str) -> list:
        """Hourly rows between two hour keys ("YYYY-MM-DDTHH"), inclusive."""
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT hour_utc,
                       min_value,
                       sum_value / samples AS avg_value,
                       max_value,
                       samples
                FROM readings
                WHERE device = ? AND metric = ?
                  AND hour_utc >= ? AND hour_utc <= ?
                ORDER BY hour_utc
                """,
                (device, metric, start_hour, end_hour),
            ).fetchall()
        return [dict(row) for row in rows]

    def first_reading_hour(self) -> str | None:
        """The oldest hour key in readings, or None when nothing is stored.

        hour_utc leads the primary key, so this is one index lookup.
        """
        if self._closed:
            return None
        with self._lock:
            row = self._db.execute("SELECT MIN(hour_utc) FROM readings").fetchone()
        return row[0] if row else None

    def averages(self, prefix: str, start_hour: str, end_hour: str) -> dict:
        """Average and number of hours per metric starting with prefix, over
        hour keys start_hour..end_hour inclusive: {metric: (average, hours)}.
        One query for all circuits, for the follower."""
        if self._closed:
            return {}
        with self._lock:
            rows = self._db.execute(
                """
                SELECT metric, SUM(sum_value) / SUM(samples), COUNT(*)
                FROM readings
                WHERE hour_utc >= ? AND hour_utc <= ? AND metric LIKE ?
                GROUP BY metric
                """,
                (start_hour, end_hour, prefix + "%"),
            ).fetchall()
        return {metric: (average, hours) for metric, average, hours in rows}

    # ------------------------------------------------------- follow state

    def follow_states(self) -> dict:
        """{key: {"normal", "mode", "since_utc"}}"""
        if self._closed:
            return {}
        with self._lock:
            rows = self._db.execute(
                "SELECT key, normal, mode, since_utc FROM follow_state").fetchall()
        return {row[0]: {"normal": row[1], "mode": row[2], "since_utc": row[3]} for row in rows}

    def set_follow_state(self, key: str, normal: float, mode: str, since_utc: str) -> None:
        if self._closed:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO follow_state (key, normal, mode, since_utc) "
                "VALUES (?, ?, ?, ?)", (key, normal, mode, since_utc))
            self._db.commit()

    def delete_follow_state(self, key: str) -> None:
        if self._closed:
            return
        with self._lock:
            self._db.execute("DELETE FROM follow_state WHERE key = ?", (key,))
            self._db.commit()

    # ---------------------------------------------------- PDU maintenance

    def maintenance(self) -> dict:
        """PDUs in maintenance now: {pdu id: {"name", "rack", "since_utc"}}."""
        if self._closed:
            return {}
        with self._lock:
            rows = self._db.execute(
                "SELECT pdu_id, name, rack, since_utc FROM pdu_maintenance "
                "WHERE ended_utc IS NULL ORDER BY since_utc").fetchall()
        return {row[0]: {"name": row[1], "rack": row[2], "since_utc": row[3]} for row in rows}

    def start_maintenance(self, pdu_id: int, name: str, rack: int | None,
                          moment: datetime | None = None) -> bool:
        """Put a PDU in maintenance. False if it already is."""
        moment = moment or utc_now()
        if self._closed:
            return False
        with self._lock:
            if self._db.execute("SELECT 1 FROM pdu_maintenance WHERE pdu_id = ? "
                                "AND ended_utc IS NULL", (pdu_id,)).fetchone():
                return False
            self._db.execute(
                "INSERT INTO pdu_maintenance (pdu_id, name, rack, since_utc) VALUES (?, ?, ?, ?)",
                (pdu_id, name, rack, to_text(moment)))
            self._db.commit()
        return True

    def end_maintenance(self, pdu_id: int, moment: datetime | None = None) -> bool:
        """End a PDU's maintenance. False if it was not in maintenance."""
        moment = moment or utc_now()
        if self._closed:
            return False
        with self._lock:
            cursor = self._db.execute(
                "UPDATE pdu_maintenance SET ended_utc = ? WHERE pdu_id = ? AND ended_utc IS NULL",
                (to_text(moment), pdu_id))
            self._db.commit()
        return cursor.rowcount > 0

    def history(self, device: str, metric: str, days: int) -> list:
        """Hourly rows for a graph: hour, min, avg, max."""
        since = hour_key(utc_now() - timedelta(days=days))
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT hour_utc,
                       min_value,
                       sum_value / samples AS avg_value,
                       max_value,
                       samples
                FROM readings
                WHERE device = ? AND metric = ? AND hour_utc >= ?
                ORDER BY hour_utc
                """,
                (device, metric, since),
            ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- alarms

    def raise_alarm(self, device: str, kind: str, key: str, description: str,
                    moment: datetime | None = None) -> bool:
        """Open an alarm if that key is not already open. True if it is new."""
        moment = moment or utc_now()
        if self._closed:
            return False
        with self._lock:
            existing = self._db.execute(
                "SELECT id FROM alarms WHERE device = ? AND key = ? AND cleared_utc IS NULL",
                (device, key),
            ).fetchone()
            if existing:
                return False
            self._db.execute(
                """
                INSERT INTO alarms (device, kind, key, description, raised_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device, kind, key, description, to_text(moment)),
            )
            self._db.commit()
        return True

    def clear_alarm(self, device: str, key: str,
                    moment: datetime | None = None) -> bool:
        """Close an open alarm. True if one was open."""
        moment = moment or utc_now()
        if self._closed:
            return False
        with self._lock:
            cursor = self._db.execute(
                """
                UPDATE alarms SET cleared_utc = ?
                WHERE device = ? AND key = ? AND cleared_utc IS NULL
                """,
                (to_text(moment), device, key),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def active_alarms(self) -> list:
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, device, kind, key, description, raised_utc
                FROM alarms WHERE cleared_utc IS NULL
                ORDER BY raised_utc
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def alarm_history(self, days: int, device: str | None = None) -> list:
        since = to_text(utc_now() - timedelta(days=days))
        query = """
            SELECT id, device, kind, description, raised_utc, cleared_utc
            FROM alarms WHERE raised_utc >= ?
        """
        params: list = [since]
        if device:
            query += " AND device = ?"
            params.append(device)
        query += " ORDER BY raised_utc DESC"
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------- unit events

    def add_unit_event(self, device: str, event_utc: str, event_id: int,
                       name: str, action: str, description: str,
                       source: str = "unit_log",
                       var1: str | None = None, var2: str | None = None) -> bool:
        """Store one event. Returns False if it was already stored.

        The unit keeps its last 200 entries and we re-read the whole list every
        poll, so the same entry arrives many times. The primary key drops the
        repeats.
        """
        if self._closed:
            return False
        with self._lock:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO unit_events
                    (device, event_utc, event_id, name, action, description,
                     source, var1, var2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (device, event_utc, event_id, name, action, description,
                 source, var1, var2),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def recent_events(self, days: int, device: str | None = None) -> list:
        since = to_text(utc_now() - timedelta(days=days))
        query = """
            SELECT device, event_utc, event_id, name, action, description, source
            FROM unit_events WHERE event_utc >= ?
        """
        params: list = [since]
        if device:
            query += " AND device = ?"
            params.append(device)
        query += " ORDER BY event_utc DESC"
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def newest_event_time(self, device: str) -> str | None:
        """Newest stored event for a device, used to skip old log entries."""
        if self._closed:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(event_utc) AS newest FROM unit_events WHERE device = ?",
                (device,),
            ).fetchone()
        return row["newest"] if row else None

    # ------------------------------------------------------------ poll log

    def log_poll(self, device: str, job: str, ok: bool, duration_ms: int,
                 error: str | None = None, moment: datetime | None = None) -> None:
        moment = moment or utc_now()
        if self._closed:
            return
        with self._lock:
            self._db.execute(
                """
                INSERT INTO poll_log (ts_utc, device, job, ok, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (to_text(moment), device, job, 1 if ok else 0, duration_ms, error),
            )
            self._db.commit()

    def poll_summary(self, hours: int = 24) -> list:
        since = to_text(utc_now() - timedelta(hours=hours))
        if self._closed:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT device, job,
                       COUNT(*) AS attempts,
                       SUM(ok) AS successes,
                       AVG(duration_ms) AS avg_ms,
                       MAX(duration_ms) AS max_ms
                FROM poll_log WHERE ts_utc >= ?
                GROUP BY device, job
                ORDER BY device, job
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- retention

    def purge(self, retention: dict) -> dict:
        """Delete rows past their retention period. Returns rows removed."""
        now = utc_now()
        removed = {}
        if self._closed:
            return removed
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM readings WHERE hour_utc < ?",
                (hour_key(now - timedelta(days=retention["readings_days"])),),
            )
            removed["readings"] = cursor.rowcount
            # Per-circuit amps for the follower: only a few weeks are kept.
            cursor = self._db.execute(
                "DELETE FROM readings WHERE metric LIKE ? AND hour_utc < ?",
                (FOLLOW_METRIC_PREFIX + "%", hour_key(
                    now - timedelta(days=retention.get("circuit_history_days", 30)))),
            )
            removed["circuit_history"] = cursor.rowcount
            cursor = self._db.execute(
                "DELETE FROM alarms WHERE raised_utc < ? AND cleared_utc IS NOT NULL",
                (to_text(now - timedelta(days=retention["alarms_days"])),),
            )
            removed["alarms"] = cursor.rowcount
            cursor = self._db.execute(
                "DELETE FROM pdu_maintenance WHERE ended_utc IS NOT NULL AND ended_utc < ?",
                (to_text(now - timedelta(days=retention["alarms_days"])),),
            )
            removed["maintenance"] = cursor.rowcount
            cursor = self._db.execute(
                "DELETE FROM unit_events WHERE event_utc < ?",
                (to_text(now - timedelta(days=retention["unit_events_days"])),),
            )
            removed["unit_events"] = cursor.rowcount
            cursor = self._db.execute(
                "DELETE FROM poll_log WHERE ts_utc < ?",
                (to_text(now - timedelta(days=retention["poll_log_days"])),),
            )
            removed["poll_log"] = cursor.rowcount
            self._db.commit()
        return removed


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        store = Storage(Path(folder) / "test.db")
        store.add_sample("AC-3", "return_temp_c", 22.4)
        store.add_sample("AC-3", "return_temp_c", 23.8)
        print("history:", store.history("AC-3", "return_temp_c", days=1))
        print("new alarm:", store.raise_alarm("AC-3", "temperature", "temp_high",
                                              "Return temperature above setpoint + 3 C"))
        print("repeat:", store.raise_alarm("AC-3", "temperature", "temp_high", "same"))
        print("active:", store.active_alarms())
        print("cleared:", store.clear_alarm("AC-3", "temp_high"))
        print("stored:", store.add_unit_event("AC-7", "2026-09-17T11:59:43Z", 155,
                                              "Al_UnitOn.Active", "Start", "Unit On"))
        print("duplicate:", store.add_unit_event("AC-7", "2026-09-17T11:59:43Z", 155,
                                                 "Al_UnitOn.Active", "Start", "Unit On"))
        store.log_poll("UPS 1", "snmp", True, 8)
        print("polls:", store.poll_summary())
        store.close()

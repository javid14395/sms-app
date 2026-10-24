"""SQLite persistence with isolated connections and auditable batch history."""

from __future__ import annotations

import sqlite3
import threading
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .validation import ValidationError, normalize_phone, validate_message

SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryStore:
    def __init__(self, path: str | Path = "sms_history_pro.db", *, initialize: bool = True):
        self.path = Path(path)
        self._local = threading.local()
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One connection per calling thread; transaction scope always explicit."""
        connection = getattr(self._local, "connection", None)
        created = connection is None
        if created:
            connection = self._connect()
            self._local.connection = connection
        try:
            yield connection
        finally:
            if created:
                connection.close()
                self._local.connection = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS contacts (
                        phone TEXT PRIMARY KEY,
                        consented INTEGER NOT NULL CHECK (consented = 1),
                        consent_source TEXT NOT NULL,
                        consented_at TEXT NOT NULL,
                        suppressed INTEGER NOT NULL DEFAULT 0 CHECK (suppressed IN (0,1)),
                        suppressed_at TEXT,
                        suppression_note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS suppression_list (
                        phone TEXT PRIMARY KEY,
                        suppressed_at TEXT NOT NULL,
                        note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS batches (
                        id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        finished_at TEXT,
                        device_serial TEXT NOT NULL,
                        message TEXT NOT NULL CHECK (length(message) <= 160),
                        state TEXT NOT NULL CHECK (state IN ('pending','running','paused','stopping','completed','failed')),
                        total INTEGER NOT NULL CHECK (total >= 0),
                        delay_seconds REAL NOT NULL CHECK (delay_seconds >= 0),
                        cooldown_every INTEGER NOT NULL CHECK (cooldown_every >= 0),
                        cooldown_seconds REAL NOT NULL CHECK (cooldown_seconds >= 0),
                        note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS batch_events (
                        id INTEGER PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES batches(id),
                        occurred_at TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        detail TEXT
                    );
                    CREATE TABLE IF NOT EXISTS message_history (
                        id INTEGER PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES batches(id),
                        phone TEXT NOT NULL REFERENCES contacts(phone),
                        message TEXT NOT NULL CHECK (length(message) <= 160),
                        status TEXT NOT NULL CHECK (status IN ('pending','sent','failed')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        device_serial TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
                        error_detail TEXT,
                        verification TEXT,
                        UNIQUE(batch_id, phone)
                    );
                    CREATE INDEX IF NOT EXISTS idx_history_batch_status ON message_history(batch_id, status);
                    CREATE INDEX IF NOT EXISTS idx_history_created ON message_history(created_at);
                    CREATE INDEX IF NOT EXISTS idx_contacts_suppressed ON contacts(suppressed);
                    CREATE INDEX IF NOT EXISTS idx_suppression_date ON suppression_list(suppressed_at);
                    CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id);
                """)
                current = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
                if current is None:
                    db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
                elif current["version"] > SCHEMA_VERSION:
                    raise RuntimeError("Database schema is newer than this application.")
                elif current["version"] == 1:
                    # v2 adds a durable batch state-transition audit trail.
                    db.execute("""CREATE TABLE IF NOT EXISTS batch_events (
                        id INTEGER PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES batches(id),
                        occurred_at TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        detail TEXT
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id)")
                    db.execute("""CREATE TABLE IF NOT EXISTS suppression_list (
                        phone TEXT PRIMARY KEY,
                        suppressed_at TEXT NOT NULL,
                        note TEXT
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS idx_suppression_date ON suppression_list(suppressed_at)")
                    db.execute("UPDATE schema_meta SET version=? WHERE version=1", (SCHEMA_VERSION,))
                elif current["version"] < SCHEMA_VERSION:
                    raise RuntimeError(f"No migration path from schema {current['version']}.")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def import_consented(self, phones: list[str], source: str) -> tuple[int, int]:
        phones = [self._normalized_phone(phone) for phone in phones]
        now = utc_now()
        inserted = suppressed = 0
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for phone in phones:
                    existing = db.execute("SELECT suppressed FROM contacts WHERE phone=?", (phone,)).fetchone()
                    listed = db.execute("SELECT 1 FROM suppression_list WHERE phone=?", (phone,)).fetchone()
                    if listed or (existing and existing["suppressed"]):
                        suppressed += 1
                    elif existing:
                        continue
                    else:
                        db.execute("INSERT INTO contacts(phone,consented,consent_source,consented_at) VALUES (?,1,?,?)",
                                   (phone, source[:300], now))
                        inserted += 1
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return inserted, suppressed

    def suppress(self, phone: str, note: str = "User requested opt-out") -> None:
        phone = self._normalized_phone(phone)
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO suppression_list(phone,suppressed_at,note) VALUES (?,?,?)
                    ON CONFLICT(phone) DO UPDATE SET suppressed_at=excluded.suppressed_at,note=excluded.note""",
                    (phone, now, note[:300]))
                db.execute("""UPDATE contacts SET suppressed=1,suppressed_at=?,suppression_note=?
                    WHERE phone=?""", (now, note[:300], phone))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def eligible(self, phones: list[str]) -> list[str]:
        if not phones:
            return []
        with self.connection() as db:
            result = []
            for phone in phones:
                listed = db.execute("SELECT 1 FROM suppression_list WHERE phone=?", (phone,)).fetchone()
                row = db.execute("SELECT consented,suppressed FROM contacts WHERE phone=?", (phone,)).fetchone()
                if not listed and row and row["consented"] and not row["suppressed"]:
                    result.append(phone)
            return result

    def create_batch(self, serial: str, message: str, phones: list[str], delay: float,
                     cooldown_every: int, cooldown_seconds: float) -> int:
        message, phones, delay, cooldown_every, cooldown_seconds = self._prepare_batch(
            serial, message, phones, delay, cooldown_every, cooldown_seconds)
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_eligible(db, phones)
                batch_id = self._insert_batch(db, serial, message, phones, delay, cooldown_every, cooldown_seconds, now)
                db.execute("COMMIT")
                return batch_id
            except Exception:
                db.execute("ROLLBACK")
                raise

    def create_batches(self, serial: str, message: str, recipient_batches: list[list[str]],
                       delay: float, cooldown_every: int, cooldown_seconds: float) -> list[int]:
        """Atomically create all size-limited batches for one user-confirmed run."""
        if not recipient_batches:
            raise ValueError("No batches were supplied.")
        prepared = [self._prepare_batch(serial, message, phones, delay, cooldown_every, cooldown_seconds)
                    for phones in recipient_batches]
        if any(item[0] != prepared[0][0] or item[2:] != prepared[0][2:] for item in prepared):
            raise ValueError("Batch settings must be consistent within one run.")
        all_phones = [phone for item in prepared for phone in item[1]]
        if len(all_phones) != len(set(all_phones)):
            raise ValueError("A run cannot contain duplicate recipient numbers.")
        now = utc_now()
        ids = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for _, phones, _, _, _ in prepared:
                    self._assert_eligible(db, phones)
                for _, phones, delay_value, every, cooldown in prepared:
                    ids.append(self._insert_batch(db, serial, message, phones, delay_value, every, cooldown, now))
                db.execute("COMMIT")
                return ids
            except Exception:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _assert_eligible(db: sqlite3.Connection, phones: list[str]) -> None:
        for phone in phones:
            listed = db.execute("SELECT 1 FROM suppression_list WHERE phone=?", (phone,)).fetchone()
            row = db.execute("SELECT consented,suppressed FROM contacts WHERE phone=?", (phone,)).fetchone()
            if listed or not row or not row["consented"] or row["suppressed"]:
                raise ValueError("Batch contains a non-consented, unknown, or suppressed recipient.")

    @staticmethod
    def _insert_batch(db: sqlite3.Connection, serial: str, message: str, phones: list[str],
                      delay: float, cooldown_every: int, cooldown_seconds: float, now: str) -> int:
        cursor = db.execute("""INSERT INTO batches(created_at,device_serial,message,state,total,delay_seconds,cooldown_every,cooldown_seconds)
            VALUES (?,?,?,'pending',?,?,?,?)""",
            (now, serial, message, len(phones), delay, cooldown_every, cooldown_seconds))
        batch_id = int(cursor.lastrowid)
        db.execute("INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail) VALUES (?,?,NULL,'pending','Batch created')",
                   (batch_id, now))
        for phone in phones:
            previous = db.execute("""SELECT COALESCE(MAX(attempt),0) FROM message_history
                WHERE phone=? AND message=?""", (phone, message)).fetchone()[0]
            db.execute("""INSERT INTO message_history(batch_id,phone,message,status,created_at,updated_at,device_serial,attempt)
                VALUES (?,?,?,'pending',?,?,?,?)""",
                (batch_id, phone, message, now, now, serial, int(previous) + 1))
        return batch_id

    def _prepare_batch(self, serial: str, message: str, phones: list[str], delay: float,
                       cooldown_every: int, cooldown_seconds: float):
        validate_message(message)
        phones = [self._normalized_phone(phone) for phone in phones]
        if not phones:
            raise ValueError("A batch must contain at least one eligible recipient.")
        if len(set(phones)) != len(phones):
            raise ValueError("A batch cannot contain duplicate recipient numbers.")
        if not serial or not serial.strip():
            raise ValueError("An explicit ADB device serial is required.")
        if not math.isfinite(float(delay)) or not 0 <= float(delay) <= 3600:
            raise ValueError("Delay must be finite and between 0 and 3600 seconds.")
        if isinstance(cooldown_every, bool) or not isinstance(cooldown_every, int) or not 0 <= cooldown_every <= 1000:
            raise ValueError("Cooldown interval must be an integer between 0 and 1000.")
        if not math.isfinite(float(cooldown_seconds)) or not 0 <= float(cooldown_seconds) <= 86400:
            raise ValueError("Cooldown must be finite and between 0 and 86400 seconds.")
        return message, phones, float(delay), cooldown_every, float(cooldown_seconds)

    def batch_items(self, batch_id: int) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM message_history WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()]

    def failed_retry_data(self, batch_id: int) -> dict:
        """Return only failed recipients; sent and pending entries are never retried."""
        with self.connection() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Batch not found.")
            phones = [row["phone"] for row in db.execute(
                "SELECT phone FROM message_history WHERE batch_id=? AND status='failed' ORDER BY id",
                (batch_id,)).fetchall()]
            return {"batch": dict(batch), "phones": phones}

    def pending_recovery_data(self, batch_id: int) -> dict:
        """Describe pending items for an explicit, user-initiated safe resume."""
        with self.connection() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Batch not found.")
            phones = [row["phone"] for row in db.execute(
                "SELECT phone FROM message_history WHERE batch_id=? AND status='pending' ORDER BY id",
                (batch_id,)).fetchall()]
            return {"batch": dict(batch), "phones": phones}

    def set_batch_state(self, batch_id: int, state: str, *, finished: bool = False, note: str | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
                if row is None:
                    raise ValueError("Batch not found.")
                now = utc_now()
                db.execute("UPDATE batches SET state=?, finished_at=?, note=COALESCE(?,note) WHERE id=?",
                           (state, now if finished else None, note, batch_id))
                if row["state"] != state or note:
                    db.execute("INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail) VALUES (?,?,?,?,?)",
                               (batch_id, now, row["state"], state, note))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def mark_item(self, item_id: int, status: str, *, error: str | None = None,
                  verification: str | None = None) -> None:
        with self.connection() as db:
            db.execute("""UPDATE message_history SET status=?,updated_at=?,error_detail=?,verification=?
                WHERE id=? AND status='pending'""",
                (status, utc_now(), error[:500] if error else None, verification, item_id))

    def history(self, limit: int = 500) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT h.id,h.batch_id,h.phone,h.message,h.status,h.created_at,h.updated_at,
                       h.device_serial,h.error_detail,h.verification,b.state AS batch_state
                FROM message_history h JOIN batches b ON b.id=h.batch_id
                ORDER BY h.id DESC LIMIT ?""", (limit,)).fetchall()]

    def recover_interrupted(self) -> int:
        """Mark runs interrupted by process exit; pending messages remain pending."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute("SELECT id,state FROM batches WHERE state IN ('running','paused','stopping')").fetchall()
                now = utc_now()
                for row in rows:
                    db.execute("""UPDATE batches SET state='failed',finished_at=?,note='Application exited before batch completion'
                        WHERE id=?""", (now, row["id"]))
                    db.execute("""INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail)
                        VALUES (?,?,?,'failed','Application exited before batch completion')""",
                        (row["id"], now, row["state"]))
                db.execute("COMMIT")
                return len(rows)
            except Exception:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _normalized_phone(phone: str) -> str:
        try:
            normalized = normalize_phone(phone)
        except ValidationError:
            raise
        if normalized != phone:
            raise ValueError("Phone number must be normalized before persistence.")
        return normalized

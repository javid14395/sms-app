"""SQLite persistence with isolated connections, versioned schema migrations,
safe SQLite backups, campaigns, templates, settings, and auditable batch history.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .validation import ValidationError, normalize_phone, validate_message

SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_local_time(utc_iso: str | None) -> str:
    """Convert stored UTC ISO-8601 string to local display timestamp."""
    if not utc_iso:
        return ""
    try:
        dt = datetime.fromisoformat(utc_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(utc_iso)



def _repair_orphaned_wal_header(db_path: Path) -> None:
    """If a SQLite file was copied without its -wal/-shm sidecars, reset bytes 18-19 to 1 (legacy journal) so SQLite can open it cleanly."""
    try:
        if db_path.exists() and db_path.stat().st_size >= 100:
            wal_path = db_path.with_name(db_path.name + "-wal")
            shm_path = db_path.with_name(db_path.name + "-shm")
            if not wal_path.exists() and not shm_path.exists():
                with open(db_path, "r+b") as fh:
                    header = bytearray(fh.read(24))
                    if header[:16] == b"SQLite format 3\x00" and (header[18] == 2 or header[19] == 2):
                        header[18] = 1
                        header[19] = 1
                        fh.seek(0)
                        fh.write(header)
    except OSError:
        pass


class HistoryStore:
    def __init__(self, path: str | Path = "sms_history_pro.db", *, initialize: bool = True):
        self.path = Path(path)
        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._all_connections: set[sqlite3.Connection] = set()
        self.last_backup_path: Path | None = None
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        with self._connections_lock:
            self._all_connections.add(connection)
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
                with self._connections_lock:
                    self._all_connections.discard(connection)
                connection.close()
                self._local.connection = None

    def close_all(self) -> None:
        """Close any remaining open SQLite connections cleanly on shutdown."""
        with self._connections_lock:
            conns = list(self._all_connections)
            self._all_connections.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.connection = None

    def backup_database(self, destination: str | Path | None = None) -> Path:
        """Create a consistent online backup using SQLite's native backup API."""
        if not self.path.exists():
            raise FileNotFoundError(f"Database file does not exist yet: {self.path}")
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_dir = self.path.parent / "db_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            destination = backup_dir / f"{self.path.stem}-backup-{stamp}-{uuid.uuid4().hex[:6]}.db"
        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as src_db:
            dst_db = sqlite3.connect(dest_path)
            try:
                src_db.backup(dst_db)
            finally:
                dst_db.close()
        self.last_backup_path = dest_path
        return dest_path

    def restore_database(self, backup_source: str | Path) -> Path:
        """Validate a backup SQLite database and restore it safely over the active database."""
        src_path = Path(backup_source)
        if not src_path.exists():
            raise FileNotFoundError(f"Backup file not found: {src_path}")
        check_conn = sqlite3.connect(src_path)
        try:
            integrity = check_conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("Backup file failed SQLite integrity_check.")
            meta = check_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if meta is None:
                raise ValueError("Selected file is not a valid SMS Desk database (missing schema_meta).")
        finally:
            check_conn.close()

        safety_copy = None
        if self.path.exists():
            safety_copy = self.backup_database()
        self.close_all()
        try:
            src_conn = sqlite3.connect(src_path)
            dst_conn = sqlite3.connect(self.path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            self.initialize()
        except Exception as exc:
            if safety_copy and safety_copy.exists():
                shutil.copy2(safety_copy, self.path)
            raise RuntimeError(f"Restore failed; original database preserved: {exc}") from exc
        return self.path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _repair_orphaned_wal_header(self.path)
        # Check if an existing database needs a pre-migration backup
        if self.path.exists() and self.path.stat().st_size > 0:
            try:
                probe = sqlite3.connect(self.path, timeout=5)
                try:
                    row = probe.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
                    existing_ver = row[0] if row else None
                except sqlite3.Error:
                    existing_ver = None
                finally:
                    probe.close()
                if existing_ver is not None and existing_ver < SCHEMA_VERSION:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup_dir = self.path.parent / "db_backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    pre_mig = backup_dir / f"{self.path.stem}-pre-v{existing_ver}-to-v{SCHEMA_VERSION}-{stamp}.db"
                    src = sqlite3.connect(self.path)
                    dst = sqlite3.connect(pre_mig)
                    try:
                        src.backup(dst)
                        self.last_backup_path = pre_mig
                    finally:
                        dst.close()
                        src.close()
            except Exception:
                pass

        with self.connection() as db:
            try:
                try:
                    db.execute("PRAGMA journal_mode = DELETE")
                except sqlite3.OperationalError:
                    pass
                db.executescript(
                    """ 
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
                        suppression_note TEXT,
                        name TEXT DEFAULT '',
                        variables_json TEXT DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS suppression_list (
                        phone TEXT PRIMARY KEY,
                        suppressed_at TEXT NOT NULL,
                        note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS campaigns (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        template_id INTEGER,
                        template_text TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'draft',
                        batch_size INTEGER NOT NULL DEFAULT 100 CHECK (batch_size >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS templates (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
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
                        note TEXT,
                        campaign_id INTEGER REFERENCES campaigns(id),
                        jitter_seconds REAL NOT NULL DEFAULT 0 CHECK (jitter_seconds >= 0),
                        claimed_by TEXT
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
                        job_uuid TEXT,
                        delivery_status TEXT,
                        error_category TEXT,
                        staged_at TEXT,
                        UNIQUE(batch_id, phone)
                    );
                    CREATE TABLE IF NOT EXISTS attempt_logs (
                        id INTEGER PRIMARY KEY,
                        history_id INTEGER NOT NULL REFERENCES message_history(id),
                        batch_id INTEGER NOT NULL REFERENCES batches(id),
                        attempt_number INTEGER NOT NULL,
                        device_serial TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        outcome TEXT NOT NULL,
                        verification TEXT,
                        error_category TEXT,
                        diagnostic TEXT
                    );
                    CREATE TABLE IF NOT EXISTS device_registry (
                        serial TEXT PRIMARY KEY,
                        alias TEXT DEFAULT '',
                        brand TEXT DEFAULT '',
                        model TEXT DEFAULT '',
                        last_seen TEXT,
                        last_checked TEXT
                    );
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS audit_trail (
                        id INTEGER PRIMARY KEY,
                        occurred_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        detail TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_history_batch_status ON message_history(batch_id, status);
                    CREATE INDEX IF NOT EXISTS idx_history_created ON message_history(created_at);
                    CREATE INDEX IF NOT EXISTS idx_contacts_suppressed ON contacts(suppressed);
                    CREATE INDEX IF NOT EXISTS idx_suppression_date ON suppression_list(suppressed_at);
                    CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id);
                    CREATE INDEX IF NOT EXISTS idx_attempt_logs_history ON attempt_logs(history_id);
                    CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_trail(occurred_at);
                    """
                )
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
                if current is None:
                    db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
                elif current["version"] > SCHEMA_VERSION:
                    raise RuntimeError("Database schema is newer than this application.")
                else:
                    ver = int(current["version"])
                    if ver == 1:
                        db.execute(
                            """CREATE TABLE IF NOT EXISTS batch_events (
                            id INTEGER PRIMARY KEY,
                            batch_id INTEGER NOT NULL REFERENCES batches(id),
                            occurred_at TEXT NOT NULL,
                            from_state TEXT,
                            to_state TEXT NOT NULL,
                            detail TEXT
                        )"""
                        )
                        db.execute("CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id)")
                        db.execute(
                            """CREATE TABLE IF NOT EXISTS suppression_list (
                            phone TEXT PRIMARY KEY,
                            suppressed_at TEXT NOT NULL,
                            note TEXT
                        )"""
                        )
                        db.execute("CREATE INDEX IF NOT EXISTS idx_suppression_date ON suppression_list(suppressed_at)")
                        ver = 2
                        db.execute("UPDATE schema_meta SET version=2")
                    if ver == 2:
                        self._migrate_v2_to_v3(db)
                        ver = 3
                        db.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
                    if ver != SCHEMA_VERSION:
                        raise RuntimeError(f"No migration path from schema {current['version']}.")
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _migrate_v2_to_v3(db: sqlite3.Connection) -> None:
        """Add v3 columns and tables non-destructively while keeping v2 CHECK constraints intact."""
        contact_cols = {r["name"] for r in db.execute("PRAGMA table_info(contacts)").fetchall()}
        if "name" not in contact_cols:
            db.execute("ALTER TABLE contacts ADD COLUMN name TEXT DEFAULT ''")
        if "variables_json" not in contact_cols:
            db.execute("ALTER TABLE contacts ADD COLUMN variables_json TEXT DEFAULT '{}'")

        batch_cols = {r["name"] for r in db.execute("PRAGMA table_info(batches)").fetchall()}
        if "campaign_id" not in batch_cols:
            db.execute("ALTER TABLE batches ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id)")
        if "jitter_seconds" not in batch_cols:
            db.execute("ALTER TABLE batches ADD COLUMN jitter_seconds REAL NOT NULL DEFAULT 0")
        if "claimed_by" not in batch_cols:
            db.execute("ALTER TABLE batches ADD COLUMN claimed_by TEXT")

        hist_cols = {r["name"] for r in db.execute("PRAGMA table_info(message_history)").fetchall()}
        if "job_uuid" not in hist_cols:
            db.execute("ALTER TABLE message_history ADD COLUMN job_uuid TEXT")
        if "delivery_status" not in hist_cols:
            db.execute("ALTER TABLE message_history ADD COLUMN delivery_status TEXT")
        if "error_category" not in hist_cols:
            db.execute("ALTER TABLE message_history ADD COLUMN error_category TEXT")
        if "staged_at" not in hist_cols:
            db.execute("ALTER TABLE message_history ADD COLUMN staged_at TEXT")

        db.execute("CREATE INDEX IF NOT EXISTS idx_history_verification ON message_history(verification)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_batches_campaign ON batches(campaign_id)")

    def record_audit(self, action: str, detail: str) -> None:
        """Write a sanitized operator action entry to the durable local audit trail."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO audit_trail(occurred_at, action, detail) VALUES (?, ?, ?)",
                (utc_now(), action[:120], detail[:500]),
            )

    def audit_logs(self, limit: int = 200) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connection() as db:
            row = db.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value_json"])
            except json.JSONDecodeError:
                return default

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        with self.connection() as db:
            db.execute(
                """INSERT INTO app_settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, payload, utc_now()),
            )

    def save_device_metadata(
        self,
        serial: str,
        *,
        alias: str | None = None,
        brand: str | None = None,
        model: str | None = None,
        seen: bool = False,
        checked: bool = False,
    ) -> None:
        now = utc_now()
        with self.connection() as db:
            existing = db.execute("SELECT * FROM device_registry WHERE serial=?", (serial,)).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO device_registry(serial, alias, brand, model, last_seen, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        serial,
                        alias if alias is not None else "",
                        brand if brand is not None else "",
                        model if model is not None else "",
                        now if seen else None,
                        now if checked else None,
                    ),
                )
            else:
                db.execute(
                    """UPDATE device_registry SET
                        alias = COALESCE(?, alias),
                        brand = CASE WHEN ? IS NOT NULL AND ? != '' THEN ? ELSE brand END,
                        model = CASE WHEN ? IS NOT NULL AND ? != '' THEN ? ELSE model END,
                        last_seen = CASE WHEN ? THEN ? ELSE last_seen END,
                        last_checked = CASE WHEN ? THEN ? ELSE last_checked END
                    WHERE serial = ?""",
                    (
                        alias,
                        brand,
                        brand,
                        brand,
                        model,
                        model,
                        model,
                        1 if seen else 0,
                        now,
                        1 if checked else 0,
                        now,
                        serial,
                    ),
                )

    def device_registry_map(self) -> dict[str, dict]:
        with self.connection() as db:
            return {
                row["serial"]: dict(row)
                for row in db.execute("SELECT * FROM device_registry").fetchall()
            }

    def save_template(self, name: str, body: str) -> int:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValidationError("Template name cannot be empty.")
        if not body or not body.strip():
            raise ValidationError("Template body cannot be empty.")
        now = utc_now()
        with self.connection() as db:
            db.execute(
                """INSERT INTO templates(name, body, created_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at""",
                (clean_name[:120], body[:1000], now, now),
            )
            row = db.execute("SELECT id FROM templates WHERE name=?", (clean_name[:120],)).fetchone()
            return int(row["id"])

    def delete_template(self, template_id: int) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM templates WHERE id=?", (template_id,))

    def list_templates(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM templates ORDER BY name").fetchall()]

    def create_campaign(
        self,
        name: str,
        template_text: str,
        *,
        batch_size: int = 100,
        template_id: int | None = None,
        note: str = "",
    ) -> int:
        clean_name = (name or "").strip() or f"Campaign {utc_now()[:10]}"
        if not 1 <= int(batch_size) <= 1000:
            raise ValueError("Campaign batch size must be between 1 and 1000.")
        now = utc_now()
        with self.connection() as db:
            cur = db.execute(
                """INSERT INTO campaigns(name, template_id, template_text, status, batch_size, created_at, updated_at, note)
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
                (clean_name[:200], template_id, template_text[:1000], int(batch_size), now, now, note[:500]),
            )
            return int(cur.lastrowid)

    def duplicate_campaign(self, campaign_id: int, new_name: str | None = None) -> int:
        with self.connection() as db:
            row = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None:
                raise ValueError("Campaign not found.")
            dup_name = (new_name or f"{row['name']} (Copy)").strip()
            now = utc_now()
            cur = db.execute(
                """INSERT INTO campaigns(name, template_id, template_text, status, batch_size, created_at, updated_at, note)
                VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)""",
                (
                    dup_name[:200],
                    row["template_id"],
                    row["template_text"],
                    row["batch_size"],
                    now,
                    now,
                    f"Duplicated from campaign #{campaign_id}",
                ),
            )
            return int(cur.lastrowid)

    def list_campaigns(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT c.*,
                    (SELECT COUNT(*) FROM batches b WHERE b.campaign_id = c.id) AS batch_count,
                    (SELECT COALESCE(SUM(b.total), 0) FROM batches b WHERE b.campaign_id = c.id) AS recipient_count
                FROM campaigns c ORDER BY c.id DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def assign_batch_device(self, batch_id: int, new_serial: str) -> None:
        """Assign or reassign a pending/paused/failed batch to a specific device serial."""
        if not new_serial or not new_serial.strip():
            raise ValueError("An explicit ADB device serial is required.")
        serial = new_serial.strip()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                batch = db.execute("SELECT state, device_serial FROM batches WHERE id=?", (batch_id,)).fetchone()
                if batch is None:
                    raise ValueError("Batch not found.")
                if batch["state"] in ("running", "stopping"):
                    raise ValueError("Cannot reassign device while batch is actively running.")
                now = utc_now()
                db.execute("UPDATE batches SET device_serial=? WHERE id=?", (serial, batch_id))
                db.execute(
                    "UPDATE message_history SET device_serial=?, updated_at=? WHERE batch_id=? AND status='pending'",
                    (serial, now, batch_id),
                )
                db.execute(
                    """INSERT INTO batch_events(batch_id, occurred_at, from_state, to_state, detail)
                    VALUES (?, ?, ?, ?, ?)""",
                    (batch_id, now, batch["state"], batch["state"], f"Assigned to device {serial}"),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def list_batches(self, limit: int = 200) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT b.*, c.name AS campaign_name,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='pending' AND (h.verification IS NULL OR h.verification != 'composer_staged')) AS pending_count,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='pending' AND h.verification='composer_staged') AS staged_count,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='sent') AS sent_count,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='failed' AND COALESCE(h.error_category,'') != 'uncertain_interrupted' AND COALESCE(h.error_category,'') != 'skipped_ineligible') AS failed_count,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='failed' AND h.error_category='skipped_ineligible') AS skipped_count,
                    (SELECT COUNT(*) FROM message_history h WHERE h.batch_id=b.id AND h.status='failed' AND h.error_category='uncertain_interrupted') AS uncertain_count
                FROM batches b
                LEFT JOIN campaigns c ON c.id = b.campaign_id
                ORDER BY b.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def dashboard_counts(self) -> dict[str, int]:
        with self.connection() as db:
            campaigns = db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
            batches = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            contacts = db.execute("SELECT COUNT(*) FROM contacts WHERE consented=1 AND suppressed=0").fetchone()[0]
            suppressed = db.execute("SELECT COUNT(*) FROM suppression_list").fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='pending' AND (verification IS NULL OR verification NOT IN ('composer_staged', 'claimed'))"
            ).fetchone()[0]
            in_progress = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='pending' AND verification='claimed'"
            ).fetchone()[0]
            staged = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='pending' AND verification='composer_staged'"
            ).fetchone()[0]
            sent = db.execute("SELECT COUNT(*) FROM message_history WHERE status='sent'").fetchone()[0]
            skipped = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='failed' AND error_category='skipped_ineligible'"
            ).fetchone()[0]
            uncertain = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='failed' AND error_category='uncertain_interrupted'"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT COUNT(*) FROM message_history WHERE status='failed' AND COALESCE(error_category, '') NOT IN ('skipped_ineligible', 'uncertain_interrupted')"
            ).fetchone()[0]
            return {
                "campaigns": int(campaigns),
                "batches": int(batches),
                "eligible_contacts": int(contacts),
                "suppressed_contacts": int(suppressed),
                "pending": int(pending),
                "in_progress": int(in_progress),
                "staged": int(staged),
                "sent": int(sent),
                "failed": int(failed),
                "skipped": int(skipped),
                "uncertain": int(uncertain),
            }

    def import_consented(
        self,
        phones: list[str],
        source: str,
        *,
        metadata: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        phones = [self._normalized_phone(phone) for phone in phones]
        now = utc_now()
        inserted = suppressed = 0
        meta_map = metadata or {}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for phone in phones:
                    existing = db.execute(
                        "SELECT suppressed FROM contacts WHERE phone=?", (phone,)
                    ).fetchone()
                    listed = db.execute(
                        "SELECT 1 FROM suppression_list WHERE phone=?", (phone,)
                    ).fetchone()
                    info = meta_map.get(phone, {})
                    name_val = str(info.get("name", ""))[:200]
                    src_val = str(info.get("consent_source") or source)[:300]
                    ts_val = str(info.get("consented_at") or now)[:64]
                    vars_json = json.dumps(info.get("variables", {}))
                    if listed or (existing and existing["suppressed"]):
                        suppressed += 1
                    elif existing:
                        if name_val or info.get("variables"):
                            db.execute(
                                "UPDATE contacts SET name=COALESCE(NULLIF(?,''), name), variables_json=? WHERE phone=?",
                                (name_val, vars_json, phone),
                            )
                        continue
                    else:
                        db.execute(
                            """INSERT INTO contacts(phone,consented,consent_source,consented_at,name,variables_json)
                            VALUES (?,1,?,?,?,?)""",
                            (phone, src_val, ts_val, name_val, vars_json),
                        )
                        inserted += 1
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return inserted, suppressed

    def contact_variables(self, phones: list[str]) -> dict[str, dict[str, str]]:
        """Retrieve stored recipient personalization variables for a list of phones."""
        if not phones:
            return {}
        result: dict[str, dict[str, str]] = {}
        with self.connection() as db:
            for phone in phones:
                row = db.execute(
                    "SELECT name, consent_source, consented_at, variables_json FROM contacts WHERE phone=?",
                    (phone,),
                ).fetchone()
                if row:
                    try:
                        vars_dict = json.loads(row["variables_json"] or "{}")
                    except json.JSONDecodeError:
                        vars_dict = {}
                    if row["name"]:
                        vars_dict.setdefault("name", row["name"])
                    vars_dict["phone"] = phone
                    result[phone] = {str(k): str(v) for k, v in vars_dict.items()}
                else:
                    result[phone] = {"phone": phone}
        return result

    def suppress(self, phone: str, note: str = "User requested opt-out") -> None:
        phone = self._normalized_phone(phone)
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT INTO suppression_list(phone,suppressed_at,note) VALUES (?,?,?)
                    ON CONFLICT(phone) DO UPDATE SET suppressed_at=excluded.suppressed_at,note=excluded.note""",
                    (phone, now, note[:300]),
                )
                db.execute(
                    """UPDATE contacts SET suppressed=1,suppressed_at=?,suppression_note=?
                    WHERE phone=?""",
                    (now, note[:300], phone),
                )
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
                row = db.execute(
                    "SELECT consented,suppressed FROM contacts WHERE phone=?", (phone,)
                ).fetchone()
                if not listed and row and row["consented"] and not row["suppressed"]:
                    result.append(phone)
            return result

    def create_batch(
        self,
        serial: str,
        message: str,
        phones: list[str],
        delay: float,
        cooldown_every: int,
        cooldown_seconds: float,
        *,
        campaign_id: int | None = None,
        per_recipient_messages: dict[str, str] | None = None,
        jitter_seconds: float = 0.0,
    ) -> int:
        message, phones, delay, cooldown_every, cooldown_seconds = self._prepare_batch(
            serial, message, phones, delay, cooldown_every, cooldown_seconds
        )
        if not math.isfinite(float(jitter_seconds)) or not 0 <= float(jitter_seconds) <= 600:
            raise ValueError("Timing variation (jitter) must be between 0 and 600 seconds.")
        if per_recipient_messages:
            for p in phones:
                if p in per_recipient_messages:
                    validate_message(per_recipient_messages[p])
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_eligible(db, phones)
                batch_id = self._insert_batch(
                    db,
                    serial,
                    message,
                    phones,
                    delay,
                    cooldown_every,
                    cooldown_seconds,
                    now,
                    campaign_id=campaign_id,
                    per_recipient_messages=per_recipient_messages,
                    jitter_seconds=float(jitter_seconds),
                )
                db.execute("COMMIT")
                return batch_id
            except Exception:
                db.execute("ROLLBACK")
                raise

    def create_batches(
        self,
        serial: str | list[str],
        message: str,
        recipient_batches: list[list[str]],
        delay: float,
        cooldown_every: int,
        cooldown_seconds: float,
        *,
        campaign_id: int | None = None,
        per_recipient_messages: dict[str, str] | None = None,
        jitter_seconds: float = 0.0,
    ) -> list[int]:
        """Atomically create all size-limited batches for one user-confirmed run.

        Supports either a single device serial string or a per-batch list of device serials.
        """
        if not recipient_batches:
            raise ValueError("No batches were supplied.")
        if isinstance(serial, list):
            if len(serial) != len(recipient_batches):
                raise ValueError("Device serial list length must match recipient_batches count.")
            serials = serial
        else:
            serials = [serial] * len(recipient_batches)

        prepared = [
            self._prepare_batch(ser, message, phones, delay, cooldown_every, cooldown_seconds)
            for ser, phones in zip(serials, recipient_batches)
        ]
        if any(item[0] != prepared[0][0] or item[2:] != prepared[0][2:] for item in prepared):
            raise ValueError("Batch settings must be consistent within one run.")
        all_phones = [phone for item in prepared for phone in item[1]]
        if len(all_phones) != len(set(all_phones)):
            raise ValueError("A run cannot contain duplicate recipient numbers.")
        if not math.isfinite(float(jitter_seconds)) or not 0 <= float(jitter_seconds) <= 600:
            raise ValueError("Timing variation (jitter) must be between 0 and 600 seconds.")
        if per_recipient_messages:
            for p in all_phones:
                if p in per_recipient_messages:
                    validate_message(per_recipient_messages[p])

        now = utc_now()
        ids = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for _, phones, _, _, _ in prepared:
                    self._assert_eligible(db, phones)
                for ser, (_, phones, delay_value, every, cooldown) in zip(serials, prepared):
                    ids.append(
                        self._insert_batch(
                            db,
                            ser,
                            message,
                            phones,
                            delay_value,
                            every,
                            cooldown,
                            now,
                            campaign_id=campaign_id,
                            per_recipient_messages=per_recipient_messages,
                            jitter_seconds=float(jitter_seconds),
                        )
                    )
                db.execute("COMMIT")
                return ids
            except Exception:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _assert_eligible(db: sqlite3.Connection, phones: list[str]) -> None:
        for phone in phones:
            listed = db.execute("SELECT 1 FROM suppression_list WHERE phone=?", (phone,)).fetchone()
            row = db.execute(
                "SELECT consented,suppressed FROM contacts WHERE phone=?", (phone,)
            ).fetchone()
            if listed or not row or not row["consented"] or row["suppressed"]:
                raise ValueError("Batch contains a non-consented, unknown, or suppressed recipient.")

    @staticmethod
    def _insert_batch(
        db: sqlite3.Connection,
        serial: str,
        message: str,
        phones: list[str],
        delay: float,
        cooldown_every: int,
        cooldown_seconds: float,
        now: str,
        *,
        campaign_id: int | None = None,
        per_recipient_messages: dict[str, str] | None = None,
        jitter_seconds: float = 0.0,
    ) -> int:
        cursor = db.execute(
            """INSERT INTO batches(created_at,device_serial,message,state,total,delay_seconds,cooldown_every,cooldown_seconds,campaign_id,jitter_seconds)
            VALUES (?,?,?,'pending',?,?,?,?,?,?)""",
            (now, serial, message, len(phones), delay, cooldown_every, cooldown_seconds, campaign_id, jitter_seconds),
        )
        batch_id = int(cursor.lastrowid)
        db.execute(
            "INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail) VALUES (?,?,NULL,'pending','Batch created')",
            (batch_id, now),
        )
        for phone in phones:
            rendered = (per_recipient_messages or {}).get(phone, message)
            previous = db.execute(
                """SELECT COALESCE(MAX(attempt),0) FROM message_history
                WHERE phone=? AND message=?""",
                (phone, rendered),
            ).fetchone()[0]
            db.execute(
                """INSERT INTO message_history(batch_id,phone,message,status,created_at,updated_at,device_serial,attempt,job_uuid,delivery_status)
                VALUES (?,?,?,'pending',?,?,?,?,?,'unverified')""",
                (batch_id, phone, rendered, now, now, serial, int(previous) + 1, str(uuid.uuid4())),
            )
        return batch_id

    def _prepare_batch(
        self,
        serial: str,
        message: str,
        phones: list[str],
        delay: float,
        cooldown_every: int,
        cooldown_seconds: float,
    ):
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
        if (
            isinstance(cooldown_every, bool)
            or not isinstance(cooldown_every, int)
            or not 0 <= cooldown_every <= 1000
        ):
            raise ValueError("Cooldown interval must be an integer between 0 and 1000.")
        if not math.isfinite(float(cooldown_seconds)) or not 0 <= float(cooldown_seconds) <= 86400:
            raise ValueError("Cooldown must be finite and between 0 and 86400 seconds.")
        return message, phones, float(delay), cooldown_every, float(cooldown_seconds)

    def batch_items(self, batch_id: int) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM message_history WHERE batch_id=? ORDER BY id", (batch_id,)
                ).fetchall()
            ]

    def claim_item(self, item_id: int, worker_id: str) -> bool:
        """Transactionally claim a pending message item so competing workers cannot claim the same job."""
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    """UPDATE message_history
                    SET verification='claimed', updated_at=?
                    WHERE id=? AND status='pending' AND (verification IS NULL OR verification='')""",
                    (now, item_id),
                )
                if cur.rowcount == 1:
                    row = db.execute(
                        "SELECT batch_id, attempt, device_serial FROM message_history WHERE id=?",
                        (item_id,),
                    ).fetchone()
                    if row:
                        db.execute(
                            """INSERT INTO attempt_logs(history_id, batch_id, attempt_number, device_serial, started_at, outcome)
                            VALUES (?, ?, ?, ?, ?, 'claimed')""",
                            (item_id, row["batch_id"], row["attempt"], row["device_serial"], now),
                        )
                db.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise

    def mark_staged(self, item_id: int) -> None:
        """Record that Android's SMS composer was opened for this item (`composer_staged`).

        A staged composer is NOT marked 'sent'; status remains 'pending' with
        verification='composer_staged' until the operator explicitly confirms or declines.
        """
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """UPDATE message_history
                    SET verification='composer_staged', staged_at=?, updated_at=?
                    WHERE id=? AND status='pending'""",
                    (now, now, item_id),
                )
                db.execute(
                    """UPDATE attempt_logs
                    SET outcome='composer_staged', verification='composer_staged'
                    WHERE history_id=? AND ended_at IS NULL""",
                    (item_id,),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def release_unconfirmed_claim(self, item_id: int) -> None:
        """Return a claimed or staged item to clean pending state upon immediate operator Stop."""
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """UPDATE message_history
                    SET verification=NULL, updated_at=?
                    WHERE id=? AND status='pending'""",
                    (now, item_id),
                )
                db.execute(
                    """UPDATE attempt_logs
                    SET ended_at=?, outcome='stopped_pending'
                    WHERE history_id=? AND ended_at IS NULL""",
                    (now, item_id),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def failed_retry_data(self, batch_id: int) -> dict:
        """Return retryable failed recipients; sent, pending, and uncertain_interrupted entries are excluded."""
        with self.connection() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Batch not found.")
            rows = db.execute(
                """SELECT phone, error_category, error_detail FROM message_history
                WHERE batch_id=? AND status='failed' ORDER BY id""",
                (batch_id,),
            ).fetchall()
            phones = []
            uncertain_phones = []
            for r in rows:
                if r["error_category"] == "uncertain_interrupted":
                    uncertain_phones.append(r["phone"])
                else:
                    phones.append(r["phone"])
            eligible = self.eligible(phones)
            ineligible = [p for p in phones if p not in eligible]
            return {
                "batch": dict(batch),
                "phones": phones,
                "eligible_phones": eligible,
                "ineligible_phones": ineligible,
                "uncertain_phones": uncertain_phones,
            }

    def pending_recovery_data(self, batch_id: int) -> dict:
        """Describe pending items for an explicit, user-initiated safe resume."""
        with self.connection() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Batch not found.")
            phones = [
                row["phone"]
                for row in db.execute(
                    "SELECT phone FROM message_history WHERE batch_id=? AND status='pending' ORDER BY id",
                    (batch_id,),
                ).fetchall()
            ]
            return {"batch": dict(batch), "phones": phones}

    def set_batch_state(
        self, batch_id: int, state: str, *, finished: bool = False, note: str | None = None
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
                if row is None:
                    raise ValueError("Batch not found.")
                now = utc_now()
                db.execute(
                    "UPDATE batches SET state=?, finished_at=?, note=COALESCE(?,note) WHERE id=?",
                    (state, now if finished else None, note, batch_id),
                )
                if row["state"] != state or note:
                    db.execute(
                        "INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail) VALUES (?,?,?,?,?)",
                        (batch_id, now, row["state"], state, note),
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def mark_item(
        self,
        item_id: int,
        status: str,
        *,
        error: str | None = None,
        verification: str | None = None,
        error_category: str | None = None,
        delivery_status: str | None = None,
    ) -> None:
        now = utc_now()
        err_trimmed = error[:500] if error else None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    """UPDATE message_history
                    SET status=?, updated_at=?, error_detail=?, verification=?,
                        error_category=COALESCE(?, error_category),
                        delivery_status=COALESCE(?, delivery_status)
                    WHERE id=? AND status='pending'""",
                    (status, now, err_trimmed, verification, error_category, delivery_status, item_id),
                )
                if cur.rowcount == 1:
                    db.execute(
                        """UPDATE attempt_logs
                        SET ended_at=?, outcome=?, verification=?, error_category=?, diagnostic=?
                        WHERE history_id=? AND ended_at IS NULL""",
                        (now, status, verification, error_category, err_trimmed, item_id),
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def history(
        self,
        limit: int = 500,
        *,
        status_filter: str = "",
        device_filter: str = "",
        search_query: str = "",
        sort_desc: bool = True,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if status_filter and status_filter != "all":
            if status_filter == "staged":
                clauses.append("h.status='pending' AND h.verification='composer_staged'")
            elif status_filter == "uncertain":
                clauses.append("h.status='failed' AND h.error_category='uncertain_interrupted'")
            elif status_filter == "skipped":
                clauses.append("h.status='failed' AND h.error_category='skipped_ineligible'")
            else:
                clauses.append("h.status=?")
                params.append(status_filter)
        if device_filter and device_filter != "all":
            clauses.append("h.device_serial=?")
            params.append(device_filter)
        if search_query and search_query.strip():
            q = f"%{search_query.strip()}%"
            clauses.append("(h.phone LIKE ? OR h.message LIKE ? OR COALESCE(h.error_detail,'') LIKE ?)")
            params.extend([q, q, q])

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_sql = "DESC" if sort_desc else "ASC"
        params.append(int(limit))
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"""
                    SELECT h.id, h.batch_id, h.phone, h.message, h.status, h.created_at, h.updated_at,
                           h.device_serial, h.attempt, h.error_detail, h.verification,
                           h.job_uuid, h.delivery_status, h.error_category, h.staged_at,
                           b.state AS batch_state, b.campaign_id
                    FROM message_history h JOIN batches b ON b.id=h.batch_id
                    {where_sql}
                    ORDER BY h.id {order_sql} LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def export_history_rows(
        self,
        rows: list[dict],
        *,
        redact_phones: bool = False,
        redact_messages: bool = False,
    ) -> list[dict]:
        """Prepare selected history rows for export with optional privacy masking."""
        exported = []
        for r in rows:
            phone = str(r.get("phone", ""))
            if redact_phones and len(phone) > 4:
                phone = "***" + phone[-4:]
            msg = str(r.get("message", ""))
            if redact_messages:
                msg = f"[redacted ({len(msg)} chars)]"
            exported.append(
                {
                    "id": r.get("id"),
                    "batch_id": r.get("batch_id"),
                    "status": r.get("status"),
                    "verification": r.get("verification") or "",
                    "delivery_status": r.get("delivery_status") or "unverified",
                    "error_category": r.get("error_category") or "",
                    "phone": phone,
                    "device_serial": r.get("device_serial"),
                    "attempt": r.get("attempt", 1),
                    "created_at_utc": r.get("created_at"),
                    "updated_at_utc": r.get("updated_at"),
                    "message": msg,
                    "error_detail": r.get("error_detail") or "",
                }
            )
        return exported

    def recover_interrupted(self) -> int:
        """Mark runs interrupted by process exit; un-staged pending items remain pending.

        Items that were left in `verification='claimed'` (before composer opened) are
        reset to clean pending. Items left in `verification='composer_staged'` keep
        their `composer_staged` verification marker so the operator can see the composer
        was opened before interruption and is never auto-retried silently.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT id,state FROM batches WHERE state IN ('running','paused','stopping')"
                ).fetchall()
                now = utc_now()
                for row in rows:
                    db.execute(
                        """UPDATE batches SET state='failed',finished_at=?,note='Application exited before batch completion'
                        WHERE id=?""",
                        (now, row["id"]),
                    )
                    db.execute(
                        """INSERT INTO batch_events(batch_id,occurred_at,from_state,to_state,detail)
                        VALUES (?,?,?,'failed','Application exited before batch completion')""",
                        (row["id"], now, row["state"]),
                    )
                    db.execute(
                        """UPDATE message_history SET verification=NULL, updated_at=?
                        WHERE batch_id=? AND status='pending' AND verification='claimed'""",
                        (now, row["id"]),
                    )
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

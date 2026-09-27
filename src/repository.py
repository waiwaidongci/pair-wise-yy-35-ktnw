from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import DOSE_STATUSES, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dosimeter_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    dosimeter_no TEXT NOT NULL,
                    wear_period TEXT NOT NULL,
                    anomaly_type TEXT NOT NULL,
                    original_dose REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','resolved')),
                    version INTEGER NOT NULL DEFAULT 1,
                    replacement_dose REAL,
                    spare_dosimeter_no TEXT,
                    verified_by TEXT,
                    verified_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_incident_open_period
                    ON dosimeter_incidents(dosimeter_no, wear_period)
                    WHERE status='open';
                CREATE INDEX IF NOT EXISTS ix_incident_item
                    ON dosimeter_incidents(item_id);
            """)
            self._migrate_columns()

    _ITEM_ADDED_COLUMNS = (
        ("dosimeter_no", "TEXT"),
        ("wear_period", "TEXT"),
        ("dose_status", f"TEXT NOT NULL DEFAULT '{DOSE_STATUSES[0]}'"),
        ("replacement_dose", "REAL"),
        ("effective_dose", "REAL NOT NULL DEFAULT 0"),
    )

    def _migrate_columns(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        for name, decl in self._ITEM_ADDED_COLUMNS:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
        self.conn.execute(
            "UPDATE items SET effective_dose=quantity WHERE effective_dose=0 AND dose_status=?"
            , (DOSE_STATUSES[0],))

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, dosimeter_no: Optional[str] = None,
                    wear_period: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, dosimeter_no, wear_period,
                       dose_status, replacement_dose, effective_dose,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, dosimeter_no, wear_period, DOSE_STATUSES[0], None,
                     quantity, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _incident(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_incident(self, item_id: int, dosimeter_no: str, wear_period: str,
                        anomaly_type: str, original_dose: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dosimeter_incidents(item_id, dosimeter_no, wear_period,
                       anomaly_type, original_dose, status, version, created_by,
                       created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, dosimeter_no, wear_period, anomaly_type, original_dose,
                     "open", 1, actor, now, now),
                )
                incident_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同周期同编号已有未结案的有效补测事件") from exc
        return self.get_incident(incident_id)

    def get_incident(self, incident_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dosimeter_incidents WHERE id=?", (incident_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("坏损剂量计事件不存在")
        return self._incident(row)

    def list_incidents(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dosimeter_incidents"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._incident(row) for row in rows]

    def list_open_incidents(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dosimeter_incidents WHERE item_id=? AND status='open' ORDER BY id",
                (item_id,),
            ).fetchall()
        return [self._incident(row) for row in rows]

    def list_incidents_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dosimeter_incidents WHERE item_id=? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [self._incident(row) for row in rows]

    def open_incident_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM dosimeter_incidents WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def apply_excluded_dose(self, item_id: int, dosimeter_no: Optional[str],
                            wear_period: Optional[str]) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE items SET dose_status=?, replacement_dose=NULL,
                   effective_dose=0, version=version+1, updated_at=?,
                   dosimeter_no=COALESCE(dosimeter_no, ?),
                   wear_period=COALESCE(wear_period, ?)
                   WHERE id=?""",
                (DOSE_STATUSES[1], now, dosimeter_no, wear_period, item_id),
            )

    def apply_replacement_dose(self, item_id: int, replacement_dose: float) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE items SET dose_status=?, replacement_dose=?,
                   effective_dose=?, version=version+1, updated_at=? WHERE id=?""",
                (DOSE_STATUSES[2], replacement_dose, replacement_dose, now, item_id),
            )

    def resolve_incident(self, incident_id: int, replacement_dose: float,
                         spare_dosimeter_no: str, verified_by: str,
                         expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dosimeter_incidents WHERE id=?", (incident_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("坏损剂量计事件不存在")
            if row["status"] != "open":
                raise ConflictError("该补测事件已结案")
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            self.conn.execute(
                """UPDATE dosimeter_incidents SET status='resolved',
                   replacement_dose=?, spare_dosimeter_no=?, verified_by=?,
                   verified_at=?, version=version+1, updated_at=? WHERE id=?""",
                (replacement_dose, spare_dosimeter_no, verified_by, now, now,
                 incident_id),
            )
        return self.get_incident(incident_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()

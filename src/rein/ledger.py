"""SQLite change ledger. One row in `changes` = one field-level change."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .attribution import Attribution
from .diff import FieldChange
from .snapshot import Snapshot
from .timeutil import iso, now_utc, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    api_version TEXT NOT NULL,
    source TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_entities (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS activities (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, seq)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    detected_at TEXT NOT NULL,
    account_id TEXT NOT NULL,
    from_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    to_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_name TEXT,
    field TEXT NOT NULL,
    change_type TEXT NOT NULL,
    before TEXT,
    after TEXT,
    entity_updated_time TEXT,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('human', 'external_agent', 'meta_ai', 'unknown')),
    attribution TEXT NOT NULL,
    evaluated_at TEXT,
    violation INTEGER NOT NULL DEFAULT 0,
    severity TEXT CHECK (severity IN ('violation', 'warning') OR severity IS NULL),
    findings TEXT NOT NULL DEFAULT '[]',
    UNIQUE (from_snapshot_id, to_snapshot_id, entity_type, entity_id, field)
);
CREATE INDEX IF NOT EXISTS changes_by_time ON changes (detected_at, id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    change_id INTEGER NOT NULL REFERENCES changes(id),
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    deliveries TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revert_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    change_ids TEXT NOT NULL,
    plan TEXT NOT NULL,
    results TEXT NOT NULL
);
-- Phase 2: MCP proxy. Kept apart from `changes` (Watch). Times use the same ISO 8601 UTC form and
-- entity ids the same string form, so Phase 3 can join audit records with detected changes.
CREATE TABLE IF NOT EXISTS proxy_audit (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event TEXT NOT NULL,
    declared_client TEXT,
    operator TEXT,
    tool TEXT NOT NULL,
    arguments_summary TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    entity_type TEXT,
    entity_id TEXT,
    approval_id TEXT,
    forwarded INTEGER NOT NULL DEFAULT 0,
    downstream_ok INTEGER,
    result_summary TEXT,
    result_kind TEXT
);
CREATE INDEX IF NOT EXISTS proxy_audit_by_time ON proxy_audit (occurred_at, id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    declared_client TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    findings TEXT NOT NULL,
    precheck TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'aborted', 'failed',
                                           'execution_failed')),
    status_reason TEXT,
    decided_at TEXT,
    decided_by TEXT,
    executed_at TEXT,
    result TEXT,
    result_kind TEXT
);
-- Phase 3: Creative Check. Originals are what the advertiser approved; checks compare candidates to them.
CREATE TABLE IF NOT EXISTS creative_originals (
    id INTEGER PRIMARY KEY,
    registered_at TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    account_id TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    ad_ids TEXT NOT NULL DEFAULT '[]',
    snapshot_id INTEGER,
    fields TEXT NOT NULL,
    substantiation TEXT NOT NULL DEFAULT '[]',
    category TEXT,
    brand TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS creative_originals_by_creative ON creative_originals (account_id, creative_id, id);
CREATE TABLE IF NOT EXISTS creative_checks (
    id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    account_id TEXT,
    creative_id TEXT NOT NULL,
    original_id INTEGER REFERENCES creative_originals(id),
    status TEXT NOT NULL CHECK (status IN ('checked', 'no_original')),
    score INTEGER,
    band TEXT CHECK (band IN ('low', 'medium', 'high') OR band IS NULL),
    change_ids TEXT NOT NULL DEFAULT '[]',
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS creative_checks_by_time ON creative_checks (checked_at, id);
"""

APPROVAL_JSON_COLUMNS = ("declared_client", "arguments", "findings", "precheck", "result")
ORIGINAL_JSON_COLUMNS = ("registered_by", "ad_ids", "fields", "substantiation")
CHECK_JSON_COLUMNS = ("change_ids", "result")


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


@dataclass
class ChangeRecord:
    id: int
    detected_at: datetime
    account_id: str
    from_snapshot_id: int
    to_snapshot_id: int
    entity_type: str
    entity_id: str
    entity_name: str | None
    field: str
    change_type: str
    before: Any
    after: Any
    entity_updated_time: str | None
    actor_kind: str
    attribution: dict[str, Any]
    evaluated_at: str | None
    violation: bool
    severity: str | None
    findings: list[dict[str, Any]]

    @property
    def flagged(self) -> bool:
        return self.severity is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChangeRecord":
        return cls(
            id=row["id"],
            detected_at=parse_time(row["detected_at"]),
            account_id=row["account_id"],
            from_snapshot_id=row["from_snapshot_id"],
            to_snapshot_id=row["to_snapshot_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            entity_name=row["entity_name"],
            field=row["field"],
            change_type=row["change_type"],
            before=_load(row["before"]),
            after=_load(row["after"]),
            entity_updated_time=row["entity_updated_time"],
            actor_kind=row["actor_kind"],
            attribution=_load(row["attribution"]),
            evaluated_at=row["evaluated_at"],
            violation=bool(row["violation"]),
            severity=row["severity"],
            findings=_load(row["findings"]),
        )

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["detected_at"] = iso(self.detected_at)
        return d


class Ledger:
    def __init__(self, path: str | Path = ":memory:", *, clock: Callable[[], datetime] = now_utc):
        self.path = str(path)
        self.clock = clock  # bookkeeping times (stored_at, evaluated_at, alert/revert created_at)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self._migrate_approval_statuses()

    def _add_missing_columns(self) -> None:
        """Phase 7: `result_kind` (e.g. created_draft) on approvals and audit rows. Older ledgers get the column
        appended; existing rows keep NULL."""
        for table in ("approvals", "proxy_audit"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "result_kind" not in cols:
                with self.conn:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN result_kind TEXT")

    def _migrate_approval_statuses(self) -> None:
        """Ledgers made before Phase 5D have an approvals CHECK without 'execution_failed'. Rebuild that table
        with the current definition, copying every row unchanged (old statuses are not rewritten)."""
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'").fetchone()
        if row is None or "execution_failed" in row["sql"]:
            return
        start = SCHEMA.index("CREATE TABLE IF NOT EXISTS approvals")
        create = SCHEMA[start:SCHEMA.index(";", start) + 1].replace("IF NOT EXISTS approvals", "approvals_new")
        with self.conn:
            self.conn.execute(create)
            self.conn.execute("INSERT INTO approvals_new SELECT * FROM approvals")
            self.conn.execute("DROP TABLE approvals")
            self.conn.execute("ALTER TABLE approvals_new RENAME TO approvals")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- snapshots

    def save_snapshot(self, snap: Snapshot) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO snapshots (account_id, taken_at, api_version, source, stored_at) VALUES (?, ?, ?, ?, ?)",
                (snap.account_id, iso(snap.taken_at), snap.api_version, snap.source, iso(self.clock())),
            )
            sid = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO snapshot_entities (snapshot_id, entity_type, entity_id, data) VALUES (?, ?, ?, ?)",
                [(sid, t, eid, _dump(data)) for t, by_id in snap.entities.items() for eid, data in by_id.items()],
            )
            self.conn.executemany(
                "INSERT INTO activities (snapshot_id, seq, data) VALUES (?, ?, ?)",
                [(sid, i, _dump(a)) for i, a in enumerate(snap.activities)],
            )
        snap.id = sid
        return sid

    def load_snapshot(self, snapshot_id: int) -> Snapshot:
        row = self.conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"snapshot {snapshot_id} not found")
        entities: dict[str, dict[str, dict[str, Any]]] = {}
        for e in self.conn.execute(
            "SELECT entity_type, entity_id, data FROM snapshot_entities WHERE snapshot_id = ? ORDER BY entity_type, entity_id",
            (snapshot_id,),
        ):
            entities.setdefault(e["entity_type"], {})[e["entity_id"]] = json.loads(e["data"])
        activities = [json.loads(a["data"]) for a in self.conn.execute(
            "SELECT data FROM activities WHERE snapshot_id = ? ORDER BY seq", (snapshot_id,))]
        return Snapshot(row["account_id"], parse_time(row["taken_at"]), row["api_version"], row["source"],
                        entities, activities, id=row["id"])

    def load_entity(self, snapshot_id: int, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT data FROM snapshot_entities WHERE snapshot_id = ? AND entity_type = ? AND entity_id = ?",
            (snapshot_id, entity_type, entity_id),
        ).fetchone()
        return None if row is None else json.loads(row["data"])

    def previous_snapshot_id(self, account_id: str, snapshot_id: int) -> int | None:
        """Latest snapshot of the same account taken before the given one."""
        row = self.conn.execute(
            """SELECT s.id FROM snapshots s, snapshots cur
               WHERE cur.id = ? AND s.account_id = ? AND s.id != cur.id AND s.taken_at <= cur.taken_at
               ORDER BY s.taken_at DESC, s.id DESC LIMIT 1""",
            (snapshot_id, account_id),
        ).fetchone()
        return None if row is None else row["id"]

    def latest_snapshot_id(self, account_id: str | None = None) -> int | None:
        sql = "SELECT id FROM snapshots"
        args: tuple = ()
        if account_id:
            sql += " WHERE account_id = ?"
            args = (account_id,)
        row = self.conn.execute(sql + " ORDER BY taken_at DESC, id DESC LIMIT 1", args).fetchone()
        return None if row is None else row["id"]

    # ------------------------------------------------------------- changes

    def record_changes(
        self,
        account_id: str,
        from_snapshot_id: int,
        to_snapshot_id: int,
        detected_at: datetime,
        items: Iterable[tuple[FieldChange, Attribution]],
    ) -> list[int]:
        ids = []
        with self.conn:
            for change, attribution in items:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO changes (detected_at, account_id, from_snapshot_id, to_snapshot_id,
                       entity_type, entity_id, entity_name, field, change_type, before, after, entity_updated_time,
                       actor_kind, attribution)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (iso(detected_at), account_id, from_snapshot_id, to_snapshot_id, change.entity_type,
                     change.entity_id, change.entity_name, change.field, change.change_type, _dump(change.before),
                     _dump(change.after), change.entity_updated_time, attribution.actor_kind,
                     _dump(attribution.evidence)),
                )
                if cur.rowcount:
                    ids.append(cur.lastrowid)
        return ids

    def get_change(self, change_id: int) -> ChangeRecord:
        row = self.conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise KeyError(f"change {change_id} not found")
        return ChangeRecord.from_row(row)

    def feed(
        self,
        *,
        account_id: str | None = None,
        since: datetime | None = None,
        entity_id: str | None = None,
        flagged_only: bool = False,
        limit: int | None = None,
    ) -> list[ChangeRecord]:
        where, args = [], []
        if account_id:
            where.append("account_id = ?")
            args.append(account_id)
        if since:
            where.append("detected_at >= ?")
            args.append(iso(since))
        if entity_id:
            where.append("entity_id = ?")
            args.append(entity_id)
        if flagged_only:
            where.append("severity IS NOT NULL")
        sql = "SELECT * FROM changes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY detected_at, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [ChangeRecord.from_row(r) for r in self.conn.execute(sql, args)]

    def changes_to_evaluate(self, *, include_evaluated: bool = False) -> list[ChangeRecord]:
        sql = "SELECT * FROM changes"
        if not include_evaluated:
            sql += " WHERE evaluated_at IS NULL"
        return [ChangeRecord.from_row(r) for r in self.conn.execute(sql + " ORDER BY detected_at, id")]

    def set_evaluation(self, change_id: int, severity: str | None, findings: list[dict[str, Any]]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE changes SET evaluated_at = ?, violation = ?, severity = ?, findings = ? WHERE id = ?",
                (iso(self.clock()), int(severity == "violation"), severity, _dump(findings), change_id),
            )

    # ------------------------------------------------------------- alerts / reverts

    def record_alert(self, change_id: int, payload: dict[str, Any], deliveries: list[dict[str, Any]]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO alerts (change_id, created_at, payload, deliveries) VALUES (?, ?, ?, ?)",
                (change_id, iso(self.clock()), _dump(payload), _dump(deliveries)),
            )
        return cur.lastrowid

    def alerts_for(self, change_id: int) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM alerts WHERE change_id = ? ORDER BY id", (change_id,))]

    def record_revert_run(self, target: str, change_ids: list[int], plan: dict[str, Any],
                          results: list[dict[str, Any]]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO revert_runs (created_at, target, change_ids, plan, results) VALUES (?, ?, ?, ?, ?)",
                (iso(self.clock()), target, _dump(change_ids), _dump(plan), _dump(results)),
            )
        return cur.lastrowid

    # ------------------------------------------------------------- proxy audit / approvals (Phase 2)

    def record_audit(self, **row: Any) -> int:
        row = dict(row)
        for key in ("declared_client", "reasons"):
            if key in row:
                row[key] = _dump(row[key])
        cols = ", ".join(row)
        with self.conn:
            cur = self.conn.execute(f"INSERT INTO proxy_audit ({cols}) VALUES ({', '.join('?' * len(row))})",
                                    tuple(row.values()))
        return cur.lastrowid

    def audit_log(self, *, limit: int | None = None, approval_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM proxy_audit", []
        if approval_id:
            sql += " WHERE approval_id = ?"
            args.append(approval_id)
        sql += " ORDER BY occurred_at, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["declared_client"] = _load(d["declared_client"])
            d["reasons"] = _load(d["reasons"])
            out.append(d)
        return out

    def insert_approval(self, **row: Any) -> None:
        row = {k: (_dump(v) if k in APPROVAL_JSON_COLUMNS else v) for k, v in row.items()}
        with self.conn:
            self.conn.execute(f"INSERT INTO approvals ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              tuple(row.values()))

    @staticmethod
    def _approval(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in APPROVAL_JSON_COLUMNS:
            d[key] = _load(d[key])
        return d

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return None if row is None else self._approval(row)

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM approvals", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        return [self._approval(r) for r in self.conn.execute(sql + " ORDER BY received_at, id", args)]

    def transition_approval(self, approval_id: str, from_status: str, to_status: str, *,
                            unexpired_at: str | None = None, **cols: Any) -> bool:
        """Atomically move an approval from one status to another. Returns False if it was not in
        `from_status` (or, with `unexpired_at`, if it had already expired at that time)."""
        cols = {k: (_dump(v) if k in APPROVAL_JSON_COLUMNS else v) for k, v in cols.items()}
        sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in cols])
        sql = f"UPDATE approvals SET {sets} WHERE id = ? AND status = ?"
        args = [to_status, *cols.values(), approval_id, from_status]
        if unexpired_at is not None:
            sql += " AND expires_at > ?"
            args.append(unexpired_at)
        with self.conn:
            return self.conn.execute(sql, args).rowcount == 1

    # ------------------------------------------------------------- creative check (Phase 3)

    @staticmethod
    def _decode(row: sqlite3.Row, json_columns: tuple[str, ...]) -> dict[str, Any]:
        d = dict(row)
        for key in json_columns:
            d[key] = _load(d[key])
        return d

    def insert_original(self, **row: Any) -> int:
        row = {k: (_dump(v) if k in ORIGINAL_JSON_COLUMNS else v) for k, v in row.items()}
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO creative_originals ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()))
        return cur.lastrowid

    def get_original(self, original_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM creative_originals WHERE id = ?", (original_id,)).fetchone()
        return None if row is None else self._decode(row, ORIGINAL_JSON_COLUMNS)

    def list_originals(self, account_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM creative_originals", []
        if account_id:
            sql += " WHERE account_id = ?"
            args.append(account_id)
        return [self._decode(r, ORIGINAL_JSON_COLUMNS) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def latest_original_for(self, account_id: str, creative_id: str) -> dict[str, Any] | None:
        """A creative may be registered more than once (e.g. to add a substantiation note); the latest wins."""
        row = self.conn.execute(
            "SELECT * FROM creative_originals WHERE account_id = ? AND creative_id = ? ORDER BY id DESC LIMIT 1",
            (account_id, creative_id)).fetchone()
        return None if row is None else self._decode(row, ORIGINAL_JSON_COLUMNS)

    def ad_creative_history(self, account_id: str, ad_id: str) -> list[str]:
        """Creative ids an ad pointed at before, as recorded by Watch (`creative.id` changes)."""
        rows = self.conn.execute(
            """SELECT before FROM changes WHERE account_id = ? AND entity_type = 'ad' AND entity_id = ?
               AND field = 'creative.id' AND before IS NOT NULL ORDER BY detected_at, id""",
            (account_id, ad_id))
        return [str(_load(r["before"])) for r in rows]

    def record_creative_check(self, **row: Any) -> int:
        row = {k: (_dump(v) if k in CHECK_JSON_COLUMNS else v) for k, v in row.items()}
        row.setdefault("checked_at", iso(self.clock()))
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO creative_checks ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()))
        return cur.lastrowid

    def creative_checks(self) -> list[dict[str, Any]]:
        return [self._decode(r, CHECK_JSON_COLUMNS)
                for r in self.conn.execute("SELECT * FROM creative_checks ORDER BY checked_at, id")]

    # ------------------------------------------------------------- evidence export (Phase 3)

    def rows(self, table: str) -> list[dict[str, Any]]:
        """Raw rows of a ledger table in id order (JSON columns left as text)."""
        if table not in {"snapshots", "changes", "alerts", "revert_runs", "proxy_audit", "approvals"}:
            raise KeyError(f"unknown table {table}")
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

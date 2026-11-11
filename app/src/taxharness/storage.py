"""Independent SQLite storage for current tax-harness state."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import InvalidInputError, SourceIntegrityError
from .importer import load_workbook_data
from .models import WorkbookData
from .rules import _rule_view, migrate_initial_conditions, parse_rules_document, seed_rules
from .safe_json import safe_case_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_company_permissions (
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    can_view INTEGER NOT NULL,
    can_modify INTEGER NOT NULL,
    PRIMARY KEY(user_id, company_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS invoices (
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    seller_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    purpose TEXT,
    remarks TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    PRIMARY KEY(company_id, invoice_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS bank_records (
    company_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('收入','支出')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    counterparty TEXT NOT NULL,
    remarks TEXT,
    PRIMARY KEY(company_id, transaction_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    company_id TEXT,
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','inactive')),
    content TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    condition_json TEXT NOT NULL DEFAULT '{}',
    condition_status TEXT NOT NULL DEFAULT 'unsupported',
    condition_summary TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(rule_id, version)
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, company_id, object_type, object_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    month TEXT,
    scope TEXT NOT NULL CHECK(scope IN ('all_missing','selected')),
    target_invoice_ids_json TEXT NOT NULL,
    pending_invoice_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting_input','open','out_of_scope','completed','cancelled','superseded','deleted')),
    message TEXT NOT NULL,
    next_step TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_scope_lookup ON tasks(user_id, company_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_unfinished_per_scope
    ON tasks(user_id, company_id)
    WHERE status IN ('waiting_input','open','out_of_scope');
CREATE TABLE IF NOT EXISTS task_ledger_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_ledger_scope ON task_ledger_events(user_id, company_id, sequence);
CREATE TABLE IF NOT EXISTS invoice_action_plans (
    plan_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted','completed','revoked')),
    created_at TEXT NOT NULL,
    UNIQUE(user_id,company_id,invoice_id,session_id,turn_id,payload_hash)
);
CREATE TABLE IF NOT EXISTS invoice_action_receipts (
    plan_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(plan_id,action_id),
    FOREIGN KEY(plan_id) REFERENCES invoice_action_plans(plan_id)
);
CREATE TABLE IF NOT EXISTS invoice_action_requests (
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(user_id,company_id,request_key)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_task_event(connection: sqlite3.Connection, *, user_id: str, company_id: str, kind: str,
                      payload: dict[str, Any], session_id: str = "", turn_id: str = "",
                      task_id: str | None = None, occurred_at: str | None = None) -> str:
    """Append within the caller's transaction; business updates remain authoritative."""

    event_id = "ledger-" + uuid4().hex
    now = _now()
    connection.execute(
        """INSERT INTO task_ledger_events
           (event_id,user_id,company_id,session_id,turn_id,task_id,kind,payload_json,occurred_at,recorded_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (event_id, user_id, company_id, session_id, turn_id, task_id, kind,
         json.dumps(payload, ensure_ascii=False, sort_keys=True), occurred_at or now, now),
    )
    return event_id


class TaxDatabase:
    """One independent database connection; no source project state is opened."""

    def __init__(self, path: str | Path, *, check_same_thread: bool = True):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=check_same_thread)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        rule_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(rules)")}
        if "condition_json" not in rule_columns:
            self.connection.execute("ALTER TABLE rules ADD COLUMN condition_json TEXT NOT NULL DEFAULT '{}'" )
        if "condition_status" not in rule_columns:
            self.connection.execute("ALTER TABLE rules ADD COLUMN condition_status TEXT NOT NULL DEFAULT 'unsupported'" )
        if "condition_summary" not in rule_columns:
            self.connection.execute("ALTER TABLE rules ADD COLUMN condition_summary TEXT NOT NULL DEFAULT ''" )
        rules_doc = self.get_meta("rules_doc_path")
        if rules_doc and Path(rules_doc).is_file():
            try:
                migrate_initial_conditions(self.connection, parse_rules_document(rules_doc))
            except (OSError, InvalidInputError):
                # The business store remains usable; missing/changed source is handled by init-db.
                pass
        self.bootstrap_ledger()

    def bootstrap_ledger(self) -> None:
        """Capture an existing database's initial task/rule state once."""

        if self.get_meta("taskledger_bootstrapped"):
            return
        has_data = self.connection.execute("SELECT 1 FROM rules LIMIT 1").fetchone() or self.connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
        if not has_data:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.get_meta("taskledger_bootstrapped"):
                self.connection.rollback()
                return
            rules = self.connection.execute("SELECT * FROM rules ORDER BY rule_id,effective_from,version").fetchall()
            for row in rules:
                rule = _rule_view(row)
                record_task_event(self.connection, user_id="__system__", company_id=rule["company_id"] or "*", kind="rule_changed",
                    payload={"rule_id": rule["rule_id"], "version": rule["version"], "kind": rule["kind"],
                             "effective_from": rule["effective_from"], "status": rule["status"], "condition": rule["condition"]})
            for row in self.connection.execute("SELECT * FROM tasks ORDER BY created_at,task_id").fetchall():
                task = dict(row)
                task["target_invoice_ids"] = json.loads(task.pop("target_invoice_ids_json"))
                task["pending_invoice_ids"] = json.loads(task.pop("pending_invoice_ids_json"))
                record_task_event(self.connection, user_id=task["user_id"], company_id=task["company_id"],
                                  task_id=task["task_id"], kind="task_imported", payload={"after": task})
            self.set_meta("taskledger_bootstrapped", "1")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def counts(self) -> dict[str, int]:
        tables = ("companies", "users", "user_company_permissions", "invoices", "bank_records", "rules")
        return {table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def initialize_database(
    database_path: str | Path,
    source_xlsx: str | Path,
    rules_doc: str | Path,
) -> dict[str, Any]:
    """Read source files and seed a fresh/reusable independent database.

    Existing current business values are not overwritten when the recorded source
    hashes match. A different source is rejected instead of silently replacing it.
    """

    workbook = load_workbook_data(source_xlsx)
    from .rules import parse_rules_document

    rules = parse_rules_document(rules_doc)
    with TaxDatabase(database_path) as database:
        existing_xlsx = database.get_meta("source_xlsx_sha256")
        existing_rules = database.get_meta("rules_doc_sha256")
        if existing_xlsx and existing_xlsx != workbook.source_sha256:
            raise SourceIntegrityError("当前数据库已绑定另一份 Excel；请使用新数据库路径。")
        rules_digest = rules[0].source_sha256 if rules else ""
        if existing_rules and existing_rules != rules_digest:
            raise SourceIntegrityError("当前数据库已绑定另一份业务规则文档；请使用新数据库路径。")
        database.connection.execute("BEGIN IMMEDIATE")
        try:
            migrate_initial_conditions(database.connection, rules)
            for company in workbook.companies:
                database.connection.execute(
                    "INSERT OR IGNORE INTO companies(company_id,name,short_name) VALUES(?,?,?)",
                    (company["company_id"], company["name"], company["short_name"]),
                )
            for user in workbook.users:
                database.connection.execute(
                    "INSERT OR IGNORE INTO users(user_id,name) VALUES(?,?)",
                    (user["user_id"], user["name"]),
                )
            for permission in workbook.permissions:
                database.connection.execute(
                    """INSERT OR IGNORE INTO user_company_permissions
                       (user_id,company_id,can_view,can_modify) VALUES(?,?,?,?)""",
                    (permission["user_id"], permission["company_id"], int(permission["can_view"]), int(permission["can_modify"])),
                )
            for invoice in workbook.invoices:
                database.connection.execute(
                    """INSERT OR IGNORE INTO invoices
                       (company_id,invoice_id,issue_date,seller_name,project_name,amount_cents,purpose,remarks,revision)
                       VALUES(?,?,?,?,?,?,?,?,1)""",
                    (invoice["company_id"], invoice["invoice_id"], invoice["issue_date"], invoice["seller_name"],
                     invoice["project_name"], invoice["amount_cents"], invoice["purpose"], invoice["remarks"]),
                )
            for record in workbook.bank_records:
                database.connection.execute(
                    """INSERT OR IGNORE INTO bank_records
                       (company_id,transaction_id,transaction_date,direction,amount_cents,counterparty,remarks)
                       VALUES(?,?,?,?,?,?,?)""",
                    (record["company_id"], record["transaction_id"], record["transaction_date"], record["direction"],
                     record["amount_cents"], record["counterparty"], record["remarks"]),
                )
            seed_rules(database.connection, rules_doc)
            database.set_meta("source_xlsx_path", workbook.source_path)
            database.set_meta("source_xlsx_sha256", workbook.source_sha256)
            database.set_meta("rules_doc_path", str(Path(rules_doc).expanduser().resolve()))
            database.set_meta("rules_doc_sha256", rules_digest)
            database.set_meta("schema_version", "1")
            database.connection.commit()
        except Exception:
            database.connection.rollback()
            raise
        database.bootstrap_ledger()
        counts = database.counts()
    return {
        "status": "ok",
        "database": str(Path(database_path).expanduser().resolve()),
        "source_xlsx_sha256": workbook.source_sha256,
        "rules_doc_sha256": rules_digest,
        "counts": counts,
    }


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def database_initialized(path: str | Path) -> bool:
    """Check an existing database without creating an empty SQLite file."""

    database = Path(path).expanduser().resolve()
    if not database.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"meta", "companies", "users", "user_company_permissions", "invoices", "bank_records", "rules"}.issubset(tables):
                return False
            meta = {row[0]: row[1] for row in connection.execute("SELECT key,value FROM meta")}
            return bool(meta.get("source_xlsx_sha256") and meta.get("rules_doc_sha256") and connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0] > 0 and connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0)
        finally:
            connection.close()
    except sqlite3.Error:
        return False

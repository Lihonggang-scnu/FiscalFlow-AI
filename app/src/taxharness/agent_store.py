"""Small durable store for agent sessions, turns, pending writes and budget."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import BudgetExceeded, SessionOwnershipError
from .safe_json import safe_case_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    current_company_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request_text TEXT NOT NULL,
    input_context_json TEXT NOT NULL,
    status TEXT NOT NULL,
    output_text TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_turns_session ON agent_turns(session_id, created_at);
CREATE TABLE IF NOT EXISTS agent_operations (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_operations_lookup ON agent_operations(session_id, user_id, company_id, object_type, object_id, status);
CREATE TABLE IF NOT EXISTS agent_model_calls (
    call_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    error TEXT,
    input_json TEXT,
    output_json TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_operations)")}
            if "turn_id" not in columns:
                connection.execute("ALTER TABLE agent_operations RENAME TO agent_operations_legacy")
                connection.execute("""CREATE TABLE agent_operations (
                    operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, company_id TEXT NOT NULL, object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                connection.execute(
                    """INSERT INTO agent_operations(operation_id,session_id,turn_id,user_id,company_id,object_type,object_id,
                       payload_json,payload_hash,expected_revision,status,result_json,created_at,updated_at)
                       SELECT operation_id,session_id,'',user_id,company_id,object_type,object_id,payload_json,
                       payload_hash,expected_revision,status,result_json,created_at,updated_at FROM agent_operations_legacy"""
                )
                connection.execute("DROP TABLE agent_operations_legacy")
            call_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_model_calls)")}
            if "input_json" not in call_columns:
                connection.execute("ALTER TABLE agent_model_calls ADD COLUMN input_json TEXT")
            if "output_json" not in call_columns:
                connection.execute("ALTER TABLE agent_model_calls ADD COLUMN output_json TEXT")
            if "turn_id" not in call_columns:
                connection.execute("ALTER TABLE agent_model_calls ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")

    def _raw_connection(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connect(self):
        connection = self._raw_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ensure_session(self, session_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is not None and row["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            if row is None:
                connection.execute("INSERT INTO agent_sessions VALUES(?,?,?,?,?,0)", (session_id, user_id, company_id, now, now))
            else:
                connection.execute("UPDATE agent_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            return dict(connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone())

    def session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is not None and row["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            return dict(row) if row is not None else None

    def set_company(self, session_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT user_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError("session 不存在。")
            if row["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            connection.execute("UPDATE agent_sessions SET current_company_id=?,updated_at=? WHERE session_id=?", (company_id, _now(), session_id))
            return dict(connection.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone())

    def set_cancel(self, session_id: str, user_id: str, requested: bool = True) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT user_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return False
            if row["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            connection.execute("UPDATE agent_sessions SET cancel_requested=?,updated_at=? WHERE session_id=?", (int(requested), _now(), session_id))
            return True

    def cancel_requested(self, session_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT user_id,cancel_requested FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return False
            if row["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            return bool(row["cancel_requested"])

    def start_turn(self, session_id: str, user_id: str, request_text: str, context: dict[str, Any]) -> str:
        turn_id = "turn-" + uuid4().hex
        now = _now()
        with self._connect() as connection:
            connection.execute("INSERT INTO agent_turns VALUES(?,?,?,?,?,?,?,?,?,?)", (turn_id, session_id, user_id, request_text, json.dumps(safe_case_json(context), ensure_ascii=False), "running", None, None, now, now))
        return turn_id

    def finish_turn(self, turn_id: str, *, status: str, output_text: str = "", result: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_turns SET status=?,output_text=?,result_json=?,updated_at=? WHERE turn_id=?", (status, output_text, json.dumps(safe_case_json(result or {}), ensure_ascii=False), _now(), turn_id))

    def list_turns(self, session_id: str, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            owner = connection.execute("SELECT user_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if owner is None:
                return []
            if owner["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            rows = connection.execute("SELECT * FROM agent_turns WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?", (session_id, user_id, max(1, min(limit, 100)))).fetchall()
            return [dict(row) for row in rows]

    def get_turn(self, turn_id: str, user_id: str) -> dict[str, Any] | None:
        """Read one turn belonging to this user, including across sessions."""

        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_turns WHERE turn_id=? AND user_id=?", (turn_id, user_id)).fetchone()
            return dict(row) if row is not None else None

    def list_all_turns(self, session_id: str, user_id: str) -> list[dict[str, Any]]:
        """Read every turn in one owned session, in newest-first order."""

        with self._connect() as connection:
            owner = connection.execute("SELECT user_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if owner is None:
                return []
            if owner["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
            rows = connection.execute(
                "SELECT * FROM agent_turns WHERE session_id=? AND user_id=? ORDER BY created_at DESC,turn_id DESC",
                (session_id, user_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_model_calls(self, session_id: str, user_id: str, limit: int = 10, turn_id: str | None = None) -> list[dict[str, Any]]:
        """Read recent model input/output records for a local diagnostic view."""

        with self._connect() as connection:
            owner = connection.execute("SELECT user_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
            if owner is None:
                return []
            if owner["user_id"] != user_id:
                raise SessionOwnershipError("该 session 不属于当前用户。")
        ledger_path = os.getenv("TAXHARNESS_BUDGET_STORE", "").strip()
        if ledger_path:
            uri = Path(ledger_path).expanduser().resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30)
            connection.row_factory = sqlite3.Row
        else:
            connection = self._raw_connection()
        with closing(connection), connection:
            if turn_id:
                rows = connection.execute(
                    "SELECT * FROM agent_model_calls WHERE session_id=? AND user_id=? AND turn_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, user_id, turn_id, max(1, min(limit, 50))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM agent_model_calls WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, user_id, max(1, min(limit, 50))),
                ).fetchall()
            return [dict(row) for row in rows]

    def latest_running_turn(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_turns WHERE session_id=? AND user_id=? AND status='running' ORDER BY created_at DESC LIMIT 1", (session_id, user_id)).fetchone()
            return dict(row) if row is not None else None

    def latest_recoverable_turn(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM agent_turns WHERE session_id=? AND user_id=? AND status IN ('running','error','cancelled') ORDER BY created_at DESC LIMIT 1", (session_id, user_id)).fetchone()
            return dict(row) if row is not None else None

    def get_or_create_operation(self, *, session_id: str, turn_id: str, user_id: str, company_id: str, object_type: str, object_id: str, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        payload_json = json.dumps(safe_case_json(payload), ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            prior = connection.execute(
                """SELECT * FROM agent_operations WHERE session_id=? AND turn_id=? AND user_id=? AND company_id=?
                   AND object_type=? AND object_id=? AND status IN ('pending','committed','unknown')
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id, turn_id, user_id, company_id, object_type, object_id),
            ).fetchone()
            if prior is not None and prior["payload_hash"] == payload_hash:
                return dict(prior)
            operation_id = f"{user_id}:{company_id}:{object_type}:{object_id}:{uuid4().hex[:16]}"
            now = _now()
            connection.execute(
                "INSERT INTO agent_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, session_id, turn_id, user_id, company_id, object_type, object_id, payload_json, payload_hash, expected_revision, "pending", None, now, now),
            )
            return dict(connection.execute("SELECT * FROM agent_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def finish_operation(self, operation_id: str, *, status: str, result: dict[str, Any] | None = None) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_operations SET status=?,result_json=?,updated_at=? WHERE operation_id=?", (status, json.dumps(safe_case_json(result or {}), ensure_ascii=False), _now(), operation_id))

    def find_operation(self, *, session_id: str, turn_id: str, user_id: str, company_id: str, object_type: str, object_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM agent_operations WHERE session_id=? AND turn_id=? AND user_id=? AND company_id=?
                   AND object_type=? AND object_id=? ORDER BY created_at DESC LIMIT 1""",
                (session_id, turn_id, user_id, company_id, object_type, object_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def operation_for_turn(self, *, session_id: str, turn_id: str, user_id: str, company_id: str, object_type: str, object_id: str) -> dict[str, Any] | None:
        return self.find_operation(session_id=session_id, turn_id=turn_id, user_id=user_id, company_id=company_id, object_type=object_type, object_id=object_id)

    def reserve_model_call(self, *, session_id: str, user_id: str, model: str, estimated_input_tokens: int, estimated_output_tokens: int, budget_usd: float, input_payload: dict[str, Any] | None = None, turn_id: str = "", input_rate: float = 0.30, output_rate: float = 1.20) -> str:
        reserve = Decimal(str(input_rate)) * Decimal(max(0, estimated_input_tokens)) / Decimal(1_000_000) + Decimal(str(output_rate)) * Decimal(max(0, estimated_output_tokens)) / Decimal(1_000_000)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            spent = connection.execute("SELECT COALESCE(SUM(CASE WHEN status IN ('pending','unknown') THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls").fetchone()[0]
            if Decimal(str(spent)) + reserve > Decimal(str(budget_usd)):
                connection.rollback()
                raise BudgetExceeded("模型调用预算不足；本次请求未发出。")
            call_id = "call-" + uuid4().hex
            connection.execute("""INSERT INTO agent_model_calls
                (call_id,session_id,user_id,turn_id,model,status,estimated_input_tokens,estimated_output_tokens,reserved_usd,
                 actual_usd,input_tokens,output_tokens,error,input_json,output_json,created_at,settled_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (call_id, session_id, user_id, turn_id, model, "pending", estimated_input_tokens, estimated_output_tokens, float(reserve), None, None, None, None, json.dumps(safe_case_json(input_payload or {}), ensure_ascii=False), None, _now(), None))
            connection.commit()
            return call_id

    def settle_model_call(self, call_id: str, input_tokens: int, output_tokens: int, output_payload: dict[str, Any] | None = None, *, input_rate: float = 0.30, output_rate: float = 1.20) -> None:
        actual = float(Decimal(str(input_rate)) * Decimal(max(0, input_tokens)) / Decimal(1_000_000) + Decimal(str(output_rate)) * Decimal(max(0, output_tokens)) / Decimal(1_000_000))
        with self._connect() as connection:
            connection.execute("UPDATE agent_model_calls SET status='settled',actual_usd=?,input_tokens=?,output_tokens=?,output_json=?,settled_at=? WHERE call_id=?", (actual, input_tokens, output_tokens, json.dumps(safe_case_json(output_payload or {}), ensure_ascii=False), _now(), call_id))

    def mark_model_unknown(self, call_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_model_calls SET status='unknown',error=?,settled_at=? WHERE call_id=?", (error[:500], _now(), call_id))

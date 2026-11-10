"""Small index of Chainlit conversations backed by AgentStore and checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .errors import SessionOwnershipError


@dataclass(frozen=True)
class HistoryEntry:
    session_id: str
    user_id: str
    database_scope: str
    checkpoint_scope: str
    session_company_id: str
    company_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    turn_count: int
    latest_status: str


def path_scope(path: str | Path) -> str:
    normalized = Path(path).expanduser().resolve().as_posix().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def checkpoint_thread_ids(path: str | Path) -> set[str]:
    checkpoint_path = Path(path).expanduser().resolve()
    if not checkpoint_path.is_file():
        return set()
    uri = "file:" + quote(checkpoint_path.as_posix(), safe="/:\\") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        if exists is None:
            return set()
        return {
            str(row[0])
            for row in connection.execute("SELECT DISTINCT thread_id FROM checkpoints")
        }
    finally:
        connection.close()


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _company_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        result = {value["company_id"]} if isinstance(value.get("company_id"), str) else set()
        for child in value.values():
            result.update(_company_ids(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_company_ids(child))
        return result
    if isinstance(value, str):
        parsed = _json_value(value)
        return _company_ids(parsed) if parsed is not value else set()
    return set()


def _permission_denied_without_data(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("status") != "permission_denied":
        return False
    data = value.get("data")
    return data is None or data == {} or data == [] or data == ""


def companies_used(session: dict[str, Any], turns: list[dict[str, Any]]) -> tuple[str, ...]:
    """Collect companies used by successful, unknown, or unfinished tool calls."""

    companies = {str(session["current_company_id"])}
    for turn in turns:
        context = _json_value(turn.get("input_context_json"))
        if isinstance(context, dict) and context.get("current_company_id"):
            companies.add(str(context["current_company_id"]))
        result = _json_value(turn.get("result_json"))
        if not isinstance(result, dict):
            continue
        if result.get("current_company_id"):
            companies.add(str(result["current_company_id"]))
        calls: dict[str, dict[str, Any]] = {}
        unmatched_calls: list[dict[str, Any]] = []
        tool_results: list[tuple[str | None, Any]] = []
        for message in result.get("messages", []):
            if not isinstance(message, dict):
                continue
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            if message.get("type") == "ai":
                for call in data.get("tool_calls", []):
                    if isinstance(call, dict):
                        call_id = call.get("id")
                        if isinstance(call_id, str) and call_id:
                            calls[call_id] = call
                        else:
                            unmatched_calls.append(call)
            elif message.get("type") == "tool":
                call_id = data.get("tool_call_id")
                tool_results.append((call_id if isinstance(call_id, str) else None, _json_value(data.get("content", ""))))
        results_by_id = {call_id: value for call_id, value in tool_results if call_id}
        for call_id, call in calls.items():
            tool_result = results_by_id.get(call_id)
            if not _permission_denied_without_data(tool_result):
                companies.update(_company_ids(call.get("args", {})))
        companies.update(
            company_id
            for call in unmatched_calls
            for company_id in _company_ids(call.get("args", {}))
        )
        for _call_id, tool_result in tool_results:
            if not _permission_denied_without_data(tool_result):
                companies.update(_company_ids(tool_result))
    return tuple(sorted(companies))


class ConversationHistoryStore:
    """Store pointers and ACL scope only; transcript text stays in AgentStore."""

    def __init__(self, path: str | Path, database_path: str | Path, checkpoint_path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database_scope = path_scope(database_path)
        self.checkpoint_scope = path_scope(checkpoint_path)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_history (
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    database_scope TEXT NOT NULL,
                    checkpoint_scope TEXT NOT NULL,
                    session_company_id TEXT NOT NULL,
                    company_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    turn_count INTEGER NOT NULL,
                    latest_status TEXT NOT NULL,
                    PRIMARY KEY(session_id, user_id, database_scope, checkpoint_scope)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS conversation_history_owner
                   ON conversation_history(user_id, database_scope, checkpoint_scope, updated_at)"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def upsert(self, session: dict[str, Any], turns: list[dict[str, Any]]) -> None:
        if not turns:
            return
        user_id = str(session["user_id"])
        session_id = str(session["session_id"])
        companies = companies_used(session, turns)
        latest = max(turns, key=lambda turn: str(turn.get("created_at", "")))
        with self._connect() as connection:
            existing = connection.execute(
                """SELECT user_id FROM conversation_history
                   WHERE session_id=? AND database_scope=? AND checkpoint_scope=?
                   LIMIT 1""",
                (session_id, self.database_scope, self.checkpoint_scope),
            ).fetchone()
            if existing is not None and existing["user_id"] != user_id:
                raise SessionOwnershipError("该历史对话不属于当前用户。")
            connection.execute(
                """INSERT INTO conversation_history
                   (session_id,user_id,database_scope,checkpoint_scope,session_company_id,
                    company_ids_json,created_at,updated_at,turn_count,latest_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id,user_id,database_scope,checkpoint_scope) DO UPDATE SET
                    session_company_id=excluded.session_company_id,
                    company_ids_json=excluded.company_ids_json,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    turn_count=excluded.turn_count,
                    latest_status=excluded.latest_status""",
                (
                    session_id,
                    user_id,
                    self.database_scope,
                    self.checkpoint_scope,
                    str(session["current_company_id"]),
                    json.dumps(companies, ensure_ascii=False),
                    str(session["created_at"]),
                    str(latest.get("updated_at") or session["updated_at"]),
                    len(turns),
                    str(latest.get("status", "unknown")),
                ),
            )

    def get(self, session_id: str, user_id: str) -> HistoryEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM conversation_history
                   WHERE session_id=? AND user_id=? AND database_scope=? AND checkpoint_scope=?""",
                (session_id, user_id, self.database_scope, self.checkpoint_scope),
            ).fetchone()
        return self._entry(row) if row else None

    def list(self, user_id: str, limit: int = 50, offset: int = 0) -> list[HistoryEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM conversation_history
                   WHERE user_id=? AND database_scope=? AND checkpoint_scope=?
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (user_id, self.database_scope, self.checkpoint_scope, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
        return [self._entry(row) for row in rows]

    @staticmethod
    def _entry(row: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            session_id=str(row["session_id"]),
            user_id=str(row["user_id"]),
            database_scope=str(row["database_scope"]),
            checkpoint_scope=str(row["checkpoint_scope"]),
            session_company_id=str(row["session_company_id"]),
            company_ids=tuple(json.loads(row["company_ids_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            turn_count=int(row["turn_count"]),
            latest_status=str(row["latest_status"]),
        )

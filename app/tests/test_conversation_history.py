from __future__ import annotations

import json
import sqlite3

import pytest

from taxharness.agent_store import AgentStore
from taxharness.conversation_history import ConversationHistoryStore, companies_used
from taxharness.errors import SessionOwnershipError, TaxHarnessError


def test_companies_used_ignores_available_company_catalog_and_reads_tool_args_and_results():
    session = {"current_company_id": "C001"}
    turns = [{
        "input_context_json": json.dumps({
            "current_company_id": "C001",
            "available_companies": ["C001", "C099"],
        }),
        "result_json": json.dumps({
            "current_company_id": "C001",
            "messages": [
                {"type": "ai", "data": {"tool_calls": [
                    {"id": "call-1", "name": "query_finance", "args": {"company_id": "C002"}},
                    {"id": "call-unfinished", "name": "query_finance", "args": {"company_id": "C004"}},
                ]}},
                {"type": "tool", "data": {"tool_call_id": "call-1", "content": json.dumps({"status": "ok", "data": {"company_id": "C003"}})}},
            ],
        }),
    }]

    assert companies_used(session, turns) == ("C001", "C002", "C003", "C004")


def test_permission_denied_tool_without_business_data_does_not_add_denied_company():
    session = {"current_company_id": "C001"}
    turn = {
        "input_context_json": json.dumps({"current_company_id": "C001", "available_companies": ["C002"]}),
        "result_json": json.dumps({
            "current_company_id": "C001",
            "messages": [
                {"type": "ai", "data": {"tool_calls": [{"id": "call-1", "name": "query_finance", "args": {"company_id": "C002"}}]}},
                {"type": "tool", "data": {"tool_call_id": "call-1", "content": json.dumps({"status": "permission_denied", "summary": "无权访问"})}},
            ],
        }),
    }

    assert companies_used(session, [turn]) == ("C001",)


def test_history_session_list_pages_older_entries_without_dropping_them(tmp_path):
    history = ConversationHistoryStore(
        tmp_path / "history.sqlite3",
        tmp_path / "business.sqlite3",
        tmp_path / "checkpoints.sqlite3",
    )
    for number in range(3):
        history.upsert(
            {
                "session_id": f"chainlit-{number}",
                "user_id": "U001",
                "current_company_id": "C001",
                "created_at": f"2026-10-27T00:0{number}:00+00:00",
                "updated_at": f"2026-10-27T00:0{number}:00+00:00",
            },
            [{"created_at": f"2026-10-27T00:0{number}:00+00:00", "updated_at": f"2026-10-27T00:0{number}:00+00:00", "status": "ok"}],
        )

    first_page = history.list("U001", limit=2, offset=0)
    second_page = history.list("U001", limit=2, offset=2)
    assert [entry.session_id for entry in first_page + second_page] == ["chainlit-2", "chainlit-1", "chainlit-0"]


def test_history_backfill_is_user_and_dataset_scoped_and_keeps_all_turns(tmp_path, monkeypatch):
    import scripts.chainlit_app as app

    agent_path = tmp_path / "agent.sqlite3"
    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    database_path = tmp_path / "business.sqlite3"
    history_path = tmp_path / "conversation-history.sqlite3"
    store = AgentStore(agent_path)
    session_id = "chainlit-owned"
    store.ensure_session(session_id, "U001", "C001")
    for number in range(105):
        turn_id = store.start_turn(
            session_id,
            "U001",
            f"question {number}",
            {"current_company_id": "C001", "available_companies": ["C001", "C099"]},
        )
        result = {"status": "ok", "current_company_id": "C001", "messages": []}
        if number == 0:
            result["messages"] = [
                {"type": "ai", "data": {"tool_calls": [{"id": "call-c002", "name": "query_finance", "args": {"company_id": "C002"}}]}},
                {"type": "tool", "data": {"tool_call_id": "call-c002", "content": json.dumps({"status": "ok", "data": {"company_id": "C003"}})}},
            ]
        store.finish_turn(turn_id, status="ok", output_text=f"answer {number}", result=result)

    store.ensure_session("chainlit-other-user", "U002", "C001")
    other_turn = store.start_turn("chainlit-other-user", "U002", "private", {"current_company_id": "C001"})
    store.finish_turn(other_turn, status="ok", output_text="private answer", result={
        "status": "ok",
        "current_company_id": "C001",
        "messages": [
            {"type": "ai", "data": {"tool_calls": [{"id": "call-denied", "name": "query_finance", "args": {"company_id": "C002"}}]}},
            {"type": "tool", "data": {"tool_call_id": "call-denied", "content": json.dumps({"status": "permission_denied", "summary": "无权访问"})}},
        ],
    })
    with sqlite3.connect(checkpoint_path) as connection:
        connection.execute("CREATE TABLE checkpoints(thread_id TEXT, checkpoint_id TEXT)")
        connection.executemany(
            "INSERT INTO checkpoints VALUES(?,?)",
            [(session_id, "cp-1"), ("chainlit-other-user", "cp-2")],
        )

    config = {
        "agent_store": agent_path,
        "checkpoint_store": checkpoint_path,
        "database": database_path,
        "history_store": history_path,
    }
    history = app._refresh_history("U001", config)
    entries = history.list("U001")
    assert [entry.session_id for entry in entries] == [session_id]
    assert entries[0].turn_count == 105
    assert entries[0].company_ids == ("C001", "C002", "C003")
    assert history.list("U002") == []

    checked_companies: list[str] = []
    monkeypatch.setattr(app, "_check_access", lambda _user, company, _db: checked_companies.append(company))
    state = app.UiState("U001", "C001", "fresh-ui-session")
    resumed_session, turns = app._load_history_conversation(state, session_id, config, history)
    assert resumed_session["session_id"] == session_id
    assert len(turns) == 105
    assert turns[-1]["request_text"] == "question 104"
    assert set(checked_companies) == {"C001", "C002", "C003"}

    u2_history = app._refresh_history("U002", config)
    u2_entries = u2_history.list("U002")
    assert len(u2_entries) == 1
    assert u2_entries[0].company_ids == ("C001",)
    u2_session, u2_turns = app._load_history_conversation(
        app.UiState("U002", "C001", "fresh-ui-session-u2"),
        "chainlit-other-user",
        config,
        u2_history,
    )
    assert u2_session["user_id"] == "U002"
    assert len(u2_turns) == 1

    def deny_revoked_company(_user: str, company: str, _database) -> None:
        if company == "C002":
            raise TaxHarnessError("revoked")

    monkeypatch.setattr(app, "_check_access", deny_revoked_company)
    with pytest.raises(app.HistoryUnavailable, match="当前无权访问"):
        app._load_history_conversation(state, session_id, config, history)

    other_dataset = ConversationHistoryStore(history_path, tmp_path / "other-business.sqlite3", checkpoint_path)
    assert other_dataset.list("U001") == []
    with pytest.raises(app.HistoryUnavailable):
        app._load_history_conversation(app.UiState("U002", "C001", "new-ui-session"), session_id, config, history)
    with pytest.raises(SessionOwnershipError):
        store.list_all_turns(session_id, "U002")

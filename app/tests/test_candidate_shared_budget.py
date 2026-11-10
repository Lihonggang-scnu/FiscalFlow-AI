from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from taxharness.agent import AgentRuntime, BudgetMiddleware
from taxharness.agent_store import AgentStore
from taxharness.agent_tools import AgentContext
from taxharness.errors import BudgetExceeded, LiveModelDisabled, SessionOwnershipError
from taxharness.provider import _peak_window
from taxharness.storage import initialize_database


def request(context):
    return SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        system_message=SystemMessage(content="只读取当前资料"),
        messages=[HumanMessage(content="查询九月")],
        tools=[],
    )


def test_candidate_sessions_are_local_but_usage_and_diagnostics_use_shared_ledger(tmp_path, monkeypatch):
    local = AgentStore(tmp_path / "candidate.sqlite3")
    shared = AgentStore(tmp_path / "shared.sqlite3")
    local.ensure_session("candidate-session", "U001", "C001")
    context = AgentContext(
        user_id="U001", session_id="candidate-session", database_path=str(tmp_path / "business.sqlite3"),
        agent_store_path=str(local.path), current_company_id="C001", budget_ledger_path=str(shared.path),
        budget_usd=1.0, max_output_tokens=100, turn_id="candidate-turn",
    )
    reply = AIMessage(content="已查询", usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25})
    BudgetMiddleware().wrap_model_call(request(context), lambda _request: SimpleNamespace(result=[reply]))

    with sqlite3.connect(shared.path) as connection:
        assert connection.execute("SELECT count(*) FROM agent_model_calls").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM agent_sessions").fetchone()[0] == 0
    with sqlite3.connect(local.path) as connection:
        assert connection.execute("SELECT count(*) FROM agent_model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT user_id FROM agent_sessions WHERE session_id='candidate-session'").fetchone()[0] == "U001"

    monkeypatch.setenv("TAXHARNESS_BUDGET_STORE", str(shared.path))
    calls = local.list_model_calls("candidate-session", "U001", turn_id="candidate-turn")
    assert len(calls) == 1 and calls[0]["status"] == "settled"
    with pytest.raises(SessionOwnershipError):
        local.list_model_calls("candidate-session", "U002", turn_id="candidate-turn")


def test_pending_reservation_in_shared_ledger_prevents_new_candidate_request(tmp_path):
    local = AgentStore(tmp_path / "candidate.sqlite3")
    shared = AgentStore(tmp_path / "shared.sqlite3")
    shared.reserve_model_call(session_id="other", user_id="U001", model="deepseek-flash", estimated_input_tokens=1000, estimated_output_tokens=1000, budget_usd=1)
    context = AgentContext(
        user_id="U001", session_id="candidate", database_path=str(tmp_path / "business.sqlite3"),
        agent_store_path=str(local.path), current_company_id="C001", budget_ledger_path=str(shared.path),
        budget_usd=0.0015, max_output_tokens=100, turn_id="turn",
    )
    with pytest.raises(BudgetExceeded):
        BudgetMiddleware().wrap_model_call(request(context), lambda _request: pytest.fail("request must not be sent"))


def test_usage_unknown_retains_shared_reservation(tmp_path):
    local = AgentStore(tmp_path / "candidate.sqlite3")
    shared = AgentStore(tmp_path / "shared.sqlite3")
    context = AgentContext(
        user_id="U001", session_id="candidate", database_path=str(tmp_path / "business.sqlite3"),
        agent_store_path=str(local.path), current_company_id="C001", budget_ledger_path=str(shared.path),
        budget_usd=1.0, max_output_tokens=100, turn_id="turn",
    )
    def timeout(_request):
        raise TimeoutError("model timed out")
    with pytest.raises(TimeoutError):
        BudgetMiddleware().wrap_model_call(request(context), timeout)
    with sqlite3.connect(shared.path) as connection:
        row = connection.execute("SELECT status,reserved_usd FROM agent_model_calls").fetchone()
    assert row[0] == "unknown" and row[1] > 0


def test_weekday_peak_and_offpeak_hours():
    zone = ZoneInfo("Asia/Shanghai")
    assert _peak_window(datetime(2026, 10, 28, 15, 0, tzinfo=zone))
    assert not _peak_window(datetime(2026, 10, 28, 18, 0, tzinfo=zone))
    assert not _peak_window(datetime(2026, 10, 31, 10, 0, tzinfo=zone))


def test_peak_authorization_expires_and_rejects_invalid_expiry(monkeypatch):
    from taxharness.provider import _peak_authorized_now

    zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 28, 17, 0, tzinfo=zone)
    monkeypatch.setenv("TAXHARNESS_PEAK_ALLOWED_UNTIL", "2026-10-28T18:00:00+08:00")
    assert _peak_authorized_now(now)
    assert not _peak_authorized_now(datetime(2026, 10, 28, 18, 0, tzinfo=zone))
    monkeypatch.setenv("TAXHARNESS_PEAK_ALLOWED_UNTIL", "2026-10-28T18:00:00")
    assert not _peak_authorized_now(now)
    monkeypatch.setenv("TAXHARNESS_PEAK_ALLOWED_UNTIL", "invalid")
    assert not _peak_authorized_now(now)


def test_fake_agent_turn_writes_conversation_locally_and_usage_to_shared_ledger(tmp_path):
    class FakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "business.sqlite3"
    initialize_database(database, root / "data/02_示例数据.xlsx", root / "data/03_业务规则.docx.txt")
    runtime = AgentRuntime(
        database_path=database,
        agent_store_path=tmp_path / "candidate.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        budget_ledger_path=tmp_path / "shared.sqlite3",
    )
    reply = AIMessage(content="已收到。", usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25})
    try:
        result = runtime.run_turn(session_id="isolated-session", user_id="U001", company_id="C001", message="你好", model=FakeModel(responses=[reply]), live_authorized=True, budget_usd=1.0)
        assert result["status"] == "ok"
        assert runtime.agent_store.list_turns("isolated-session", "U001")
        with sqlite3.connect(tmp_path / "shared.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM agent_model_calls").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM agent_turns").fetchone()[0] == 0
    finally:
        runtime.close()


def test_candidate_cannot_use_real_provider_without_shared_ledger(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "business.sqlite3"
    initialize_database(database, root / "data/02_示例数据.xlsx", root / "data/03_业务规则.docx.txt")
    monkeypatch.delenv("TAXHARNESS_BUDGET_STORE", raising=False)
    monkeypatch.setattr("taxharness.agent.build_live_model", lambda **_kwargs: pytest.fail("provider must stay closed"))
    runtime = AgentRuntime(database_path=database, agent_store_path=tmp_path / "candidate.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        result = runtime.run_turn(session_id="s", user_id="U001", company_id="C001", message="你好", live_authorized=True, budget_usd=1)
        assert result["status"] == "error" and "共享预算账本" in result["summary"]
    finally:
        runtime.close()


def test_peak_window_closes_real_provider_before_reading_credentials(monkeypatch):
    import taxharness.provider as provider

    monkeypatch.setenv("TAXHARNESS_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("TAXHARNESS_OFFPEAK_ONLY", "true")
    monkeypatch.setattr(provider, "_peak_window", lambda _now: True)
    monkeypatch.setattr(provider, "_read_api_key", lambda _config: pytest.fail("credential must not be read"))
    with pytest.raises(LiveModelDisabled, match="高峰时段"):
        provider.build_live_model(authorized=True)


def test_model_calls_recheck_peak_window_before_each_reservation(tmp_path, monkeypatch):
    import taxharness.agent as agent

    local = AgentStore(tmp_path / "candidate.sqlite3")
    shared = AgentStore(tmp_path / "shared.sqlite3")
    context = AgentContext(
        user_id="U001", session_id="candidate", database_path=str(tmp_path / "business.sqlite3"),
        agent_store_path=str(local.path), current_company_id="C001", budget_ledger_path=str(shared.path),
        budget_usd=1.0, max_output_tokens=100, turn_id="turn",
    )
    monkeypatch.setenv("TAXHARNESS_OFFPEAK_ONLY", "true")
    monkeypatch.setattr(agent, "_peak_window", lambda _now: True)
    with pytest.raises(LiveModelDisabled, match="高峰时段"):
        BudgetMiddleware().wrap_model_call(request(context), lambda _request: pytest.fail("request must stay closed"))
    with sqlite3.connect(shared.path) as connection:
        assert connection.execute("SELECT count(*) FROM agent_model_calls").fetchone()[0] == 0

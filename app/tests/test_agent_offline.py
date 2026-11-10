from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain.agents.middleware import ModelRequest
from langgraph.runtime import Runtime

from taxharness.agent import AgentRuntime, ProgramContextMiddleware
from taxharness.agent_tools import AgentContext
from taxharness.agent_store import AgentStore
from taxharness.errors import BudgetExceeded, LiveModelDisabled, SessionOwnershipError
from taxharness.provider import build_live_model, provider_config
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database


SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def runtime(tmp_path):
    database = tmp_path / "business.sqlite3"
    initialize_database(database, SOURCE_XLSX, RULES_DOC)
    value = AgentRuntime(database_path=database, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    yield value
    value.close()


def test_offline_agent_executes_real_query_tool_and_records_turn(runtime):
    model = ToolFake(responses=[
        AIMessage(content="", tool_calls=[{"name": "query_finance", "args": {"month": "2026-09", "company_id": "C001"}, "id": "call-q", "type": "tool_call"}]),
        AIMessage(content="已完成甲企业 2026-09 查询。"),
    ])
    result = runtime.run_turn(session_id="agent-s1", user_id="U001", company_id="C001", message="查甲企业九月金额", model=model)
    assert result["status"] == "ok"
    assert result["tool_call_count"] == 1
    assert runtime.list_turns(session_id="agent-s1", user_id="U001")["turns"]


def test_switch_batch_does_not_grant_access_to_another_company(runtime):
    model = ToolFake(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "switch_company", "args": {"company_id": "C002"}, "id": "call-switch", "type": "tool_call"},
            {"name": "query_finance", "args": {"month": "2026-09", "company_id": "C002"}, "id": "call-query", "type": "tool_call"},
        ]),
        AIMessage(content="没有访问乙企业的权限。"),
    ])
    result = runtime.run_turn(session_id="agent-switch", user_id="U002", company_id="C001", message="切到乙企业并查询", model=model)
    assert result["status"] == "ok"
    assert result["current_company_id"] == "C001"
    tool_results = [message["data"]["content"] for message in result["messages"] if message["type"] == "tool"]
    assert len(tool_results) == 2
    assert all("permission_denied" in str(content) for content in tool_results)


def test_agent_store_connections_close_after_use(tmp_path):
    from taxharness.agent_store import AgentStore

    path = tmp_path / "agent.sqlite3"
    store = AgentStore(path)
    store.ensure_session("close-check", "U001", "C001")
    assert store.session("close-check", "U001")["current_company_id"] == "C001"
    path.rename(tmp_path / "renamed.sqlite3")


def test_agent_save_uses_current_revision_and_supports_partial_changes(runtime):
    model = ToolFake(responses=[
        AIMessage(content="", tool_calls=[{"name": "save_invoice", "args": {"invoice_id": "F002", "month": "2026-09", "company_id": "C001", "changes": {"remarks": "刚才补充"}}, "id": "call-save", "type": "tool_call"}]),
        AIMessage(content="备注已保存。"),
    ])
    result = runtime.run_turn(session_id="agent-save", user_id="U001", company_id="C001", message="给F002追加备注刚才补充", model=model)
    assert result["status"] == "ok"
    with TaxDatabase(runtime.database_path) as database:
        row = database.connection.execute("SELECT purpose,remarks FROM invoices WHERE company_id='C001' AND invoice_id='F002'").fetchone()
        assert row["purpose"] is None and row["remarks"] == "刚才补充"


def test_agent_whitespace_no_change_is_success_without_revision_or_committed_operation(runtime):
    model = ToolFake(responses=[
        AIMessage(content="", tool_calls=[{"name": "save_invoice", "args": {"invoice_id": "F003", "month": "2026-09", "company_id": "C001", "changes": {"purpose": "   "}}, "id": "call-blank", "type": "tool_call"}]),
        AIMessage(content="当前用途已经为空，本次无需修改。"),
    ])
    result = runtime.run_turn(session_id="agent-blank", user_id="U001", company_id="C001", message="清空 F003 用途。", model=model)
    assert result["status"] == "ok"
    operation = runtime.agent_store.find_operation(session_id="agent-blank", turn_id=result["turn_id"], user_id="U001", company_id="C001", object_type="invoice", object_id="F003")
    assert operation["status"] == "no_change"
    with TaxDatabase(runtime.database_path) as database:
        row = database.connection.execute("SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()
        assert row["purpose"] is None and row["revision"] == 1


def test_session_cannot_be_read_by_another_user(runtime):
    model = ToolFake(responses=[AIMessage(content="ok")])
    runtime.run_turn(session_id="owned", user_id="U001", company_id="C001", message="开始", model=model)
    with pytest.raises(SessionOwnershipError):
        runtime.run_turn(session_id="owned", user_id="U002", company_id="C001", message="读取", model=model)


def test_initial_denied_company_does_not_leave_session(runtime):
    model = ToolFake(responses=[AIMessage(content="ok")])
    denied = runtime.run_turn(session_id="new-scope", user_id="U002", company_id="C002", message="开始", model=model)
    assert denied["status"] == "permission_denied"
    assert runtime.agent_store.session("new-scope", "U002") is None
    allowed = runtime.run_turn(session_id="new-scope", user_id="U002", company_id="C001", message="开始", model=model)
    assert allowed["status"] == "ok"


def test_live_provider_stays_disabled_without_authorization():
    with pytest.raises(LiveModelDisabled):
        build_live_model(authorized=False, config=provider_config())


def test_budget_ledger_is_cumulative_and_rejects_over_limit(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    store.reserve_model_call(session_id="s", user_id="U001", model="deepseek-flash", estimated_input_tokens=1_000_000, estimated_output_tokens=0, budget_usd=0.5)
    with pytest.raises(BudgetExceeded):
        store.reserve_model_call(session_id="s", user_id="U001", model="deepseek-flash", estimated_input_tokens=1_000_000, estimated_output_tokens=0, budget_usd=0.5)


def test_provider_uses_one_config_file_and_fixed_model(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nmodel="deepseek-flash"\nbase_url="https://api.deepseek.com"\ntimeout_seconds=77\nmax_output_tokens=2048\nbudget_usd=2\n', encoding="utf-8")
    monkeypatch.setenv("TAXHARNESS_CONFIG", str(config))
    from taxharness.provider import provider_config
    value = provider_config()
    assert value.model == "deepseek-flash" and value.timeout_seconds == 77 and value.max_output_tokens == 2048 and value.budget_usd == 2


def test_program_context_keeps_recent_complete_tool_turns(tmp_path):
    initialize_database(tmp_path / "business.sqlite3", SOURCE_XLSX, RULES_DOC)
    context = AgentContext(user_id="U001", session_id="window", database_path=str(tmp_path / "business.sqlite3"), agent_store_path=str(tmp_path / "agent.sqlite3"), current_company_id="C001")
    messages = []
    for index in range(11):
        messages.append(HumanMessage(content=f"turn-{index}"))
        messages.append(AIMessage(content="", tool_calls=[{"name": "query_finance", "args": {}, "id": f"call-{index}", "type": "tool_call"}]))
        messages.append(ToolMessage(content="{}", tool_call_id=f"call-{index}", name="query_finance"))
    request = ModelRequest(model=ToolFake(responses=[AIMessage(content="ok")]), messages=messages, system_message=SystemMessage(content="base"), tools=[], runtime=Runtime(context=context))
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    kept = seen["request"].messages
    assert kept[0].content == "turn-3" and kept[-1].content == "{}"
    for message in kept:
        if isinstance(message, AIMessage) and message.tool_calls:
            assert any(isinstance(item, ToolMessage) and item.tool_call_id == message.tool_calls[0]["id"] for item in kept)


@pytest.mark.parametrize("later_changed", [False, True])
def test_recovery_returns_committed_business_receipt_after_error(runtime, later_changed):
    store = runtime.agent_store
    session_id = "recover-session"
    store.ensure_session(session_id, "U001", "C001")
    old_turn = store.start_turn(session_id, "U001", "保存 F002", {})
    store.finish_turn(old_turn, status="cancelled", output_text="中断", result={})
    operation = store.get_or_create_operation(
        session_id=session_id, turn_id=old_turn, user_id="U001", company_id="C001",
        object_type="invoice", object_id="F002", payload={"changes": {"purpose": "恢复用途"}}, expected_revision=1,
    )
    with TaxDatabase(runtime.database_path) as database:
        service = TaxService(database)
        saved = service.update_invoice(user_id="U001", company_id="C001", invoice_id="F002", changes={"purpose": "恢复用途"}, expected_revision=1, idempotency_key=operation["operation_id"])
        assert saved["status"] == "committed"
        if later_changed:
            later = service.update_invoice(user_id="U001", company_id="C001", invoice_id="F002", changes={"purpose": "后来更正的用途"}, expected_revision=2, idempotency_key="U001:C001:invoice:F002:later")
            assert later["status"] == "committed"

    class FakeGraph:
        def __init__(self):
            self.updated = None

        def get_state(self, config):
            return SimpleNamespace(next=("tools",), values={"messages": [AIMessage(content="", tool_calls=[{"name": "save_invoice", "args": {"invoice_id": "F002", "company_id": "C001", "month": "2026-09", "changes": {"purpose": "恢复用途"}}, "id": "old-call", "type": "tool_call"}])]})

        def update_state(self, config, values, as_node=None):
            self.updated = values["messages"]

    graph = FakeGraph()
    context = AgentContext(user_id="U001", session_id=session_id, database_path=runtime.database_path, agent_store_path=str(store.path), current_company_id="C001")
    assert runtime._recover_unfinished_tools(graph, {"configurable": {"thread_id": session_id}}, context, old_turn) is True
    receipt = json.loads(graph.updated[0].content)
    assert receipt["status"] == "duplicate"
    assert receipt["current_matches_receipt"] is not later_changed
    assert receipt["current"]["purpose"] == ("后来更正的用途" if later_changed else "恢复用途")
    assert context.recovered_operations[0]["result"]["after"]["purpose"] == "恢复用途"
    request = ModelRequest(model=ToolFake(responses=[AIMessage(content="ok")]), messages=[HumanMessage(content="核对现在的值")],
                           system_message=SystemMessage(content="base"), tools=[], runtime=Runtime(context=context))
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    program_context = seen["request"].system_message.content
    assert "recovered_operations" in program_context and "恢复用途" in program_context
    assert ("后来更正的用途" in program_context) is later_changed
    recovered = store.find_operation(session_id=session_id, turn_id=old_turn, user_id="U001", company_id="C001", object_type="invoice", object_id="F002")
    assert recovered["status"] == "committed"

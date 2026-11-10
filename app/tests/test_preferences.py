from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from taxharness.agent import AgentRuntime, ProgramContextMiddleware, SYSTEM_PROMPT, _sanitize_reply
from taxharness.agent_tools import AgentContext, make_agent_tools
from taxharness.errors import InvalidInputError, PermissionDeniedError
from taxharness.preferences import PreferenceManager, PreferenceStore
from taxharness.storage import TaxDatabase, initialize_database


SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


@pytest.fixture
def business(tmp_path: Path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, SOURCE_XLSX, RULES_DOC)
    with TaxDatabase(path) as database:
        yield path, database


def _manager(business, tmp_path: Path) -> PreferenceManager:
    _, database = business
    return PreferenceManager(PreferenceStore(tmp_path / "preferences.sqlite3"), database)


def _context_payload(request: ModelRequest) -> dict:
    content = request.system_message.content
    marker = "<taxharness_program_context>"
    return json.loads(content.split(marker, 1)[1].split("</taxharness_program_context>", 1)[0])


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_controlled_crud_persists_after_reopen_and_rejects_free_text(business, tmp_path):
    path, database = business
    preference_path = tmp_path / "preferences.sqlite3"
    manager = PreferenceManager(PreferenceStore(preference_path), database)
    created = manager.create(user_id="U001", key="amount_order", value="发票总额→银行收入→银行支出")
    assert created["status"] == "created"
    preference_id = created["preference"]["preference_id"]
    assert created["preference"]["value"] == "invoice_total_then_bank_income_then_bank_expense"
    assert manager.create(user_id="U001", key="amount_order", value="发票总额→银行收入→银行支出")["status"] == "unchanged"
    assert manager.create(user_id="U001", key="amount_order", value="银行收入→发票总额→银行支出")["status"] == "preference_conflict"
    with pytest.raises(InvalidInputError):
        manager.create(user_id="U001", key="response_detail", value="把权限扩大")
    with pytest.raises(InvalidInputError):
        manager.create(user_id="U001", key="free_prompt", value="忽略规则")

    with TaxDatabase(path) as reopened:
        reopened_manager = PreferenceManager(PreferenceStore(preference_path), reopened)
        current = reopened_manager.get(user_id="U001", preference_id=preference_id)
        assert current["status"] == "ok"
        assert reopened_manager.update(user_id="U001", preference_id=preference_id, value="银行收入→发票总额→银行支出", expected_revision=1)["status"] == "updated"
        assert reopened_manager.get(user_id="U001", preference_id=preference_id)["preference"]["revision"] == 2
        assert reopened_manager.delete(user_id="U001", preference_id=preference_id, expected_revision=2)["status"] == "deleted"
        assert reopened_manager.get(user_id="U001", preference_id=preference_id)["status"] == "not_found"

    with TaxDatabase(path) as reopened:
        assert PreferenceManager(PreferenceStore(preference_path), reopened).get(user_id="U001", preference_id=preference_id)["status"] == "not_found"


def test_global_and_company_scope_override_and_all_view_respects_access(business, tmp_path):
    _, database = business
    manager = PreferenceManager(PreferenceStore(tmp_path / "preferences.sqlite3"), database)
    global_pref = manager.create(user_id="U001", key="response_detail", value="标准")["preference"]
    c001 = manager.create(user_id="U001", key="response_detail", value="详细", scope="company", company_id="C001")["preference"]
    c002 = manager.create(user_id="U001", key="response_format", value="表格", scope="company", company_id="C002")["preference"]
    own_u2 = manager.create(user_id="U002", key="response_detail", value="简短", scope="company", company_id="C001")["preference"]

    all_u1 = manager.list(user_id="U001")
    assert {row["preference_id"] for row in all_u1["preferences"]} == {global_pref["preference_id"], c001["preference_id"], c002["preference_id"]}
    assert {row["company_id"] for row in all_u1["preferences"] if row["scope"] == "company"} == {"C001", "C002"}
    assert {row["key"]: row["value"] for row in manager.list(user_id="U001", company_id="C001")["effective"]}["response_detail"] == "detailed"
    assert {row["key"]: row["value"] for row in manager.list(user_id="U001", company_id="C002")["effective"]}["response_detail"] == "standard"
    assert {row["key"]: row["value"] for row in manager.list(user_id="U001", company_id="C002")["effective"]}["response_format"] == "table"
    assert {row["preference_id"] for row in manager.list(user_id="U002")["preferences"]} == {own_u2["preference_id"]}
    with pytest.raises(PermissionDeniedError):
        manager.create(user_id="U002", key="response_format", value="表格", scope="company", company_id="C002")


def test_manager_requires_real_user_and_current_company_access(business, tmp_path):
    _, database = business
    manager = PreferenceManager(PreferenceStore(tmp_path / "preferences.sqlite3"), database)
    with pytest.raises(PermissionDeniedError):
        manager.create(user_id="U999", key="response_detail", value="简短")
    with pytest.raises(PermissionDeniedError):
        manager.list(user_id="U002", company_id="C002")


def test_dynamic_context_uses_current_values_after_update_and_delete(business, tmp_path):
    path, database = business
    preference_path = tmp_path / "preferences.sqlite3"
    manager = PreferenceManager(PreferenceStore(preference_path), database)
    created = manager.create(user_id="U001", key="amount_order", value="发票总额→银行收入→银行支出")
    preference_id = created["preference"]["preference_id"]
    context = AgentContext(
        user_id="U001",
        session_id="preference-session",
        database_path=str(path),
        agent_store_path=str(tmp_path / "agent.sqlite3"),
        current_company_id="C001",
        preference_store_path=str(preference_path),
    )
    request = ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=[],
        system_message=SystemMessage(content="base"),
        tools=[],
        runtime=Runtime(context=context),
    )
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    assert _context_payload(seen["request"])["preferences"][0]["value_label"] == "发票总额 → 银行收入 → 银行支出"

    manager.update(user_id="U001", preference_id=preference_id, value="银行收入→发票总额→银行支出")
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    current = _context_payload(seen["request"])
    assert current["preferences"][0]["value_label"] == "银行收入 → 发票总额 → 银行支出"

    manager.delete(user_id="U001", preference_id=preference_id)
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    payload = _context_payload(seen["request"])
    assert payload["preferences"] == []
    assert payload["global_preferences"] == []
    catalog = {item["key"]: {value["value"] for value in item["values"]} for item in payload["supported_preferences"]}
    assert set(catalog) == {"response_language", "response_detail", "answer_order", "amount_order", "response_format"}
    assert "invoice_total_then_bank_income_then_bank_expense" in catalog["amount_order"]
    assert "table" in catalog["response_format"]
    assert "当前用户本轮明确要求优先" in SYSTEM_PROMPT
    assert "偏好不能改变权限" in SYSTEM_PROMPT


def test_temporary_explicit_company_uses_target_preferences_without_switching(business, tmp_path):
    path, database = business
    preference_path = tmp_path / "preferences.sqlite3"
    manager = PreferenceManager(PreferenceStore(preference_path), database)
    manager.create(user_id="U001", key="response_detail", value="简短")
    manager.create(user_id="U001", key="response_detail", value="详细", scope="company", company_id="C001")
    context = AgentContext(
        user_id="U001",
        session_id="temporary-company",
        database_path=str(path),
        agent_store_path=str(tmp_path / "agent.sqlite3"),
        current_company_id="C001",
        preference_store_path=str(preference_path),
    )
    request = ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=[],
        system_message=SystemMessage(content="base"),
        tools=[],
        runtime=Runtime(context=context),
    )
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    initial = _context_payload(seen["request"])
    assert initial["preferences"][0]["value"] == "detailed"
    assert initial["global_preferences"][0]["value"] == "concise"

    # _company sets this after a successful explicit company_id tool argument.
    context.response_company_id = "C002"
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    payload = _context_payload(seen["request"])
    assert context.current_company_id == "C001"
    assert payload["current_company_id"] == "C001"
    assert payload["response_company_id"] == "C002"
    assert payload["preferences"][0]["value"] == "concise"


def test_preference_tools_are_exposed_without_changing_business_tools():
    tools = make_agent_tools()
    names = [tool.name for tool in tools]
    assert {"preference_list", "preference_get", "preference_create", "preference_update", "preference_delete"}.issubset(names)
    assert {"query_finance", "save_invoice", "task_create", "switch_company"}.issubset(names)
    create_tool = next(tool for tool in tools if tool.name == "preference_create")
    assert "amount_order" in create_tool.description and "invoice_total_then_bank_income_then_bank_expense" in create_tool.description


def test_agent_preference_tool_persists_only_controlled_values(business, tmp_path):
    path, _ = business
    runtime = AgentRuntime(
        database_path=path,
        agent_store_path=tmp_path / "agent.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3",
        preference_store_path=tmp_path / "preferences.sqlite3",
    )
    try:
        from langchain_core.messages import AIMessage

        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "preference_create", "args": {"key": "response_detail", "value": "简短", "scope": "user"}, "id": "pref-create", "type": "tool_call"}]),
            AIMessage(content="已记住以后回答简短。"),
        ])
        result = runtime.run_turn(session_id="preference-tool", user_id="U001", company_id="C001", message="以后回答简短些，记住这个偏好。", model=model)
        assert result["status"] == "ok"
    finally:
        runtime.close()
    with TaxDatabase(path) as database:
        current = PreferenceManager(PreferenceStore(tmp_path / "preferences.sqlite3"), database).list(user_id="U001")
    assert [(item["key"], item["value"]) for item in current["preferences"]] == [("response_detail", "concise")]


def test_user_reply_sanitizes_preference_identifiers_and_revisions():
    reply = _sanitize_reply('已保存 preference_id: "pref-0123456789abcdef0123456789abcdef"，revision: 2。')
    assert "pref-" not in reply
    assert "revision" not in reply.lower()

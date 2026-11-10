import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.agent import AgentRuntime, ProgramContextMiddleware
from taxharness.agent_tools import AgentContext, make_agent_tools
from taxharness.errors import InvalidInputError
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]


def _context(tmp_path):
    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    return AgentContext(user_id="U001", session_id="test-session", turn_id="turn-1", current_company_id="C001",
                        database_path=str(business), agent_store_path=str(tmp_path / "agent.sqlite3"),
                        preference_store_path=str(tmp_path / "preferences.sqlite3"))


def test_save_uses_the_version_seen_earlier_in_the_turn(tmp_path):
    context = _context(tmp_path)
    runtime = SimpleNamespace(context=context)
    tools = {tool.name: tool.func for tool in make_agent_tools()}
    seen = tools["read_invoice"](invoice_id="F003", company_id="C001", runtime=runtime)
    assert seen["data"]["purpose"] is None
    with TaxDatabase(context.database_path) as db:
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("别人已核实的用途", "C001", "F003"))
    result = tools["save_invoice"](invoice_id="F003", company_id="C001", changes={"purpose": "寄送合同"}, runtime=runtime)
    assert result["status"] == "revision_conflict"
    with TaxDatabase(context.database_path) as db:
        assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] == "别人已核实的用途"


def test_conditional_save_checks_user_requested_empty_field(tmp_path):
    context = _context(tmp_path)
    context.current_user_text = "如果 F003 的用途仍为空才保存，否则保留原来的内容。"
    runtime = SimpleNamespace(context=context)
    save = next(tool.func for tool in make_agent_tools() if tool.name == "save_invoice")
    missing = save(invoice_id="F003", company_id="C001", changes={"purpose": "寄送合同"}, runtime=runtime)
    assert missing["status"] == "precondition_required"
    with TaxDatabase(context.database_path) as db:
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("已核实", "C001", "F003"))
    wrong = save(invoice_id="F003", company_id="C001", changes={"purpose": "寄送合同"}, expected_fields={"purpose": "已核实"}, runtime=runtime)
    assert wrong["status"] == "precondition_required"
    blocked = save(invoice_id="F003", company_id="C001", changes={"purpose": "寄送合同"}, expected_fields={"purpose": ""}, runtime=runtime)
    assert blocked["status"] == "precondition_not_met"
    with TaxDatabase(context.database_path) as db:
        assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] == "已核实"


def test_frontier_diff_does_not_follow_the_user_to_another_company(tmp_path):
    context = _context(tmp_path)
    with TaxDatabase(context.database_path) as db:
        manager = TaskManager(db)
        manager.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])
        ledger = TaskLedger(db)
        ledger.frontier(user_id="U001", company_id="C001", session_id="test-session", turn_id="turn-1")
        ledger.apply(user_id="U001", company_id="C001", session_id="test-session", turn_id="turn-1",
                     operations=[{"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送文件"}])

    class Request:
        runtime = SimpleNamespace(context=context)
        messages = [HumanMessage(content="继续办理")]
        system_message = SystemMessage(content="助手")
        tools = []

        def override(self, **kwargs):
            return SimpleNamespace(**kwargs)

    middleware = ProgramContextMiddleware()
    first = middleware.wrap_model_call(Request(), lambda request: request)
    first_context = json.loads(first.system_message.content.split("<taxharness_program_context>")[1].split("</taxharness_program_context>")[0])
    assert "F003" in json.dumps(first_context["task_frontier"]["diff"], ensure_ascii=False)
    context.current_company_id = "C002"
    second = middleware.wrap_model_call(Request(), lambda request: request)
    second_context = json.loads(second.system_message.content.split("<taxharness_program_context>")[1].split("</taxharness_program_context>")[0])
    assert "F003" not in json.dumps(second_context["task_frontier"]["diff"], ensure_ascii=False)
    assert second_context["task_frontier"]["frontier"]["company_id"] == "C002"


def test_context_limit_after_a_save_reports_the_actual_completed_write(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.delenv("TAXHARNESS_CONTEXT_BUDGET_TOKENS", raising=False)

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            monkeypatch.setenv("TAXHARNESS_CONTEXT_BUDGET_TOKENS", "1")
            return result

    model = Model(responses=[AIMessage(content="", tool_calls=[{"name": "save_invoice", "args": {
        "invoice_id": "F003", "changes": {"purpose": "寄送验收文件"}}, "id": "save", "type": "tool_call"}])])
    runtime = AgentRuntime(database_path=context.database_path, agent_store_path=context.agent_store_path,
                           checkpoint_path=tmp_path / "checkpoint.sqlite3", preference_store_path=context.preference_store_path)
    try:
        result = runtime.run_turn(session_id="budget-after-write", user_id="U001", company_id="C001",
                                  message="保存 F003 用途为寄送验收文件。", model=model)
        assert result["error_type"] == "ContextBudgetExceeded"
        assert "F003" in result["summary"] and "寄送验收文件" in result["summary"] and "已办理" in result["summary"]
        with TaxDatabase(context.database_path) as db:
            assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] == "寄送验收文件"
    finally:
        runtime.close()


def test_question_candidates_cannot_cover_an_unrelated_invoice_or_field(tmp_path):
    import pytest

    context = _context(tmp_path)
    with TaxDatabase(context.database_path) as db:
        TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])
    plan = next(tool.func for tool in make_agent_tools() if tool.name == "plan_followup_questions")
    candidate = {"id": "q", "text": "请说明用途", "targets": ["F999"], "required_slots": {"F999": ["purpose"]}, "burden": 1, "priority": 1}
    with pytest.raises(InvalidInputError, match="当前任务"):
        plan(candidates=[candidate], runtime=SimpleNamespace(context=context))
    candidate.update({"targets": ["F003"], "required_slots": {"F003": ["tax_number"]}})
    with pytest.raises(InvalidInputError, match="当前任务"):
        plan(candidates=[candidate], runtime=SimpleNamespace(context=context))

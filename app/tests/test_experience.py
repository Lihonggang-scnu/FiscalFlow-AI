from pathlib import Path

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from taxharness.agent import ProgramContextMiddleware
from taxharness.agent_tools import AgentContext
from taxharness.experience import ExperienceBook, matching_strategies
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]


def test_task_strategy_lifecycle_and_context_injection(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as db:
        TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])
        ledger = TaskLedger(db)
        book = ExperienceBook(ledger)
        proposal = book.propose(user_id="U001", company_id="C001", session_id="s", turn_id="t1",
            situation="多张票仍缺用途", approach="把需要用户补充的用途合并询问", tags=["multiple_invoices", "needs_input"])
        strategy_id = proposal["strategy"]["strategy_id"]
        frontier = ledger.frontier(user_id="U001", company_id="C001")["frontier"]
        assert matching_strategies(ledger.memory(user_id="U001", company_id="C001"), frontier, {}) == []
        assert book.set_active(user_id="U001", company_id="C001", session_id="s", turn_id="t2", strategy_id=strategy_id, active=True)["status"] == "ok"
        assert matching_strategies(ledger.memory(user_id="U001", company_id="C001"), frontier, {})[0]["approach"] == "把需要用户补充的用途合并询问"
        assert ledger.memory(user_id="U002", company_id="C001")["strategies"] == {}

    context = AgentContext(user_id="U001", session_id="s", database_path=str(path), agent_store_path=str(tmp_path / "agent.sqlite3"), current_company_id="C001")
    request = ModelRequest(model=FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), messages=[HumanMessage(content="继续办理")],
                           system_message=SystemMessage(content="base"), tools=[], runtime=Runtime(context=context))
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    assert "把需要用户补充的用途合并询问" in seen["request"].system_message.content

    with TaxDatabase(path) as db:
        book = ExperienceBook(TaskLedger(db))
        assert book.set_active(user_id="U001", company_id="C001", session_id="s", turn_id="t3", strategy_id=strategy_id, active=False)["status"] == "ok"
        memory = TaskLedger(db).memory(user_id="U001", company_id="C001")
        assert memory["strategies"][strategy_id]["status"] == "retired"
        assert matching_strategies(memory, frontier, {}) == []

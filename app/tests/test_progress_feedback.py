from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from taxharness.agent import AgentRuntime
from taxharness.progress_feedback import progress_feedback
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger


ROOT = Path(__file__).resolve().parents[1]


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _attempt(call_id):
    return [
        AIMessage(content="", tool_calls=[{"name": "read_invoice", "args": {"invoice_id": "missing"}, "id": call_id, "type": "tool_call"}]),
        ToolMessage(content='{"status":"not_found","summary":"未找到发票"}', name="read_invoice", tool_call_id=call_id),
    ]


def test_repeated_tool_outcome_is_feedback_not_a_new_guard():
    messages = [HumanMessage(content="查这张票"), *_attempt("a"), *_attempt("b")]
    result = progress_feedback(messages)
    assert result["repeated"] == [{"tool": "read_invoice", "count": 2, "status": "not_found"}]
    assert result["suggestion"] and len(result["issues"]) == 2
    assert progress_feedback(messages + [HumanMessage(content="再查一次"), *_attempt("c")])["repeated"] == []


def test_agent_saves_repeated_observation_as_a_case(tmp_path):
    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "read_invoice", "args": {"invoice_id": "missing"}, "id": "a", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "read_invoice", "args": {"invoice_id": "missing"}, "id": "b", "type": "tool_call"}]),
            AIMessage(content="没有找到这张票，请确认编号。"),
        ])
        result = runtime.run_turn(session_id="feedback", user_id="U001", company_id="C001", message="查这张票", model=model)
        assert result["status"] == "ok"
        with TaxDatabase(business) as db:
            cases = [event for event in TaskLedger(db).events(user_id="U001", company_id="C001") if event["kind"] == "case_observed"]
            assert len(cases) == 1 and cases[0]["payload"]["feedback"]["repeated"][0]["count"] == 2
    finally:
        runtime.close()

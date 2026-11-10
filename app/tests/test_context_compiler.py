from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from taxharness.agent import AgentRuntime
from taxharness.context_compiler import compile_context
from taxharness.storage import initialize_database


ROOT = Path(__file__).resolve().parents[1]


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _turn(index):
    call = f"call-{index}"
    return [
        HumanMessage(content=f"用户第 {index} 轮", id=f"turn-{index}"),
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": call, "type": "tool_call"}]),
        ToolMessage(content=f"结果 {index}", tool_call_id=call),
        AIMessage(content=f"答复 {index}"),
    ]


def test_context_keeps_old_task_source_and_recent_complete_tool_turns():
    messages = [message for index in range(20) for message in _turn(index)]
    selected, manifest = compile_context(messages, token_budget=10000, recent_turns=3, pinned={"turn-1"})
    users = [message.id for message in selected if isinstance(message, HumanMessage)]
    assert users == ["turn-1", "turn-15", "turn-16", "turn-17", "turn-18", "turn-19"]
    assert manifest["folded_turns"] == 14 and "turn-1" in manifest["pinned_turn_ids"]
    for message in selected:
        if isinstance(message, AIMessage) and message.tool_calls:
            assert any(isinstance(result, ToolMessage) and result.tool_call_id == message.tool_calls[0]["id"] for result in selected)


def test_latest_user_turn_is_retained_when_budget_is_small():
    messages = [message for index in range(5) for message in _turn(index)]
    selected, manifest = compile_context(messages, token_budget=1, recent_turns=8, pinned={"turn-0"})
    assert [message.id for message in selected if isinstance(message, HumanMessage)] == ["turn-4"]
    assert manifest["latest_turn_over_budget"] is True


def test_recent_context_changes_its_prefix_in_batches():
    selections = []
    for count in (8, 9, 10, 11):
        messages = [message for index in range(count) for message in _turn(index)]
        selected, manifest = compile_context(messages, token_budget=10000, recent_turns=8, fold_every=3)
        selections.append(([message.id for message in selected if isinstance(message, HumanMessage)], manifest["fold_boundary"]))
    assert selections[0][1] == selections[1][1] == selections[2][1] == 0
    assert selections[2][0][:8] == selections[0][0]
    assert selections[3][1] == 3 and selections[3][0][0] == "turn-3"


def test_old_user_statement_can_be_recalled_from_another_session(tmp_path):
    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        first = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "update_task_memory", "args": {"operations": [{"action": "note", "key": "waiting", "value": "周五问经办人"}]}, "id": "note", "type": "tool_call"}]),
            AIMessage(content="已记下周五再问经办人。"),
        ])
        initial = runtime.run_turn(session_id="first", user_id="U001", company_id="C001", message="周五再问经办人，先记下来。", model=first)
        assert initial["status"] == "ok"
        second = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "recall_task_turn", "args": {"turn_id": initial["turn_id"]}, "id": "recall", "type": "tool_call"}]),
            AIMessage(content="你当时说周五再问经办人。"),
        ])
        continued = runtime.run_turn(session_id="second", user_id="U001", company_id="C001", message="我之前怎么说的？", model=second)
        results = [item["data"]["content"] for item in continued["messages"] if item["type"] == "tool" and item["data"].get("name") == "recall_task_turn"]
        assert continued["status"] == "ok" and results and "周五再问经办人" in results[-1]
        other = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "recall_task_turn", "args": {"turn_id": initial["turn_id"]}, "id": "other", "type": "tool_call"}]),
            AIMessage(content="没有找到该对话。"),
        ])
        denied = runtime.run_turn(session_id="other", user_id="U002", company_id="C001", message="查看那条历史", model=other)
        results = [item["data"]["content"] for item in denied["messages"] if item["type"] == "tool" and item["data"].get("name") == "recall_task_turn"]
        assert results and "not_found" in results[-1] and "周五" not in results[-1]
    finally:
        runtime.close()


def test_old_draft_does_not_authorize_saving_in_a_new_turn(tmp_path):
    from taxharness.storage import TaxDatabase
    from taxharness.task_ledger import TaskLedger
    from taxharness.task_manager import TaskManager

    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(business) as db:
        TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])
        ledger = TaskLedger(db)
        ledger.apply(user_id="U001", company_id="C001", session_id="first", turn_id="t1", operations=[{"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送文件", "save_requested": True}])
        same_turn = ledger.frontier(user_id="U001", company_id="C001", session_id="first", turn_id="t1")
        assert same_turn["frontier"]["ready_to_save"] == ["F003"]
        later = ledger.frontier(user_id="U001", company_id="C001", session_id="second", turn_id="t2")
        assert later["frontier"]["ready_to_save"] == []
        assert later["frontier"]["awaiting_confirmation"] == ["F003"]

import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.agent import AgentRuntime
from taxharness.errors import PermissionDeniedError
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger, replay_events
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    return path


def test_task_memory_reconciles_correction_and_external_save(business):
    with TaxDatabase(business) as db:
        tasks = TaskManager(db)
        created = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])
        task_id = created["task"]["task_id"]
        ledger = TaskLedger(db)
        initial = ledger.frontier(user_id="U001", company_id="C001", session_id="s1", turn_id="t1")
        assert initial["frontier"]["needs_input"] == ["F003", "F009"]
        assert initial["frontier"]["dependencies"]["F003"]["target_basis"] == "user_selection"
        assert initial["frontier"]["dependencies"]["F003"]["invoice_revision"] == 1
        ledger.apply(user_id="U001", company_id="C001", session_id="s1", turn_id="t2", operations=[
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送验收材料", "save_requested": False},
            {"action": "draft", "invoice_id": "F009", "field": "remarks", "value": "待确认", "save_requested": False},
            {"action": "defer", "invoice_id": "F009", "reason": "等待经办人回复"},
        ])
        paused = ledger.frontier(user_id="U001", company_id="C001", session_id="s1", turn_id="t2")
        assert paused["frontier"]["waiting"] == ["F009"]
        assert paused["frontier"]["awaiting_confirmation"] == ["F003"]
        assert paused["frontier"]["drafts"]["F009"]["remarks"]["value"] == "待确认"

    # A separate business operation completes one invoice while the user is away.
    with TaxDatabase(business) as db:
        result = TaskManager(db).save_invoice(user_id="U001", company_id="C001", invoice_id="F003", month="2026-09", changes={"purpose": "寄送验收材料"}, expected_revision=1, idempotency_key="U001:C001:invoice:F003:colleague")
        assert result["status"] == "committed"

    with TaxDatabase(business) as db:
        ledger = TaskLedger(db)
        continued = ledger.frontier(user_id="U001", company_id="C001", session_id="s2", turn_id="t3")
        assert continued["frontier"]["completed"] == ["F003"]
        assert continued["frontier"]["waiting"] == ["F009"]
        assert "F003" not in continued["frontier"]["drafts"]
        changed = TaskManager(db).correct_task(task_id=task_id, user_id="U001", company_id="C001", changes={"invoice_ids": ["F003", "F006"]})
        assert changed["status"] == "updated"
        next_work = ledger.frontier(user_id="U001", company_id="C001", session_id="s2", turn_id="t4")
        assert next_work["frontier"]["needs_input"] == ["F006"]
        assert next_work["diff"]["added_targets"] == ["F006"]
        assert next_work["diff"]["removed_targets"] == ["F009"]
        assert next_work["diff"]["affected_targets"] == ["F006", "F009"]
        assert next_work["diff"]["unchanged_targets"] == ["F003"]
        memory = ledger.memory(user_id="U001", company_id="C001")
        assert not memory["drafts"] and not memory["deferred"]
        assert replay_events(ledger.events(user_id="U001", company_id="C001")) == memory
        assert db.connection.execute("SELECT remarks FROM invoices WHERE company_id='C001' AND invoice_id='F009'").fetchone()[0] is None


def test_task_memories_do_not_cross_user_or_company(business):
    with TaxDatabase(business) as db:
        ledger = TaskLedger(db)
        ledger.apply(user_id="U001", company_id="C001", session_id="s1", turn_id="t1", operations=[{"action": "defer", "invoice_id": "F009", "reason": "等待"}])
        assert ledger.memory(user_id="U002", company_id="C001")["deferred"] == {}
        assert ledger.memory(user_id="U001", company_id="C002")["deferred"] == {}
        with pytest.raises(PermissionDeniedError):
            ledger.memory(user_id="U002", company_id="C002")


def test_new_task_does_not_inherit_cancelled_task_decisions(business):
    with TaxDatabase(business) as db:
        tasks = TaskManager(db)
        ledger = TaskLedger(db)
        first = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F009"])["task"]
        ledger.apply(user_id="U001", company_id="C001", session_id="old", turn_id="old", operations=[
            {"action": "defer", "invoice_id": "F009", "reason": "旧任务等待客户"},
            {"action": "draft", "invoice_id": "F009", "field": "remarks", "value": "旧任务草稿"},
            {"action": "note", "key": "安排", "value": "旧任务安排"},
        ])
        ledger.frontier(user_id="U001", company_id="C001", session_id="old", turn_id="old")
        assert tasks.cancel_task(task_id=first["task_id"], user_id="U001", company_id="C001")["status"] == "cancelled"
        second = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F009"])["task"]
        current = ledger.frontier(user_id="U001", company_id="C001", session_id="new", turn_id="new")
        assert current["frontier"]["task_id"] == second["task_id"]
        assert current["frontier"]["needs_input"] == ["F009"]
        assert current["frontier"]["waiting"] == []
        assert current["frontier"]["drafts"] == {}
        assert current["frontier"]["notes"] == {}
        assert current["diff"]["initial"] is True
        assert any(event.get("task_id") == first["task_id"] and event["kind"] == "deferred"
                   for event in ledger.events(user_id="U001", company_id="C001"))


def test_same_turn_draft_before_creation_is_attached_only_to_matching_task(business):
    with TaxDatabase(business) as db:
        tasks, ledger = TaskManager(db), TaskLedger(db)
        ledger.apply(user_id="U001", company_id="C001", session_id="s", turn_id="create-turn", operations=[
            {"action": "draft", "invoice_id": "F002", "field": "purpose", "value": "购买培训资料"},
            {"action": "draft", "invoice_id": "F006", "field": "purpose", "value": "其他票草稿"},
        ])
        task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F002", "F003"])["task"]
        ledger.bind_task_turn(user_id="U001", company_id="C001", session_id="s", turn_id="create-turn", task=task)
        current = ledger.frontier(user_id="U001", company_id="C001", session_id="s", turn_id="first")
        assert current["frontier"]["drafts"]["F002"]["purpose"]["value"] == "购买培训资料"
        assert "F006" not in current["frontier"]["drafts"]
        tasks.correct_task(task_id=task["task_id"], user_id="U001", company_id="C001", changes={"invoice_ids": ["F003", "F006"]})
        ledger.frontier(user_id="U001", company_id="C001", session_id="s", turn_id="remove")
        tasks.correct_task(task_id=task["task_id"], user_id="U001", company_id="C001", changes={"invoice_ids": ["F002", "F003"]})
        restored = ledger.frontier(user_id="U001", company_id="C001", session_id="s", turn_id="readd")
        assert "F002" not in restored["frontier"]["drafts"]


def test_late_save_receipt_does_not_erase_newer_draft(business):
    with TaxDatabase(business) as db:
        tasks = TaskManager(db)
        ledger = TaskLedger(db)
        task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])["task"]
        ledger.apply(user_id="U001", company_id="C001", session_id="s", turn_id="old", operations=[
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送旧资料"},
        ])
        saved = tasks.save_invoice(user_id="U001", company_id="C001", invoice_id="F003", month="2026-09",
            changes={"purpose": "寄送旧资料"}, expected_revision=1, idempotency_key="U001:C001:invoice:F003:late")
        assert saved["status"] == "committed" and saved["task"]["status"] == "completed"
        ledger.apply(user_id="U001", company_id="C001", session_id="s", turn_id="new", operations=[
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送新版验收文件"},
        ])
        operation_id = db.connection.execute("SELECT operation_id FROM operations WHERE idempotency_key=?", ("U001:C001:invoice:F003:late",)).fetchone()[0]
        ledger.invoice_saved(user_id="U001", company_id="C001", session_id="s", turn_id="old", invoice_id="F003",
                             fields=["purpose"], task_id=task["task_id"], source_operation_id=operation_id)
        memory = ledger.memory(user_id="U001", company_id="C001")
        assert memory["drafts"]["F003:purpose"]["value"] == "寄送新版验收文件"
        assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] == "寄送旧资料"
        frontier = ledger.frontier(user_id="U001", company_id="C001", session_id="s", turn_id="review")["frontier"]
        assert frontier["awaiting_confirmation"] == ["F003"]
        assert "F003" not in frontier["completed"]
        assert not any(action["action"] == "finish" for action in frontier["next_actions"])
        ledger.apply(user_id="U001", company_id="C001", session_id="s", turn_id="wait", operations=[
            {"action": "defer", "invoice_id": "F003", "reason": "稍后确认更正"},
        ])
        deferred = ledger.frontier(user_id="U001", company_id="C001", session_id="s", turn_id="wait")["frontier"]
        assert deferred["waiting"] == ["F003"] and deferred["awaiting_confirmation"] == []
        assert deferred["drafts"]["F003"]["purpose"]["value"] == "寄送新版验收文件"


def test_task_and_invoice_changes_replay_from_events(business):
    with TaxDatabase(business) as db:
        tasks = TaskManager(db)
        task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])["task"]
        assert tasks.correct_task(task_id=task["task_id"], user_id="U001", company_id="C001", changes={"invoice_ids": ["F003", "F006"]})["status"] == "updated"
        assert tasks.save_invoice(user_id="U001", company_id="C001", invoice_id="F003", month="2026-09",
            changes={"purpose": "寄送文件"}, expected_revision=1, idempotency_key="U001:C001:invoice:F003:save")["status"] == "committed"
        ledger = TaskLedger(db)
        replayed = replay_events(ledger.events(user_id="U001", company_id="C001"))
        current = tasks.get_task(task_id=task["task_id"], user_id="U001", company_id="C001")["task"]
        assert replayed["tasks"][task["task_id"]] == current
        assert replayed["invoice_events"]["F003"]["after_revision"] == 2
        assert "F003" not in TaskLedger(db).memory(user_id="U001", company_id="C002")["invoice_events"]


def test_task_and_event_are_written_together(business, monkeypatch):
    import taxharness.task_manager as task_manager

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event unavailable")

    with TaxDatabase(business) as db:
        monkeypatch.setattr(task_manager, "record_task_event", fail_event)
        with pytest.raises(RuntimeError, match="event unavailable"):
            TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_memory_can_be_read_by_recorded_and_effective_time(business):
    with TaxDatabase(business) as db:
        ledger = TaskLedger(db)
        first_id = ledger.append(user_id="U001", company_id="C001", session_id="s", turn_id="t1", kind="note_set",
            payload={"key": "安排", "value": "先办理甲票"}, occurred_at="2026-08-01T00:00:00+08:00")
        first_time = "2026-10-01T00:00:00.000001+00:00"
        db.connection.execute("UPDATE task_ledger_events SET recorded_at=? WHERE event_id=?", (first_time, first_id))
        second_id = ledger.append(user_id="U001", company_id="C001", session_id="s", turn_id="t2", kind="note_set",
            payload={"key": "安排", "value": "改为先办理乙票"}, occurred_at="2026-09-01T00:00:00+08:00")
        second_time = "2026-10-01T00:00:00.000002+00:00"
        db.connection.execute("UPDATE task_ledger_events SET recorded_at=? WHERE event_id=?", (second_time, second_id))
        old_knowledge = ledger.memory(user_id="U001", company_id="C001", as_of_recorded=first_time, valid_at="2026-09-15")
        earlier_period = ledger.memory(user_id="U001", company_id="C001", as_of_recorded=second_time, valid_at="2026-08-15")
        corrected = ledger.memory(user_id="U001", company_id="C001", as_of_recorded=second_time, valid_at="2026-09-15")
        assert old_knowledge["notes"]["安排"]["value"] == "先办理甲票"
        assert earlier_period["notes"]["安排"]["value"] == "先办理甲票"
        assert corrected["notes"]["安排"]["value"] == "改为先办理乙票"
        ledger.append(user_id="U001", company_id="C001", session_id="s", turn_id="t3", kind="note_set",
            payload={"key": "未来安排", "value": "届时再处理"}, occurred_at="2099-01-01T00:00:00+08:00")
        assert "未来安排" not in ledger.memory(user_id="U001", company_id="C001")["notes"]
        assert ledger.memory(user_id="U001", company_id="C001", valid_at="2099-01-02")["notes"]["未来安排"]["value"] == "届时再处理"


def test_agent_keeps_deferred_decision_across_new_sessions(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        first = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "task_create", "args": {"month": "2026-09", "scope": "selected", "invoice_ids": ["F003", "F009"]}, "id": "create", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "update_task_memory", "args": {"operations": [{"action": "defer", "invoice_id": "F009", "reason": "等经办人"}]}, "id": "defer", "type": "tool_call"}]),
            AIMessage(content="任务已建立，F009 等经办人回复。"),
        ])
        result = runtime.run_turn(session_id="session-one", user_id="U001", company_id="C001", message="跟进 F003 和 F009，F009 先等经办人", model=first)
        assert result["status"] == "ok"
        second = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "inspect_task_frontier", "args": {}, "id": "frontier", "type": "tool_call"}]),
            AIMessage(content="F003 需要用途，F009 等经办人。"),
        ])
        result = runtime.run_turn(session_id="session-two", user_id="U001", company_id="C001", message="继续没办完的", model=second)
        assert result["status"] == "ok"
        tool_results = [message["data"]["content"] for message in result["messages"] if message["type"] == "tool" and message["data"].get("name") == "inspect_task_frontier"]
        assert tool_results and "F009" in tool_results[-1] and "waiting" in tool_results[-1]
    finally:
        runtime.close()


def test_switch_and_query_same_turn_use_new_company(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[
                {"name": "switch_company", "args": {"company_id": "C002"}, "id": "switch", "type": "tool_call"},
                {"name": "query_finance", "args": {"month": "2026-09"}, "id": "query", "type": "tool_call"},
            ]),
            AIMessage(content="", tool_calls=[
                {"name": "query_finance", "args": {"month": "2026-09", "company_id": "C002"}, "id": "query-again", "type": "tool_call"},
            ]),
            AIMessage(content="已切到乙企业并完成查询。"),
        ])
        result = runtime.run_turn(session_id="switch", user_id="U001", company_id="C001", message="切到乙企业并查九月", model=model)
        assert result["status"] == "ok" and result["current_company_id"] == "C002"
        tools = [message["data"]["content"] for message in result["messages"] if message["type"] == "tool" and message["data"].get("name") == "query_finance"]
        assert "company_scope_needed" in tools[0] and "12667.24" in tools[-1]
    finally:
        runtime.close()


def test_mixed_save_then_switch_keeps_invoice_in_requested_company(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        with TaxDatabase(business) as db:
            original_other_purpose = db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C002' AND invoice_id='F003'").fetchone()[0]
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[
                {"name": "save_invoice", "args": {"invoice_id": "F003", "month": "2026-09", "changes": {"purpose": "甲企业寄送资料"}}, "id": "save", "type": "tool_call"},
                {"name": "switch_company", "args": {"company_id": "C002"}, "id": "switch", "type": "tool_call"},
            ]),
            AIMessage(content="", tool_calls=[
                {"name": "save_invoice", "args": {"invoice_id": "F003", "month": "2026-09", "company_id": "C001", "changes": {"purpose": "甲企业寄送资料"}}, "id": "save-again", "type": "tool_call"},
            ]),
            AIMessage(content="", tool_calls=[
                {"name": "switch_company", "args": {"company_id": "C002"}, "id": "switch-again", "type": "tool_call"},
            ]),
            AIMessage(content="甲企业 F003 已保存，已切到乙企业。"),
        ])
        result = runtime.run_turn(session_id="mixed", user_id="U001", company_id="C001", message="先把甲企业 F003 用途保存为甲企业寄送资料，再切到乙企业。", model=model)
        assert result["status"] == "ok" and result["current_company_id"] == "C002"
        tool_results = [message["data"] for message in result["messages"] if message["type"] == "tool"]
        assert all("ordered_actions_needed" in item["content"] for item in tool_results[:2])
        assert tool_results[2]["name"] == "save_invoice" and tool_results[3]["name"] == "switch_company"
        with TaxDatabase(business) as db:
            rows = db.connection.execute("SELECT company_id,purpose FROM invoices WHERE invoice_id='F003' ORDER BY company_id").fetchall()
            assert [(row["company_id"], row["purpose"]) for row in rows] == [("C001", "甲企业寄送资料"), ("C002", original_other_purpose)]
    finally:
        runtime.close()


def test_undo_of_last_save_reopens_its_completed_task(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        first = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "task_create", "args": {"month": "2026-09", "scope": "selected", "invoice_ids": ["F003"]}, "id": "create", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "save_invoice", "args": {"invoice_id": "F003", "month": "2026-09", "changes": {"purpose": "寄送验收材料"}}, "id": "save", "type": "tool_call"}]),
            AIMessage(content="用途已保存，任务完成。"),
        ])
        assert runtime.run_turn(session_id="undo-task", user_id="U001", company_id="C001", message="跟进 F003，保存用途为寄送验收材料", model=first)["status"] == "ok"
        with TaxDatabase(business) as db:
            assert TaskManager(db).list_tasks(user_id="U001", company_id="C001")["tasks"] == []
        second = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "undo_saved_invoice", "args": {"invoice_id": "F003", "field": "purpose"}, "id": "undo", "type": "tool_call"}]),
            AIMessage(content="已撤销 F003 用途，任务继续。"),
        ])
        result = runtime.run_turn(session_id="undo-task", user_id="U001", company_id="C001", message="撤销 F003 刚才保存的用途", model=second)
        assert result["status"] == "ok"
        with TaxDatabase(business) as db:
            task = TaskManager(db).list_tasks(user_id="U001", company_id="C001")["tasks"][0]
            assert task["status"] == "open" and task["pending_invoice_ids"] == ["F003"]
    finally:
        runtime.close()


def test_agent_question_planner_uses_drafts_and_deferred_state(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        with TaxDatabase(business) as db:
            TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F006", "F009"])
            TaskLedger(db).apply(user_id="U001", company_id="C001", session_id="earlier", turn_id="t1", operations=[
                {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送验收文件", "save_requested": False},
                {"action": "defer", "invoice_id": "F009", "reason": "等经办人"},
            ])
        candidates = [{"id": "batch", "text": "请分别说明三张票的用途。", "targets": ["F003", "F006", "F009"],
                       "required_slots": {"F003": ["purpose"], "F006": ["purpose"], "F009": ["purpose"]}, "burden": 2, "priority": 5}]
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "plan_followup_questions", "args": {"candidates": candidates}, "id": "questions", "type": "tool_call"}]),
            AIMessage(content="F006 的用途是什么？"),
        ])
        result = runtime.run_turn(session_id="questions", user_id="U001", company_id="C001", message="继续办理，问我还需要的信息", model=model)
        assert result["status"] == "ok", result
        tool_results = [message["data"]["content"] for message in result["messages"] if message["type"] == "tool" and message["data"].get("name") == "plan_followup_questions"]
        question = json.loads(tool_results[-1])["questions"][0]
        assert question["coverage_slots"] == [{"target_id": "F006", "slot": "purpose"}]
        assert question["redundant_slots"] == [{"target_id": "F003", "slot": "purpose"}]
        assert question["waiting_targets"] == ["F009"]
    finally:
        runtime.close()


def test_agent_rule_preview_uses_current_rules_without_saving(business, tmp_path):
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "preview_rule_impact", "args": {"month": "2026-09", "condition_type": "amount_gte", "threshold_yuan": "500"}, "id": "preview", "type": "tool_call"}]),
            AIMessage(content="预览显示两张未填用途的票会新增为必填。"),
        ])
        result = runtime.run_turn(session_id="preview", user_id="U001", company_id="C002", message="如果门槛改为500元，预览影响", model=model)
        tool_results = [message["data"]["content"] for message in result["messages"] if message["type"] == "tool" and message["data"].get("name") == "preview_rule_impact"]
        preview = json.loads(tool_results[-1])
        assert preview["preview_only"] is True
        assert {item["invoice_id"] for item in preview["newly_required_missing"]} == {"F001", "F004"}
        with TaxDatabase(business) as db:
            assert db.connection.execute("SELECT COUNT(*) FROM rules WHERE rule_id='R003'").fetchone()[0] == 1
    finally:
        runtime.close()

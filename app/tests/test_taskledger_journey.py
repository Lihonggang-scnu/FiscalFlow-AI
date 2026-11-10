from __future__ import annotations

import json
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.agent import AgentRuntime
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager
from taxharness.workspace_presentation import read_workspace_page, sync_workspace_draft, workspace_shared_draft


ROOT = Path(__file__).resolve().parents[1]


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def test_multi_invoice_draft_correction_partial_save_and_new_session(tmp_path):
    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        create = ToolFake(responses=[_call("task_create", {"month": "2026-09", "scope": "all_missing"}, "create"), AIMessage(content="已建立任务。")])
        assert runtime.run_turn(session_id="first", user_id="U001", company_id="C001", message="检查甲企业九月缺用途发票。", model=create)["status"] == "ok"

        drafts = ToolFake(responses=[_call("update_task_memory", {"operations": [
            {"action": "draft", "invoice_id": "F002", "field": "purpose", "value": "购买培训资料"},
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送客户资料"},
            {"action": "draft", "invoice_id": "F006", "field": "purpose", "value": "项目管理服务"},
        ]}, "drafts"), AIMessage(content="已整理三张草稿，尚未保存。")])
        assert runtime.run_turn(session_id="first", user_id="U001", company_id="C001", message="三张先整理成草稿，暂时别保存。", model=drafts)["status"] == "ok"
        with TaxDatabase(business) as db:
            assert all(row[0] is None for row in db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id IN ('F002','F003','F006')"))

        correction = ToolFake(responses=[_call("update_task_memory", {"operations": [
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送客户验收文件"},
        ]}, "correct"), AIMessage(content="已更正 F003 草稿。")])
        assert runtime.run_turn(session_id="first", user_id="U001", company_id="C001", message="更正 F003，是寄送客户验收文件。", model=correction)["status"] == "ok"

        save = ToolFake(responses=[
            _call("save_invoice", {"invoice_id": "F002", "month": "2026-09", "changes": {"purpose": "购买培训资料"}}, "save-2"),
            _call("save_invoice", {"invoice_id": "F003", "month": "2026-09", "changes": {"purpose": "寄送客户验收文件"}}, "save-3"),
            _call("update_task_memory", {"operations": [{"action": "defer", "invoice_id": "F006", "reason": "稍后再处理"}]}, "wait"),
            AIMessage(content="两张已保存，F006 暂缓。"),
        ])
        assert runtime.run_turn(session_id="first", user_id="U001", company_id="C001", message="保存 F002 和 F003，F006 再等等。", model=save)["status"] == "ok"
        with TaxDatabase(business) as db:
            task = TaskManager(db).list_tasks(user_id="U001", company_id="C001")["tasks"][0]
            assert set(task["pending_invoice_ids"]) == {"F004", "F006", "F009"}
            assert TaskLedger(db).memory(user_id="U001", company_id="C001")["drafts"]["F006:purpose"]["value"] == "项目管理服务"

        resumed = ToolFake(responses=[_call("inspect_task_frontier", {}, "resume"), AIMessage(content="F006 仍有草稿并暂缓。")])
        result = runtime.run_turn(session_id="second", user_id="U001", company_id="C001", message="继续刚才的办件。", model=resumed)
        frontier = next(json.loads(message["data"]["content"]) for message in result["messages"] if message["type"] == "tool" and message["data"].get("name") == "inspect_task_frontier")
        assert frontier["frontier"]["drafts"]["F006"]["purpose"]["value"] == "项目管理服务"
        assert "F006" in frontier["frontier"]["waiting"]
        assert "F006" not in frontier["frontier"]["awaiting_confirmation"]

        page = read_workspace_page(database_path=business, user_id="U001", company_id="C001", month="2026-09")
        form = workspace_shared_draft(page, "F006")
        changed = sync_workspace_draft(database_path=business, user_id="U001", company_id="C001", invoice_id="F006",
            expected_revision=form["expected_revision"], changes={"purpose": "研发团队项目管理服务"}, expected_draft_events=form["draft_event_ids"],
            session_id="workbench", turn_id="workbench-edit")
        assert changed["status"] == "ok"
        final = ToolFake(responses=[_call("save_invoice", {"invoice_id": "F006", "month": "2026-09", "changes": {"purpose": "研发团队项目管理服务"}}, "save-6"), AIMessage(content="F006 已保存。")])
        assert runtime.run_turn(session_id="second", user_id="U001", company_id="C001", message="保存 F006 最新草稿。", model=final)["status"] == "ok"
        with TaxDatabase(business) as db:
            task = TaskManager(db).list_tasks(user_id="U001", company_id="C001")["tasks"][0]
            assert set(task["pending_invoice_ids"]) == {"F004", "F009"}
            assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F006'").fetchone()[0] == "研发团队项目管理服务"
    finally:
        runtime.close()

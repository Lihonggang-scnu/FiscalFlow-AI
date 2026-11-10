from __future__ import annotations

import ast
import inspect
from pathlib import Path

from taxharness.invoice_undo import undo_invoice_change
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager
from taxharness.workspace_presentation import read_workspace_page, sync_workspace_draft, workspace_shared_draft


ROOT = Path(__file__).resolve().parents[1]


def _new_business(tmp_path: Path) -> Path:
    database_path = tmp_path / "workspace.sqlite3"
    initialize_database(database_path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    return database_path


def _save_for_task(database_path: Path, invoice_ids: list[str], *, defer_invoice: str | None = None) -> tuple[str, str]:
    with TaxDatabase(database_path) as database:
        task_result = TaskManager(database).create_task(
            user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=invoice_ids,
        )
        task_id = task_result["task"]["task_id"]
        ledger = TaskLedger(database)
        ledger.frontier(user_id="U001", company_id="C001", session_id="ui-session", turn_id="baseline", observe=True)
        if defer_invoice:
            ledger.apply(
                user_id="U001", company_id="C001", session_id="ui-session", turn_id="defer-turn",
                operations=[{"action": "defer", "invoice_id": defer_invoice, "reason": "等待经办人回复"}],
            )
        saved = TaskManager(database).save_invoice(
            user_id="U001", company_id="C001", invoice_id="F003", month="2026-09",
            changes={"purpose": "寄送验收材料"}, expected_revision=1,
            idempotency_key="U001:C001:invoice:F003:workspace-save",
        )
        assert saved["status"] == "committed"
        operation = database.connection.execute(
            "SELECT operation_id FROM operations WHERE user_id='U001' AND company_id='C001' AND object_id='F003' AND idempotency_key='U001:C001:invoice:F003:workspace-save'"
        ).fetchone()
        ledger.invoice_saved(
            user_id="U001", company_id="C001", session_id="ui-session", turn_id="save-turn",
            invoice_id="F003", fields=["purpose"], task_id=task_id, source_operation_id=operation["operation_id"],
        )
        return task_id, operation["operation_id"]


def _invoice(page: dict, invoice_id: str) -> dict:
    return next(invoice for invoice in page["invoices"] if invoice["invoice_id"] == invoice_id)


def test_workspace_shows_live_frontier_diff_recent_events_and_user_scoped_undo(tmp_path):
    database_path = _new_business(tmp_path)
    task_id, operation_id = _save_for_task(database_path, ["F003", "F009"], defer_invoice="F009")

    page = read_workspace_page(
        database_path=database_path, user_id="U001", company_id="C001", month="2026-09",
        session_id="ui-session", turn_id="page-read",
    )
    readonly_page = read_workspace_page(
        database_path=database_path, user_id="U002", company_id="C001", month="2026-09",
        session_id="readonly-session", turn_id="page-read",
    )

    assert page["frontier"]["task_id"] == task_id
    assert page["frontier"]["completed"] == ["F003"]
    assert page["frontier"]["waiting"] == ["F009"]
    assert page["frontier"]["waiting_reasons"] == {"F009": "等待经办人回复"}
    assert page["frontier"]["rule"]["rule_id"]
    assert page["frontier"]["invoice_revisions"]["F003"] == _invoice(page, "F003")["revision"]
    assert page["diff"]["changes"]["completed"]["added"] == ["F003"]
    assert page["diff"]["changes"]["waiting"]["added"] == ["F009"]
    assert page["diff"]["changes"]["invoice_revisions"]["F003"]["before"] == 1
    assert any("F003 已保存用途" in change["summary"] for change in page["recent_changes"])
    assert any("F009 已暂缓" in change["summary"] for change in page["recent_changes"])
    assert _invoice(page, "F003")["undo_fields"] == ["purpose"]
    assert _invoice(readonly_page, "F003")["undo_fields"] == []
    assert readonly_page["tasks"] == []

    with TaxDatabase(database_path) as database:
        saved_event = next(
            event for event in TaskLedger(database).events(user_id="U001", company_id="C001")
            if event["kind"] == "invoice_saved"
            and event["payload"].get("source_operation_id") == operation_id
        )
        assert saved_event["task_id"] == task_id
        assert saved_event["payload"]["fields"] == ["purpose"]


def test_undo_of_latest_saved_field_reopens_task_and_refreshes_frontier(tmp_path):
    database_path = _new_business(tmp_path)
    task_id, _ = _save_for_task(database_path, ["F003"])
    before = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month="2026-09")
    assert _invoice(before, "F003")["undo_fields"] == ["purpose"]

    with TaxDatabase(database_path) as database:
        result = undo_invoice_change(
            database, user_id="U001", company_id="C001", invoice_id="F003", field="purpose",
            session_id="ui-session", turn_id="undo-turn",
        )

    assert result["status"] == "committed"
    assert result["undone_fields"] == ["purpose"]
    assert result["task_reopen_status"] == "reopened"
    after = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month="2026-09")
    assert after["frontier"]["task_id"] == task_id
    assert after["frontier"]["needs_input"] == ["F003"]
    assert _invoice(after, "F003")["purpose"] is None
    assert _invoice(after, "F003")["undo_fields"] == []


def test_chat_and_workspace_share_task_drafts_without_writing_invoice(tmp_path):
    database_path = _new_business(tmp_path)
    with TaxDatabase(database_path) as database:
        TaskManager(database).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])
        TaskLedger(database).apply(user_id="U001", company_id="C001", session_id="chat", turn_id="chat-1", operations=[
            {"action": "draft", "invoice_id": "F003", "field": "purpose", "value": "寄送客户资料"},
            {"action": "draft", "invoice_id": "F009", "field": "purpose", "value": "维护生产设备"},
        ])
    page = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month="2026-09")
    form = workspace_shared_draft(page, "F003")
    assert form["purpose"] == "寄送客户资料"
    changed = sync_workspace_draft(database_path=database_path, user_id="U001", company_id="C001", invoice_id="F003",
        expected_revision=form["expected_revision"], changes={"purpose": "寄送客户验收文件"},
        expected_draft_events=form["draft_event_ids"], session_id="ui", turn_id="edit-1")
    assert changed["status"] == "ok"
    with TaxDatabase(database_path) as database:
        memory = TaskLedger(database).memory(user_id="U001", company_id="C001")
        assert memory["drafts"]["F003:purpose"]["value"] == "寄送客户验收文件"
        assert memory["drafts"]["F009:purpose"]["value"] == "维护生产设备"
        assert database.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] is None
    stale = sync_workspace_draft(database_path=database_path, user_id="U001", company_id="C001", invoice_id="F003",
        expected_revision=form["expected_revision"], changes={"purpose": "过期编辑"},
        expected_draft_events=form["draft_event_ids"], session_id="ui", turn_id="edit-2")
    assert stale["status"] == "revision_conflict"


def test_chainlit_callbacks_match_taskledger_and_undo_interfaces():
    app_path = ROOT / "scripts" / "chainlit_app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    callbacks = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    save_source = ast.unparse(callbacks["workspace_save_invoice"])
    undo_source = ast.unparse(callbacks["workspace_undo_invoice"])

    assert "SELECT operation_id FROM operations" in save_source
    assert "ledger.invoice_saved" in save_source
    assert "source_operation_id=operation['operation_id']" in save_source
    assert "_workspace_task_for_invoice" in save_source
    assert "undo_invoice_change" in undo_source
    assert "field=field" in undo_source and "session_id=state.session_id" in undo_source
    assert "field not in visible[invoice_id].get('undo_fields', [])" in undo_source
    assert "refresh_tasks=True" in undo_source

    save_parameters = inspect.signature(TaskLedger.invoice_saved).parameters
    undo_parameters = inspect.signature(undo_invoice_change).parameters
    assert {"task_id", "source_operation_id", "session_id", "turn_id"} <= set(save_parameters)
    assert {"database", "user_id", "company_id", "invoice_id", "field", "session_id", "turn_id"} <= set(undo_parameters)
    assert "turn_id=state.workspace_turn_id or \"\"" in app_path.read_text(encoding="utf-8")


def test_welcome_panel_uses_real_frontier_and_hides_undo_for_readonly_users():
    frontend = (ROOT / "public" / "elements" / "WelcomePanel.jsx").read_text(encoding="utf-8")
    stylesheet = (ROOT / "public" / "chainlit_ui.css").read_text(encoding="utf-8")

    assert "data.frontier" in frontend and "data.diff" in frontend
    assert "frontier.waiting_reasons" in frontend and "frontier.drafts" in frontend
    assert "data.recent_changes" in frontend and "frontierDiffLines" in frontend
    assert 'key === "rule"' in frontend and 'key === "invoice_revisions"' in frontend
    assert 'name: "workspace_undo_invoice"' in frontend
    assert "canModify && undoFields.length" in frontend
    assert 'readOnly={!canModify}' in frontend
    assert ".th-work-frontier" in stylesheet and ".th-undo-saved-fields" in stylesheet

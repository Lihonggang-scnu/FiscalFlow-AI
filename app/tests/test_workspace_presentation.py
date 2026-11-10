from __future__ import annotations

import sqlite3
import ast
from pathlib import Path

from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager
from taxharness.workspace_presentation import read_workspace_page


APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE_XLSX = APP_ROOT / "data" / "02_示例数据.xlsx"
RULES_DOC = APP_ROOT / "data" / "03_业务规则.docx.txt"


def _initialize_test_database(tmp_path: Path) -> Path:
    destination = tmp_path / "business.sqlite3"
    initialize_database(destination, SOURCE_XLSX, RULES_DOC)
    return destination


def _invoice_by_id(data: dict, invoice_id: str) -> dict:
    return next(invoice for invoice in data["invoices"] if invoice["invoice_id"] == invoice_id)


def _task_revisions(database_path: Path) -> list[tuple]:
    uri = database_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return connection.execute(
            "SELECT task_id,revision,status,target_invoice_ids_json,pending_invoice_ids_json,updated_at FROM tasks ORDER BY task_id"
        ).fetchall()


def test_monthly_invoice_purpose_status_uses_the_program_rule_result(tmp_path):
    database_path = _initialize_test_database(tmp_path)

    c001 = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month="2026-09")
    c002 = read_workspace_page(database_path=database_path, user_id="U001", company_id="C002", month="2026-09")

    assert _invoice_by_id(c001, "F009")["purpose_status"] == "required"
    assert _invoice_by_id(c002, "F002")["purpose_status"] == "required"
    assert _invoice_by_id(c002, "F004")["purpose_status"] == "optional"
    assert _invoice_by_id(c002, "F005")["purpose_status"] == "required"
    assert _invoice_by_id(c002, "F006")["purpose_status"] == "required"
    u002 = read_workspace_page(database_path=database_path, user_id="U002", company_id="C001", month="2026-09")
    assert u002["can_modify"] is False
    assert _invoice_by_id(u002, "F009")["purpose_status"] == "required"


def test_out_of_scope_and_unavailable_rule_do_not_guess_missing_purpose(tmp_path):
    database_path = _initialize_test_database(tmp_path)

    august = read_workspace_page(database_path=database_path, user_id="U001", company_id="C002", month="2026-08")
    assert august["purpose_check_status"] == "out_of_scope"
    assert _invoice_by_id(august, "F011")["purpose_status"] == "undetermined"

    with TaxDatabase(database_path) as database:
        database.connection.execute(
            "UPDATE rules SET status='inactive' WHERE company_id='C002' AND kind='invoice_purpose' AND rule_id='R003'"
        )
    no_rule = read_workspace_page(database_path=database_path, user_id="U001", company_id="C002", month="2026-09")
    assert no_rule["purpose_check_status"] == "rule_not_found"
    assert _invoice_by_id(no_rule, "F004")["purpose_status"] == "undetermined"


def test_unselected_month_and_period_read_leave_task_state_unchanged(tmp_path):
    database_path = _initialize_test_database(tmp_path)
    with TaxDatabase(database_path) as database:
        created = TaskManager(database).create_task(user_id="U001", company_id="C001", month="2026-09", scope="all_missing")
        assert created["status"] == "created"
    before = _task_revisions(database_path)

    empty = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month=None)
    period = read_workspace_page(database_path=database_path, user_id="U001", company_id="C001", month="2026-09")

    assert empty["invoices"] == empty["tasks"] == empty["rules"] == []
    assert period["month"] == "2026-09"
    assert _task_revisions(database_path) == before
    app_source = Path(__file__).resolve().parents[1] / "scripts" / "chainlit_app.py"
    app_tree = ast.parse(app_source.read_text(encoding="utf-8"))
    month_callback = next(node for node in app_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_context_month")
    assert not any(
        keyword.arg == "refresh_tasks" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        for node in ast.walk(month_callback)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_publish_workspace_page"
        for keyword in node.keywords
    )


def test_page_switch_refreshes_work_view_without_changing_session_identity():
    source = Path(__file__).resolve().parents[1] / "scripts" / "chainlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    callback = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_workspace_page")
    calls = {node.func.id for node in ast.walk(callback) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assigned_attributes = {
        target.attr
        for node in ast.walk(callback)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "state"
    }

    assert "_publish_workspace_page" in calls
    assert not {"session_id", "user_id", "company_id", "context_month"} & assigned_attributes
    assert "workspace_drafts" in ast.unparse(callback)


def test_invoice_save_keeps_acl_revision_idempotency_and_refresh_receipt_boundaries():
    source = Path(__file__).resolve().parents[1] / "scripts" / "chainlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    callback = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "workspace_save_invoice")
    callback_text = ast.unparse(callback)

    assert "service.require_access(user_id=state.user_id, company_id=state.company_id, modify=True)" in callback_text
    assert "TaskManager(database, service).save_invoice" in callback_text
    assert "expected_revision=expected_revision" in callback_text
    assert "idempotency_key=idempotency_key" in callback_text
    assert "task_refresh_status" in callback_text
    assert "no_active_task" in callback_text and "ok" in callback_text

    frontend = (Path(__file__).resolve().parents[1] / "public" / "elements" / "WelcomePanel.jsx").read_text(encoding="utf-8")
    assert 'readOnly={!canModify}' in frontend
    assert 'disabled={!canModify}' not in frontend


def test_rule_views_show_chinese_validity_and_actual_version():
    frontend = (Path(__file__).resolve().parents[1] / "public" / "elements" / "WelcomePanel.jsx").read_text(encoding="utf-8")

    assert "function ruleStatusLabel" in frontend
    assert "function ruleVersionLabel" in frontend
    assert "有效" in frontend and "规则冲突" in frontend and "无适用规则" in frontend
    assert "purposeRule?.execution_summary" in frontend
    assert "purposeRules.map" in frontend
    assert "execution_status || rule.status" not in frontend

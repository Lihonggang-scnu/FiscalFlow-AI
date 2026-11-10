from __future__ import annotations

from pathlib import Path

import pytest

from taxharness.errors import PermissionDeniedError
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    initialize_database(path, SOURCE_XLSX, RULES_DOC)
    with TaxDatabase(path) as database:
        yield database


def test_all_missing_save_replay_resume_and_selected_low_amount(db):
    manager = TaskManager(db)
    created = manager.create_task(user_id="U001", company_id="C001", month="2026-09", scope="all_missing")
    assert created["status"] == "created"
    task_id = created["task"]["task_id"]
    assert set(created["task"]["pending_invoice_ids"]) == {"F002", "F003", "F004", "F006", "F009"}
    resumed = manager.resume_task(task_id=None, user_id="U001", company_id="C001")
    assert resumed["status"] == "resumed" and resumed["task"]["task_id"] == task_id
    saved = manager.save_invoice_for_task(
        task_id=task_id, user_id="U001", company_id="C001", invoice_id="F002",
        changes={"purpose": "办公资料"}, expected_revision=1,
        idempotency_key="U001:C001:invoice:F002:tm-1",
    )
    assert saved["status"] == "committed" and "F002" not in saved["task"]["pending_invoice_ids"]
    assert manager.complete_task(task_id=task_id, user_id="U001", company_id="C001")["status"] == "incomplete"
    replay = manager.save_invoice_for_task(
        task_id=task_id, user_id="U001", company_id="C001", invoice_id="F002",
        changes={"purpose": "办公资料"}, expected_revision=1,
        idempotency_key="U001:C001:invoice:F002:tm-1",
    )
    assert replay["status"] == "duplicate" and replay["task_refresh_status"] == "ok"
    with TaxDatabase(db.path) as reopened:
        resumed_again = TaskManager(reopened).resume_task(task_id=None, user_id="U001", company_id="C001")
        assert resumed_again["status"] == "resumed"
        assert set(resumed_again["task"]["pending_invoice_ids"]) == {"F003", "F004", "F006", "F009"}

    selected = manager.create_task(user_id="U001", company_id="C002", month="2026-09", scope="selected", invoice_ids=["F004"])
    assert selected["status"] == "created" and selected["task"]["pending_invoice_ids"] == ["F004"]
    selected_save = manager.save_invoice_for_task(
        task_id=selected["task"]["task_id"], user_id="U001", company_id="C002", invoice_id="F004",
        changes={"purpose": "用户主动补充"}, expected_revision=1,
        idempotency_key="U001:C002:invoice:F004:tm-1",
    )
    assert selected_save["status"] == "committed" and selected_save["task"]["status"] == "completed"
    assert manager.complete_task(task_id=selected["task"]["task_id"], user_id="U001", company_id="C002")["status"] == "completed"


def test_user_task_isolation_and_business_write_permission(db):
    manager = TaskManager(db)
    u2 = manager.create_task(user_id="U002", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])
    assert u2["status"] == "created"
    own = manager.list_tasks(user_id="U002", company_id="C001")["tasks"]
    assert [row["task_id"] for row in own] == [u2["task"]["task_id"]]
    with pytest.raises(PermissionDeniedError):
        manager.list_tasks(user_id="U002", company_id="C002")
    denied = manager.save_invoice_for_task(
        task_id=u2["task"]["task_id"], user_id="U002", company_id="C001", invoice_id="F003",
        changes={"purpose": "不应写入"}, expected_revision=1,
        idempotency_key="U002:C001:invoice:F003:tm-denied",
    )
    assert denied["status"] == "permission_denied" and denied["task_progress_updated"] is False
    assert "仅有查看权限" in denied["summary"]
    row = db.connection.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()
    assert row["purpose"] is None


def test_correction_out_of_scope_does_not_change_task_and_cancel_delete(db):
    manager = TaskManager(db)
    created = manager.create_task(user_id="U001", company_id="C001", month="2026-09", scope="all_missing")
    task_id = created["task"]["task_id"]
    old_revision = created["task"]["revision"]
    out = manager.correct_task(task_id=task_id, user_id="U001", company_id="C001", changes={"month": "2026-08"})
    assert out["status"] == "out_of_scope" and out["task"]["month"] == "2026-09"
    current = manager.get_task(task_id=task_id, user_id="U001", company_id="C001")["task"]
    assert current["revision"] == old_revision and current["month"] == "2026-09"
    cancelled = manager.cancel_task(task_id=task_id, user_id="U001", company_id="C001")
    assert cancelled["status"] == "cancelled"
    assert manager.resume_task(task_id=task_id, user_id="U001", company_id="C001")["status"] == "not_resumable"
    deleted = manager.delete_task(task_id=task_id, user_id="U001", company_id="C001")
    assert deleted["status"] == "deleted"
    assert manager.get_task(task_id=task_id, user_id="U001", company_id="C001")["status"] == "not_found"
    assert manager.refresh_task(task_id=task_id, user_id="U001", company_id="C001")["status"] == "not_found"


def test_waiting_month_and_invalid_selected_target_preserve_task(db):
    manager = TaskManager(db)
    out_create = manager.create_task(user_id="U001", company_id="C002", month="2026-08", scope="all_missing")
    assert out_create["status"] == "out_of_scope"
    assert manager.list_tasks(user_id="U001", company_id="C002")["tasks"] == []
    waiting = manager.create_task(user_id="U001", company_id="C001", scope="all_missing")
    assert waiting["task"]["status"] == "waiting_input"
    out = manager.correct_task(task_id=waiting["task"]["task_id"], user_id="U001", company_id="C001", changes={"month": "2026-08"})
    assert out["status"] == "out_of_scope" and manager.get_task(task_id=waiting["task"]["task_id"], user_id="U001", company_id="C001")["task"]["status"] == "waiting_input"
    invalid = manager.create_task(user_id="U001", company_id="C002", month="2026-09", scope="selected", invoice_ids=["F999"])
    assert invalid["status"] == "invalid_target"

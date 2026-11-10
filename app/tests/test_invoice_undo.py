from pathlib import Path

import pytest

from taxharness.errors import PermissionDeniedError
from taxharness.invoice_undo import undo_invoice_change
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as database:
        yield database


def _save(db, *, user, changes, revision, suffix):
    return TaskManager(db).save_invoice(user_id=user, company_id="C001", invoice_id="F003", month="2026-09",
        changes=changes, expected_revision=revision, idempotency_key=f"{user}:C001:invoice:F003:{suffix}")


def test_undo_restores_saved_value_and_refreshes_pending_task(db):
    tasks = TaskManager(db)
    task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])["task"]
    assert _save(db, user="U001", changes={"purpose": "寄送验收材料"}, revision=1, suffix="first")["status"] == "committed"
    assert tasks.get_task(task_id=task["task_id"], user_id="U001", company_id="C001")["task"]["pending_invoice_ids"] == ["F009"]
    result = undo_invoice_change(db, user_id="U001", company_id="C001", invoice_id="F003", field="purpose")
    assert result["status"] == "committed" and result["undone_fields"] == ["purpose"]
    current = TaxService(db).read_invoice(user_id="U001", company_id="C001", invoice_id="F003")["data"]
    assert current["purpose"] is None and current["revision"] == 3
    assert set(tasks.get_task(task_id=task["task_id"], user_id="U001", company_id="C001")["task"]["pending_invoice_ids"]) == {"F003", "F009"}
    assert undo_invoice_change(db, user_id="U001", company_id="C001", invoice_id="F003", field="purpose")["status"] == "already_undone"
    assert TaxService(db).read_invoice(user_id="U001", company_id="C001", invoice_id="F003")["data"]["revision"] == 3


def test_undo_preserves_later_change_to_another_field(db):
    assert _save(db, user="U001", changes={"purpose": "寄送验收材料"}, revision=1, suffix="purpose")["status"] == "committed"
    assert _save(db, user="U001", changes={"remarks": "经办人已核实"}, revision=2, suffix="remark")["status"] == "committed"
    result = undo_invoice_change(db, user_id="U001", company_id="C001", invoice_id="F003", field="purpose")
    assert result["status"] == "committed"
    current = TaxService(db).read_invoice(user_id="U001", company_id="C001", invoice_id="F003")["data"]
    assert current["purpose"] is None and current["remarks"] == "经办人已核实"


def test_undo_does_not_overwrite_another_users_later_value(db):
    db.connection.execute("INSERT INTO users(user_id,name) VALUES('U003','另一位测试用户')")
    db.connection.execute("INSERT INTO user_company_permissions(user_id,company_id,can_view,can_modify) VALUES('U003','C001',1,1)")
    assert _save(db, user="U001", changes={"purpose": "首次用途"}, revision=1, suffix="first")["status"] == "committed"
    assert _save(db, user="U003", changes={"purpose": "后来确认的用途"}, revision=2, suffix="later")["status"] == "committed"
    result = undo_invoice_change(db, user_id="U001", company_id="C001", invoice_id="F003", field="purpose")
    assert result["status"] == "revision_conflict"
    assert TaxService(db).read_invoice(user_id="U001", company_id="C001", invoice_id="F003")["data"]["purpose"] == "后来确认的用途"


def test_read_only_user_cannot_undo(db):
    with pytest.raises(PermissionDeniedError):
        undo_invoice_change(db, user_id="U002", company_id="C001", invoice_id="F003", field="purpose")

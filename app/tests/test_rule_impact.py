import json
from pathlib import Path

import pytest

from taxharness.errors import PermissionDeniedError
from taxharness.rule_impact import preview_purpose_rule
from taxharness.rule_manager import RuleManager
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as value:
        yield value


def test_rule_preview_compares_real_invoices_without_changing_business_state(db):
    task = TaskManager(db).create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")["task"]
    rules_before = db.connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    result = preview_purpose_rule(db, user_id="U001", company_id="C002", month="2026-09", condition_type="amount_gte", threshold_yuan="500.00")
    assert result["preview_only"] is True
    assert result["before_missing"] == ["F002", "F005", "F006"]
    assert {item["invoice_id"] for item in result["newly_required_missing"]} == {"F001", "F004"}
    assert {item["invoice_id"] for item in result["newly_required_filled"]} == {"F008"}
    assert set(result["after_missing"]) == {"F001", "F002", "F004", "F005", "F006"}
    assert result["task_impacts"][0]["added_pending"] == ["F001", "F004"]
    assert db.connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == rules_before
    assert TaskManager(db).get_task(task_id=task["task_id"], user_id="U001", company_id="C002")["task"]["revision"] == task["revision"]


def test_actual_rule_update_changes_work_frontier(db, tmp_path):
    tasks = TaskManager(db)
    tasks.create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")
    ledger = TaskLedger(db)
    before = ledger.frontier(user_id="U001", company_id="C002", session_id="s", turn_id="t1")
    assert set(before["frontier"]["needs_input"]) == {"F002", "F005", "F006"}
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(json.dumps({"rules": [{
        "rule_id": "R003", "version": "2.0", "name": "乙企业发票用途规则", "kind": "invoice_purpose",
        "company_id": "C002", "effective_from": "2026-09-01", "status": "active",
        "content": "金额达到500元必须填写用途。", "condition": {"type": "amount_gte", "amount_cents": 50000},
    }]}, ensure_ascii=False), encoding="utf-8")
    assert RuleManager(db).update_file(rule_file)["status"] == "updated"
    after = ledger.frontier(user_id="U001", company_id="C002", session_id="s", turn_id="t2")
    assert after["diff"]["added_targets"] == ["F001", "F004"]
    assert after["diff"]["changes"]["rule"]["before"]["version"] == "1.0"
    assert after["diff"]["changes"]["rule"]["after"]["version"] == "2.0"
    assert set(after["frontier"]["needs_input"]) == {"F001", "F002", "F004", "F005", "F006"}


def test_selected_task_remains_user_selected_under_rule_preview(db):
    TaskManager(db).create_task(user_id="U001", company_id="C002", month="2026-09", scope="selected", invoice_ids=["F002", "F005"])
    result = preview_purpose_rule(db, user_id="U001", company_id="C002", month="2026-09", condition_type="none")
    assert result["after_missing"] == []
    assert result["task_impacts"][0]["scope"] == "selected"
    assert result["task_impacts"][0]["removed_pending"] == []


def test_rule_preview_respects_access(db):
    with pytest.raises(PermissionDeniedError):
        preview_purpose_rule(db, user_id="U002", company_id="C002", month="2026-09", condition_type="all")

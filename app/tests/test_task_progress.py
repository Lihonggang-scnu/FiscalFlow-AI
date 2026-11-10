from pathlib import Path

from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger
from taxharness.task_manager import TaskManager
from taxharness.rule_manager import RuleManager
from test_rules_management import _write_rule


ROOT = Path(__file__).resolve().parents[1]


def test_all_missing_task_keeps_completed_in_the_progress_denominator(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as db:
        tasks = TaskManager(db)
        task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="all_missing")["task"]
        ledger = TaskLedger(db)
        before = ledger.frontier(user_id="U001", company_id="C001")["frontier"]["progress"]
        assert set(before["tracked_targets"]) == {"F002", "F003", "F004", "F006", "F009"}
        saved = tasks.save_invoice(user_id="U001", company_id="C001", invoice_id="F002", month="2026-09",
                                   changes={"purpose": "员工培训资料"}, expected_revision=1, idempotency_key="U001:C001:invoice:F002:progress-save")
        assert saved["status"] == "committed"
        after = ledger.frontier(user_id="U001", company_id="C001")["frontier"]["progress"]
        assert after["initial_targets"] == before["initial_targets"]
        assert len(after["tracked_targets"]) == 5 and after["completed"] == ["F002"]
        assert set(after["remaining"]) == {"F003", "F004", "F006", "F009"}
        assert after["removed_by_rule"] == [] and after["removed_by_scope"] == []


def test_scope_removal_is_not_reported_as_a_completed_invoice(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as db:
        tasks = TaskManager(db)
        task = tasks.create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003", "F009"])["task"]
        tasks.correct_task(task_id=task["task_id"], user_id="U001", company_id="C001", changes={"invoice_ids": ["F003"]})
        progress = TaskLedger(db).frontier(user_id="U001", company_id="C001")["frontier"]["progress"]
        assert progress["tracked_targets"] == ["F003", "F009"]
        assert progress["removed_by_scope"] == ["F009"]
        assert progress["remaining"] == ["F003"] and progress["completed"] == []


def test_future_task_event_does_not_change_current_progress(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as db:
        task = TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09", scope="selected", invoice_ids=["F003"])["task"]
        ledger = TaskLedger(db)
        future = {**task, "target_invoice_ids": ["F003", "F009"], "pending_invoice_ids": ["F003", "F009"]}
        ledger.append(user_id="U001", company_id="C001", session_id="s", turn_id="future", task_id=task["task_id"],
                      kind="task_changed", payload={"before": task, "after": future}, occurred_at="2099-01-01T00:00:00+08:00")
        progress = ledger.frontier(user_id="U001", company_id="C001")["frontier"]["progress"]
        assert progress["tracked_targets"] == ["F003"]


def test_rule_removal_is_distinct_from_completed_work(tmp_path):
    path = tmp_path / "business.sqlite3"
    initialize_database(path, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(path) as db:
        TaskManager(db).create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")
        ledger = TaskLedger(db)
        ledger.frontier(user_id="U001", company_id="C002")
        rule = _write_rule(tmp_path / "raised-threshold.json", condition={"type": "amount_gte", "amount_cents": 120000}, content="金额达到1200.00元时必须填写用途。")
        RuleManager(db).import_file(rule)
        progress = ledger.frontier(user_id="U001", company_id="C002")["frontier"]["progress"]
        assert set(progress["tracked_targets"]) == {"F002", "F005", "F006"}
        assert set(progress["removed_by_rule"]) == {"F002", "F005"}
        assert progress["remaining"] == ["F006"] and progress["completed"] == []

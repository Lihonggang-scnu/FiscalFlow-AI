"""Exercise a committed write whose acknowledgement is interrupted."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


APP = Path(__file__).resolve().parents[1]
UPGRADE = APP.parent
PROJECT = UPGRADE.parents[1]
sys.path.insert(0, str(APP / "src"))

from taxharness.agent import AgentRuntime
from taxharness.provider import _peak_window
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


def _invoice_and_commits(path: Path) -> tuple[dict, int]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        invoice = connection.execute("SELECT purpose,remarks,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'").fetchone()
        count = connection.execute("SELECT COUNT(*) FROM operations WHERE company_id='C001' AND object_id='F002' AND status='committed'").fetchone()[0]
        return dict(invoice), count


def _total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _historical_receipt_explained(reply: str) -> bool:
    confirmed = ("已提交", "提交成功", "已保存", "保存成功", "已写入", "已生效", "已确认", "已完成")
    denied = ("无法确认", "不能确认", "没有留下", "没有相关", "无法确定")
    return "购买培训资料" in reply and any(word in reply for word in confirmed) and not any(word in reply for word in denied)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed", action="store_true", help="Change the invoice again before recovery.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("提交发票 → 模拟回执中断 → 重启 → 核对当前值与提交次数" + ("（恢复前另行更正）" if args.changed else ""))
        return 0
    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行恢复场景。")
    budget = json.loads((UPGRADE / "budget.json").read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _total(ledger)
    run_root = UPGRADE / "experiments" / ("recovery-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    database = run_root / "business.sqlite3"
    initialize_database(database, APP / "data/02_示例数据.xlsx", APP / "data/03_业务规则.docx.txt")
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)

    def make_runtime():
        return AgentRuntime(database_path=database, agent_store_path=run_root / "agent.sqlite3",
                            checkpoint_path=run_root / "checkpoint.sqlite3", preference_store_path=run_root / "preferences.sqlite3",
                            budget_ledger_path=ledger)

    report = {"scenario": "receipt_lost_then_changed" if args.changed else "receipt_lost", "steps": []}
    original_save = TaskManager.save_invoice
    interrupted = {"fired": False}

    def interrupt_after_commit(self, **kwargs):
        result = original_save(self, **kwargs)
        if kwargs.get("invoice_id") == "F002" and result.get("status") == "committed" and not interrupted["fired"]:
            interrupted["fired"] = True
            raise RuntimeError("模拟提交后回执中断")
        return result

    runtime = make_runtime()
    try:
        TaskManager.save_invoice = interrupt_after_commit
        first = runtime.run_turn(session_id="recover", user_id="U001", company_id="C001",
            message="把甲企业2026年9月F002用途保存为购买培训资料。", live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
        TaskManager.save_invoice = original_save
        after_first, first_commits = _invoice_and_commits(database)
        report["steps"].append({"name": "提交后中断", "status": first.get("status"), "invoice": after_first, "commits": first_commits})
        runtime.close()
        runtime = None
        if first.get("status") != "error" or not interrupted["fired"] or after_first["purpose"] != "购买培训资料" or first_commits != 1:
            report["checks"] = {"interruption_reproduced": False}
            report["passed"] = False
            return 1
        if args.changed:
            with TaxDatabase(database) as db:
                updated = TaxService(db).update_invoice(user_id="U001", company_id="C001", invoice_id="F002",
                    changes={"purpose": "后来更正的培训资料"}, expected_revision=after_first["revision"],
                    idempotency_key="U001:C001:invoice:F002:external-correction")
                if updated.get("status") != "committed":
                    raise RuntimeError("无法准备恢复前的模拟更正。")
        runtime = make_runtime()
        question = "刚才保存被中断了，请核对那次保存和现在F002的用途，不要再次修改发票。"
        second = runtime.run_turn(session_id="recover", user_id="U001", company_id="C001", message=question,
                                  live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
        current, total_commits = _invoice_and_commits(database)
        reply = second.get("reply", "")
        expected = "后来更正的培训资料" if args.changed else "购买培训资料"
        report["steps"].append({"name": "重启后核对", "status": second.get("status"), "reply": reply, "invoice": current, "commits": total_commits})
        report["checks"] = {"interruption_reproduced": True, "recovery_completed": second.get("status") == "ok",
                            "current_value_correct": current["purpose"] == expected,
                            "current_value_in_reply": expected in reply,
                            "historical_receipt_explained": _historical_receipt_explained(reply),
                            "no_duplicate_write": total_commits == (2 if args.changed else 1)}
        report["passed"] = all(report["checks"].values())
        return 0 if report["passed"] else 1
    finally:
        TaskManager.save_invoice = original_save
        if runtime is not None:
            runtime.close()
        after_count, after_total = _total(ledger)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

"""Run Chinese enterprise reply, shared draft, confirmation and review with DeepSeek."""

from __future__ import annotations

import argparse
import csv
import io
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

from taxharness.enterprise.agent import generate_reply_drafts
from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.conversation import EnterpriseAgentRuntime
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase
from taxharness.provider import _peak_window


def _total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("中国企业十月票据 → 经办人自然回复 → AI拆分草稿 → 对话更正 → 新会话确认 → 会计复核")
        return 0
    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行企业场景。")
    budget_path = Path(os.getenv("TAXHARNESS_UPGRADE_BUDGET", str(UPGRADE / "budget.json")))
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _total(ledger)
    run_root = Path(os.getenv("TAXHARNESS_EXPERIMENTS_DIR", str(UPGRADE / "experiments"))) / ("enterprise-live-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    database = run_root / "enterprise.sqlite3"
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)
    report = {"steps": []}
    runtime = None
    try:
        with EnterpriseDatabase(database) as db:
            admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password="test-only-password-2026")
            service = EnterpriseService(db)
            company = service.create_company(actor=admin, name="青岚科技（测试）", currency="CNY", require_independent_review=True)
            accountant = create_user(db, username="accountant@example.test", display_name="会计", password="test-only-password-2026")
            reviewer = create_user(db, username="reviewer@example.test", display_name="复核", password="test-only-password-2026")
            requester = create_user(db, username="requester@example.test", display_name="经办人", password="test-only-password-2026")
            for person, role in ((accountant, "accountant"), (reviewer, "reviewer"), (requester, "requester")):
                assign_membership(db, user_id=person["user_id"], company_id=company["company_id"], role=role)
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "purpose", "remarks"])
            writer.writerows([["S001", "2026-10-08", "240.00", "CNY", "青禾文具（测试）", "办公材料", "", ""],
                              ["S002", "2026-10-10", "500.00", "CNY", "远行服务（测试）", "服务费", "", ""]])
            importer = ImportManager(db)
            preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="测试ERP", filename="october.csv", content=output.getvalue().encode("utf-8-sig"), mapping={})
            importer.commit(user=accountant, preview_id=preview["preview_id"])
            task = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-10")["work_item"]
            items = service.work_item(user=accountant, task_id=task["task_id"])["invoices"]["items"]
            ids = {item["source_invoice_id"]: item["invoice_id"] for item in items}
            request = service.create_request(user=accountant, task_id=task["task_id"], respondent_user_id=requester["user_id"], invoice_ids=[ids["S001"], ids["S002"]], message="请说明两张发票的实际用途。")
            response = service.submit_reply(user=requester, request_id=request["request_id"], reply_text="S001是部门培训材料；S002还要问项目负责人，先等等。")
            extracted = generate_reply_drafts(db, user=accountant, response_id=response["response_id"], live_authorized=True,
                                               budget_path=budget_path, ledger_path=ledger)
            draft = next((item for item in extracted["drafts"] if item.get("invoice_id") == ids["S001"] and item.get("proposed_purpose") == "部门培训材料"), None)
            no_write = db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (ids["S001"],)).fetchone()[0] is None
            report["steps"].append({"name": "经办人回复拆分", "passed": bool(draft and no_write), "draft_count": extracted["draft_count"], "invoice_unchanged": no_write})
            print(f"经办人回复拆分：{'通过' if draft and no_write else '需处理'}", flush=True)
            if not draft or not no_write:
                report["passed"] = False
                return 1
        runtime = EnterpriseAgentRuntime(database_path=database, agent_store_path=run_root / "agent.sqlite3",
            checkpoint_path=run_root / "checkpoint.sqlite3", preference_store_path=run_root / "preferences.sqlite3",
            budget_ledger_path=ledger, active_task_id=task["task_id"])
        first = runtime.run_turn(session_id="enterprise-a", user_id=accountant["user_id"], company_id=company["company_id"],
            message="把S001的用途草稿更正为新员工培训材料，先不要保存发票。", live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
        with EnterpriseDatabase(database) as db:
            detail = EnterpriseService(db).work_item(user=accountant, task_id=task["task_id"])
            current = next(item for item in detail["drafts"] if item["draft_id"] == draft["draft_id"])
            value = db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (ids["S001"],)).fetchone()[0]
        ok = first.get("status") == "ok" and current["proposed_purpose"] == "新员工培训材料" and value is None
        report["steps"].append({"name": "对话更正草稿", "passed": ok, "reply": first.get("reply", ""), "draft": current["proposed_purpose"], "invoice": value})
        print(f"对话更正草稿：{'通过' if ok else '需处理'}", flush=True)
        if not ok:
            report["passed"] = False
            return 1
        second = runtime.run_turn(session_id="enterprise-b", user_id=accountant["user_id"], company_id=company["company_id"],
            message="继续这个工作项，保存刚才更正的S001用途草稿；S002仍等经办人确认。", live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
        with EnterpriseDatabase(database) as db:
            service = EnterpriseService(db)
            detail = service.work_item(user=accountant, task_id=task["task_id"])
            by_source = {item["source_invoice_id"]: item for item in detail["invoices"]["items"]}
            saved = by_source["S001"]["purpose"] == "新员工培训材料" and by_source["S001"]["task_status"] == "awaiting_review"
            other_unchanged = by_source["S002"]["purpose"] is None
            ok = second.get("status") == "ok" and saved and other_unchanged
            report["steps"].append({"name": "新会话确认并待复核", "passed": ok, "reply": second.get("reply", ""), "saved": saved, "other_unchanged": other_unchanged})
            print(f"新会话确认并待复核：{'通过' if ok else '需处理'}", flush=True)
            if ok:
                review_token = next(row["review_token"] for row in service.work_item(user=reviewer, task_id=task["task_id"])["invoices"]["items"] if row["invoice_id"] == ids["S001"])
                reviewed = service.review_invoice(user=reviewer, task_id=task["task_id"], invoice_id=ids["S001"], action="approve", review_token=review_token)
                report["steps"].append({"name": "复核人批准", "passed": reviewed.get("status") == "ok"})
                ok = reviewed.get("status") == "ok"
        report["passed"] = ok
        return 0 if ok else 1
    finally:
        if runtime is not None:
            runtime.close()
        after_count, after_total = _total(ledger)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

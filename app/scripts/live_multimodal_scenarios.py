"""Test Chinese invoice image/PDF plus user text with the real enterprise flow."""

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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


APP = Path(__file__).resolve().parents[1]
UPGRADE = APP.parent
PROJECT = UPGRADE.parents[1]
sys.path.insert(0, str(APP / "src"))

from taxharness.enterprise.auth import SESSION_COOKIE, assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.multimodal import extract_document, prepare_document
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase, issue_session
from taxharness.enterprise.web import create_app
from taxharness.provider import _peak_window


NUMBER = "24312000000055556666"


def _total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _matches(fields: list[dict]) -> bool:
    for item in fields:
        number = str(item.get("invoice_number") or "").replace(" ", "")
        amount = str(item.get("amount") or "").replace("¥", "").replace("￥", "").replace(",", "").strip()
        try:
            if NUMBER == number and Decimal(amount) == Decimal("24.00"):
                return True
        except InvalidOperation:
            pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upload", choices=("image", "pdf"), default="image")
    args = parser.parse_args()
    if args.dry_run:
        print("中文票据 PDF 及截图 → DeepSeek 图文识别 → 企业工作项草稿 → 新对话确认保存")
        return 0
    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行多模态场景。")
    budget_path = UPGRADE / "budget.json"
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _total(ledger)
    run_root = UPGRADE / "experiments" / ("multimodal-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_ENTERPRISE_BUDGET"] = str(budget_path)
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)
    report = {"steps": []}
    try:
        sample = UPGRADE / "datasets/china_invoice_guard/07_行程发票.pdf"
        pdf = prepare_document(sample.name, sample.read_bytes())
        pdf_result = extract_document(pdf, user_text="请读取票号和金额；这张用于客户拜访，先不要保存。", user_id="vision-test", session_id="vision-pdf", turn_id="pdf",
                                      budget_ledger_path=ledger, budget_usd=budget["new_cumulative_ledger_limit"])
        pdf_ok = _matches(pdf_result["documents"])
        report["steps"].append({"name": "PDF加文字识别", "passed": pdf_ok, "documents": pdf_result["documents"]})
        print(f"PDF加文字识别：{'通过' if pdf_ok else '需处理'}", flush=True)
        if not pdf_ok:
            report["passed"] = False
            return 1

        database = run_root / "enterprise.sqlite3"
        with EnterpriseDatabase(database) as db:
            admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password="test-only-password-2026")
            service = EnterpriseService(db)
            company = service.create_company(actor=admin, name="示例科技有限公司", currency="CNY")
            accountant = create_user(db, username="accountant@example.test", display_name="会计", password="test-only-password-2026")
            assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "purpose", "remarks"])
            writer.writerow([NUMBER, "2026-07-08", "24.00", "CNY", "北京小桔科技有限公司", "客运服务费", "", ""])
            importer = ImportManager(db)
            preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="测试ERP", filename="invoice.csv", content=output.getvalue().encode("utf-8-sig"), mapping={})
            importer.commit(user=accountant, preview_id=preview["preview_id"])
            task = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-07")["work_item"]
            invoice_id = service.work_item(user=accountant, task_id=task["task_id"])["invoices"]["items"][0]["invoice_id"]
            token, csrf = issue_session(db, accountant["user_id"])
        app = create_app(database_path=database, agent_store_path=ledger)
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, token)
        upload_name, upload_content, upload_mime = (("发票截图.png", pdf["images"][0]["content"], "image/png")
            if args.upload == "image" else (sample.name, sample.read_bytes(), "application/pdf"))
        response = client.post(f"/api/work-items/{task['task_id']}/assistant/turn-with-files",
            data={"message": "这张发票实际用途是拜访客户，请先记成草稿，不要保存；同时告诉我票号和金额。", "session_id": ""},
            files=[("files", (upload_name, upload_content, upload_mime))], headers={"X-CSRF-Token": csrf})
        body = response.json()
        with EnterpriseDatabase(database) as db:
            detail = EnterpriseService(db).work_item(user=accountant, task_id=task["task_id"])
            drafts = [item for item in detail["drafts"] if item["status"] == "proposed"]
            actual = db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
            attachment_id = next(iter(body.get("attachments") or []), "")
            attachment = db.connection.execute("SELECT extracted_json FROM enterprise_assistant_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
            image_documents = json.loads(attachment["extracted_json"])["documents"] if attachment else []
        draft_ok = bool(drafts and drafts[0]["proposed_purpose"] in {"拜访客户", "客户拜访"}) and actual is None
        image_ok = _matches(image_documents)
        report["steps"].append({"name": ("图片" if args.upload == "image" else "PDF") + "加文字生成草稿", "passed": response.status_code == 200 and body.get("status") == "ok" and draft_ok and image_ok,
                                "reply": body.get("reply", ""), "documents": image_documents, "draft_purpose": drafts[0]["proposed_purpose"] if drafts else None, "invoice_unchanged": actual is None})
        print(f"{args.upload} 加文字生成草稿：{'通过' if report['steps'][-1]['passed'] else '需处理'}", flush=True)
        if not report["steps"][-1]["passed"]:
            report["passed"] = False
            return 1
        again = client.post(f"/api/work-items/{task['task_id']}/assistant/turn", json={"message": "继续这个工作项，把刚才那张票的用途草稿保存。"}, headers={"X-CSRF-Token": csrf})
        body2 = again.json()
        with EnterpriseDatabase(database) as db:
            detail = EnterpriseService(db).work_item(user=accountant, task_id=task["task_id"])
            item = detail["invoices"]["items"][0]
        saved = item["purpose"] in {"拜访客户", "客户拜访"} and item["task_status"] == "awaiting_review"
        report["steps"].append({"name": "新会话保存并待复核", "passed": again.status_code == 200 and body2.get("status") == "ok" and saved,
                                "reply": body2.get("reply", ""), "purpose": item["purpose"], "task_status": item["task_status"]})
        report["passed"] = all(step["passed"] for step in report["steps"])
        print(f"新会话保存并待复核：{'通过' if saved else '需处理'}", flush=True)
        return 0 if report["passed"] else 1
    finally:
        after_count, after_total = _total(ledger)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

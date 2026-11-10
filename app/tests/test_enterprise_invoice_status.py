from __future__ import annotations

import csv
import io

from taxharness.enterprise.agent import generate_reply_drafts
from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase
from taxharness.enterprise.work_projection import project_enterprise_work


def _csv(rows, *, include_status=True):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = ["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name"]
    writer.writerow(fields + (["invoice_status"] if include_status else []))
    writer.writerows(row if include_status else row[:-1] for row in rows)
    return output.getvalue().encode("utf-8-sig")


def test_source_void_is_excluded_and_old_draft_cannot_be_confirmed(tmp_path):
    with EnterpriseDatabase(tmp_path / "enterprise.sqlite3") as db:
        admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password="test-only-password-2026")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="样本企业", currency="CNY")
        accountant = create_user(db, username="accountant@example.test", display_name="会计", password="test-only-password-2026")
        requester = create_user(db, username="requester@example.test", display_name="经办人", password="test-only-password-2026")
        for person, role in ((accountant, "accountant"), (requester, "requester")):
            assign_membership(db, user_id=person["user_id"], company_id=company["company_id"], role=role)
        manager = ImportManager(db)
        rows = [["A1", "2026-10-08", "100.00", "CHF", "样本供应商", "软件服务", "issued"],
                ["A2", "2026-10-09", "75.00", "CAD", "样本供应商", "培训服务", "paid"]]

        def import_rows(data):
            preview = manager.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="sample.csv", content=data, mapping={})
            assert preview["issue_count"] == 0
            return manager.commit(user=accountant, preview_id=preview["preview_id"])

        import_rows(_csv(rows))
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-10")
        task_id = missing["work_item"]["task_id"]
        detail = service.work_item(user=accountant, task_id=task_id)
        by_source = {row["source_invoice_id"]: row["invoice_id"] for row in detail["invoices"]["items"]}
        request = service.create_request(user=accountant, task_id=task_id, respondent_user_id=requester["user_id"], invoice_ids=[by_source["A1"]], message="请说明用途。")
        response = service.submit_reply(user=requester, request_id=request["request_id"], reply_text="用于研发培训。")
        drafts = generate_reply_drafts(db, user=accountant, response_id=response["response_id"], model=lambda **_: {"items": [
            {"invoice_id": by_source["A1"], "purpose": "研发培训", "evidence_quote": "研发培训", "needs_clarification": False},
        ]})["drafts"]

        rows[0][-1] = "void"
        result = import_rows(_csv(rows))
        assert result["counts"]["void_in_source"] == 1
        summary = service.finance_summary(user=accountant, company_id=company["company_id"], period="2026-10")
        assert [(item["currency"], item["invoice_total"]) for item in summary["by_currency"]] == [("CAD", "75.00")]
        assert service.list_missing(user=accountant, company_id=company["company_id"], period="2026-10")["missing_count"] == 1
        projection = project_enterprise_work(db, user=accountant, task_id=task_id)
        assert projection["excluded"] == [by_source["A1"]]
        denied = service.confirm_drafts(user=accountant, draft_ids=[drafts[0]["draft_id"]])
        assert denied["items"][0]["status"] == "void_invoice"
        assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (by_source["A1"],)).fetchone()[0] is None

        import_rows(_csv(rows, include_status=False))
        assert service.finance_summary(user=accountant, company_id=company["company_id"], period="2026-10")["invoice_count"] == 1
        rows[0][-1] = "issued"
        import_rows(_csv(rows))
        assert service.finance_summary(user=accountant, company_id=company["company_id"], period="2026-10")["invoice_count"] == 2
        conflict = service.confirm_drafts(user=accountant, draft_ids=[drafts[0]["draft_id"]])
        assert conflict["items"][0]["status"] == "revision_conflict"


def test_red_flush_status_is_not_silently_treated_as_void():
    from taxharness.enterprise.imports import parse_import

    content = _csv([["A1", "2026-10-08", "100.00", "CNY", "供应商", "服务", "已冲红"]])
    parsed = parse_import(filename="sample.csv", content=content, mapping={}, default_currency="CNY")
    assert not parsed["rows"] and "无法识别发票状态" in parsed["issues"][0]["details"]

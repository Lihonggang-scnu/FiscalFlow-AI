import csv
import io

import pytest

from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase
from taxharness.enterprise.work_projection import project_enterprise_work


PASSWORD = "correct horse battery staple"


def _csv(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "purpose", "remarks", "unit_price", "quantity"])
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _setup(db, *, period: str, invoice_count: int):
    admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password=PASSWORD)
    service = EnterpriseService(db)
    company = service.create_company(actor=admin, name="投影测试企业", currency="GBP", require_independent_review=True)
    accountant = create_user(db, username="accountant@example.test", display_name="会计", password=PASSWORD)
    reviewer = create_user(db, username="reviewer@example.test", display_name="复核人", password=PASSWORD)
    requester = create_user(db, username="requester@example.test", display_name="经办人", password=PASSWORD)
    for account, role in ((accountant, "accountant"), (reviewer, "reviewer"), (requester, "requester")):
        assign_membership(db, user_id=account["user_id"], company_id=company["company_id"], role=role)

    rows = [
        [f"INV-{index:03d}", f"{period}-01", "10.00", "GBP", "测试商户", "服务", "", "", "10.0000", "1"]
        for index in range(invoice_count)
    ]
    importer = ImportManager(db)
    preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="invoices.csv", content=_csv(rows), mapping={})
    importer.commit(user=accountant, preview_id=preview["preview_id"])
    missing = service.list_missing(user=accountant, company_id=company["company_id"], period=period)
    assert missing["status"] == "ok"
    task_id = missing["work_item"]["task_id"]
    first_page = service.work_item(user=accountant, task_id=task_id, page=1, page_size=100)["invoices"]
    invoices = list(first_page["items"])
    for page in range(2, first_page["pages"] + 1):
        invoices.extend(service.work_item(user=accountant, task_id=task_id, page=page, page_size=100)["invoices"]["items"])
    return service, company, accountant, reviewer, requester, task_id, [invoice["invoice_id"] for invoice in invoices]


def _reply(service, accountant, requester, task_id, invoice_ids):
    request = service.create_request(
        user=accountant,
        task_id=task_id,
        respondent_user_id=requester["user_id"],
        invoice_ids=invoice_ids,
        message="请提供这些票据的用途。",
    )
    return service.submit_reply(user=requester, request_id=request["request_id"], reply_text="请按票据说明处理。")


def test_project_reads_every_page_and_supports_arbitrary_month(tmp_path):
    with EnterpriseDatabase(tmp_path / "pagination.sqlite3") as db:
        _, company, accountant, _, _, task_id, invoice_ids = _setup(db, period="2027-02", invoice_count=105)

        projection = project_enterprise_work(db, user=accountant, task_id=task_id)

        assert projection["task"]["company_id"] == company["company_id"]
        assert projection["task"]["period"] == "2027-02"
        assert projection["target_invoice_count"] == 105
        assert len(projection["tickets"]) == len(invoice_ids) == 105
        assert set(projection["needs_input"]) == set(invoice_ids)
        assert projection["awaiting_confirmation"] == []
        assert projection["invoice_record_kind"] == "current_database"
        assert all(ticket["actual_invoice"]["revision"] >= 1 for ticket in projection["tickets"])


def test_project_separates_draft_from_invoice_and_tracks_review_lifecycle(tmp_path):
    with EnterpriseDatabase(tmp_path / "lifecycle.sqlite3") as db:
        service, _, accountant, reviewer, requester, task_id, invoice_ids = _setup(db, period="2028-04", invoice_count=1)
        invoice_id = invoice_ids[0]
        reply = _reply(service, accountant, requester, task_id, [invoice_id])
        source_revision = db.connection.execute(
            "SELECT revision FROM invoices WHERE invoice_id=?", (invoice_id,)
        ).fetchone()[0]
        drafts = service.save_drafts(
            user=accountant,
            response_id=reply["response_id"],
            items=[{
                "invoice_id": invoice_id,
                "purpose": "客户培训资料",
                "remarks": None,
                "evidence_quote": "请按票据说明处理",
                "needs_clarification": False,
            }],
            source_revisions={invoice_id: source_revision},
        )
        draft = drafts[0]

        proposed = project_enterprise_work(db, user=accountant, task_id=task_id)
        assert proposed["awaiting_confirmation"] == [invoice_id]
        assert proposed["needs_input"] == []
        assert proposed["tickets"][0]["actual_invoice"]["purpose"] is None
        assert proposed["drafts"][0]["proposed_fields"]["purpose"] == "客户培训资料"
        assert proposed["drafts"][0]["draft_revision"] == 1
        assert proposed["drafts"][0]["source_invoice_revision"] == source_revision
        assert proposed["drafts"][0]["source_version_state"] == "current"

        confirmation = service.confirm_drafts(
            user=accountant,
            draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: 1},
        )
        assert confirmation["items"][0]["status"] == "committed"
        awaiting_review = project_enterprise_work(db, user=accountant, task_id=task_id)
        assert awaiting_review["awaiting_review"] == [invoice_id]
        assert awaiting_review["completed"] == []
        assert awaiting_review["tickets"][0]["actual_invoice"]["purpose"] == "客户培训资料"
        assert awaiting_review["drafts"][0]["status"] == "confirmed"
        assert awaiting_review["drafts"][0]["draft_revision"] == 2

        review = service.review_invoice(user=reviewer, task_id=task_id, invoice_id=invoice_id, action="approve", comment="已核对")
        assert review["invoice_status"] == "approved"
        completed = project_enterprise_work(db, user=reviewer, task_id=task_id)
        assert completed["completed"] == [invoice_id]
        assert completed["awaiting_review"] == []
        assert completed["next_actions"] == [{"action": "close_work_item", "task_id": task_id}]


def test_project_surfaces_ambiguous_unavailable_and_stale_drafts(tmp_path):
    with EnterpriseDatabase(tmp_path / "draft-states.sqlite3") as db:
        service, _, accountant, _, requester, task_id, invoice_ids = _setup(db, period="2029-01", invoice_count=4)
        ambiguous_id, second_id, unavailable_id, conflict_id = invoice_ids
        reply = _reply(service, accountant, requester, task_id, invoice_ids)
        revisions = {
            row["invoice_id"]: int(row["revision"])
            for row in db.connection.execute(
                "SELECT invoice_id,revision FROM invoices WHERE company_id=(SELECT company_id FROM enterprise_work_items WHERE task_id=?)",
                (task_id,),
            ).fetchall()
        }
        service.save_drafts(
            user=accountant,
            response_id=reply["response_id"],
            items=[
                {
                    "candidate_invoice_ids": [ambiguous_id, second_id],
                    "purpose": None,
                    "remarks": None,
                    "evidence_quote": "这两张需要再确认",
                    "needs_clarification": True,
                    "clarification_question": "请确认分别对应哪张票。",
                },
                {
                    "invoice_id": unavailable_id,
                    "purpose": None,
                    "remarks": None,
                    "evidence_quote": "无法提供",
                    "unavailable": True,
                },
                {
                    "invoice_id": conflict_id,
                    "purpose": "较早版本用途",
                    "remarks": None,
                    "evidence_quote": "较早版本用途",
                    "needs_clarification": False,
                },
            ],
            source_revisions={**revisions, conflict_id: revisions[conflict_id] - 1},
        )

        projection = project_enterprise_work(db, user=accountant, task_id=task_id)
        assert set(projection["waiting"]) == {ambiguous_id, second_id, unavailable_id, conflict_id}
        assert projection["waiting_reasons"][ambiguous_id] == "needs_clarification"
        assert projection["waiting_reasons"][unavailable_id] == "unavailable_needs_review_disposition"
        assert projection["waiting_reasons"][conflict_id] == "invoice_revision_conflict"
        ambiguous = next(draft for draft in projection["drafts"] if draft["needs_clarification"])
        assert ambiguous["candidate_invoice_ids"] == [ambiguous_id, second_id]
        assert ambiguous["evidence_quote"] == "这两张需要再确认"
        conflict = next(draft for draft in projection["drafts"] if draft["invoice_id"] == conflict_id)
        assert conflict["source_version_state"] == "stale"
        assert conflict["source_invoice_revision"] != conflict["current_invoice_revisions"][conflict_id]
        assert projection["awaiting_confirmation"] == []


def test_project_uses_enterprise_role_checks(tmp_path):
    with EnterpriseDatabase(tmp_path / "permissions.sqlite3") as db:
        _, _, _, _, requester, task_id, _ = _setup(db, period="2030-03", invoice_count=1)

        with pytest.raises(PermissionError):
            project_enterprise_work(db, user=requester, task_id=task_id)

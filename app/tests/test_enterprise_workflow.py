import csv
import io
import json

import pytest
from taxharness.rule_manager import RuleManager
from taxharness.enterprise.agent import generate_reply_drafts
from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService, invoice_key
from taxharness.enterprise.storage import EnterpriseDatabase


def _csv(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "purpose", "remarks", "unit_price", "quantity"])
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def test_offline_enterprise_reply_review_export_and_source_conflict(tmp_path):
    path = tmp_path / "enterprise.sqlite3"
    with EnterpriseDatabase(path) as db:
        admin = create_first_admin(db, username="admin@example.test", display_name="企业管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="测试机构客户", currency="GBP", require_independent_review=True)
        accountant = create_user(db, username="accountant@example.test", display_name="会计", password="correct horse battery staple")
        reviewer = create_user(db, username="reviewer@example.test", display_name="复核", password="correct horse battery staple")
        respondent = create_user(db, username="agent@example.test", display_name="经办人", password="correct horse battery staple")
        assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        assign_membership(db, user_id=reviewer["user_id"], company_id=company["company_id"], role="reviewer")
        assign_membership(db, user_id=respondent["user_id"], company_id=company["company_id"], role="requester")

        first = _csv([
            ["S-01", "2026-09-02", "120.00", "GBP", "Hotel A", "住宿", "", "", "60.0000", "2"],
            ["S-02", "2026-09-03", "125.00", "GBP", "Hotel B", "住宿", "", "", "125.000", "1"],
            ["S-03", "2026-09-04", "10.00", "CNY", "Courier", "快递", "", "", "10.0000", "1"],
            ["O-01", "2026-10-01", "8.00", "GBP", "Courier", "邮寄", "已填", "", "8.0000", "1"],
        ])
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="CloudERP", filename="first.csv", content=first, mapping={})
        result = importer.commit(user=accountant, preview_id=preview["preview_id"])
        assert result["counts"]["inserted"] == 4
        assert db.connection.execute("SELECT unit_price_text FROM enterprise_invoice_extra WHERE invoice_id=?", (invoice_key(company["company_id"], "CloudERP", "S-01"),)).fetchone()[0] == "60.0000"

        summary = service.finance_summary(user=accountant, company_id=company["company_id"], period="2026-09")
        assert summary["invoice_count"] == 3
        assert {row["currency"]: row["invoice_total"] for row in summary["by_currency"]} == {"CNY": "10.00", "GBP": "245.00"}
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        assert missing["missing_count"] == 3
        task_id = missing["work_item"]["task_id"]
        task = service.work_item(user=accountant, task_id=task_id)
        ids = [item["invoice_id"] for item in task["invoices"]["items"]]
        request = service.create_request(user=accountant, task_id=task_id, respondent_user_id=respondent["user_id"], invoice_ids=ids, message="请说明三张票的实际用途。")
        reply = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="两张住宿都是去上海拜访客户，快递是寄合同。")
        by_source = {db.connection.execute("SELECT source_invoice_id FROM enterprise_invoice_links WHERE invoice_id=?", (invoice_id,)).fetchone()[0]: invoice_id for invoice_id in ids}

        def model(*, system, input):
            return {"items": [
                {"invoice_id": by_source["S-01"], "purpose": "上海客户拜访", "evidence_quote": "两张住宿都是去上海拜访客户", "needs_clarification": False},
                {"invoice_id": by_source["S-02"], "purpose": "上海客户拜訪", "evidence_quote": "两张住宿都是去上海拜访客户", "needs_clarification": False},
                {"invoice_id": by_source["S-03"], "purpose": "寄送合同", "evidence_quote": "快递是寄合同", "needs_clarification": False},
            ]}

        drafts = generate_reply_drafts(db, user=accountant, response_id=reply["response_id"], model=model)
        assert drafts["draft_count"] == 3
        assert all(db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] is None for invoice_id in ids)
        first_draft = drafts["drafts"][0]
        revised = service.update_draft(user=accountant, draft_id=first_draft["draft_id"], invoice_id=first_draft["invoice_id"],
                                       purpose=first_draft["proposed_purpose"], remarks=first_draft["proposed_remarks"], expected_draft_revision=1)
        assert revised["draft_revision"] == 2
        stale = service.confirm_drafts(user=accountant, draft_ids=[first_draft["draft_id"]], expected_draft_revisions={first_draft["draft_id"]: 1})
        assert stale["items"][0]["status"] == "draft_conflict"
        assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (first_draft["invoice_id"],)).fetchone()[0] is None
        versions = {draft["draft_id"]: (2 if draft["draft_id"] == first_draft["draft_id"] else 1) for draft in drafts["drafts"]}
        confirmed = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"] for draft in drafts["drafts"]], expected_draft_revisions=versions)
        assert confirmed["confirmed"] == 3 and confirmed["failed"] == 0
        assert [db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] for invoice_id in ids] == ["上海客户拜访", "上海客户拜訪", "寄送合同"]
        assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant", active=False)
        denied_replay = service.confirm_drafts(user=accountant, draft_ids=[drafts["drafts"][0]["draft_id"]])
        assert denied_replay["items"][0]["status"] == "permission_denied" and "result" not in denied_replay["items"][0]
        assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant", active=True)
        for invoice_id in ids:
            review_token = next(row["review_token"] for row in service.work_item(user=reviewer, task_id=task_id)["invoices"]["items"] if row["invoice_id"] == invoice_id)
            reviewed = service.review_invoice(user=reviewer, task_id=task_id, invoice_id=invoice_id, action="approve", review_token=review_token, comment="票据说明与回复一致")
            assert reviewed["status"] == "ok"
        acknowledgement = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="收到，谢谢，内容没有变化。")
        no_new_suggestions = generate_reply_drafts(db, user=accountant, response_id=acknowledgement["response_id"], model=lambda **_: {"items": []})
        assert no_new_suggestions["draft_count"] == 0
        assert db.connection.execute("SELECT extraction_status,extraction_outcome FROM enterprise_responses WHERE response_id=?", (acknowledgement["response_id"],)).fetchone()[0] == "complete"
        closed = service.close_work_item(user=accountant, task_id=task_id)
        assert closed["status"] == "closed"
        filename, content = service.export_work_item(user=accountant, task_id=task_id)
        assert filename.endswith(".csv") and "上海客户拜访" in content and "GBP" in content

        late_reply = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="第一张住宿还补充了时间，其他票无需更改。")
        assert late_reply["late"] is True
        late_draft = generate_reply_drafts(db, user=accountant, response_id=late_reply["response_id"], model=lambda **_: {"items": [{"invoice_id": by_source["S-01"], "purpose": "上海客户拜访（补充时间）", "evidence_quote": "第一张住宿还补充了时间", "needs_clarification": False}]})
        assert late_draft["draft_count"] == 1
        blocked_late = service.confirm_drafts(user=accountant, draft_ids=[late_draft["drafts"][0]["draft_id"]], expected_draft_revisions={late_draft["drafts"][0]["draft_id"]: late_draft["drafts"][0]["draft_revision"]})
        assert blocked_late["items"][0]["status"] == "task_closed"
        followup = service.create_late_reply_followup(user=accountant, task_id=task_id, draft_ids=[late_draft["drafts"][0]["draft_id"]])
        assert followup["status"] == "created" and followup["task_id"] != task_id
        assert db.connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()[0] == "closed"

        changed = _csv([
            ["S-01", "2026-09-02", "150.00", "GBP", "Hotel A", "住宿", "来源更新值", "", "60.0000", "2"],
        ])
        preview2 = importer.preview(user=accountant, company_id=company["company_id"], source_system="CloudERP", filename="increment.csv", content=changed, mapping={})
        result2 = importer.commit(user=accountant, preview_id=preview2["preview_id"])
        assert result2["counts"]["conflicts"] == 1
        preview3 = importer.preview(user=accountant, company_id=company["company_id"], source_system="CloudERP", filename="increment-copy.csv", content=changed, mapping={})
        importer.commit(user=accountant, preview_id=preview3["preview_id"])
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_import_conflicts WHERE status='open'").fetchone()[0] == 1
        assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (by_source["S-01"],)).fetchone()[0] == "上海客户拜访"
        conflict = db.connection.execute("SELECT conflict_id FROM enterprise_import_conflicts WHERE status='open'").fetchone()[0]
        resolved = importer.resolve_conflict(user=accountant, conflict_id=conflict, resolution="keep_local")
        assert resolved["status"] == "resolved"
        _, historical_export = service.export_work_item(user=accountant, task_id=task_id)
        assert "120.00" in historical_export and "150.00" not in historical_export
        assert result2["source_change_followups"]

        different_source = _csv([["S-01", "2026-09-02", "120.00", "GBP", "Hotel A", "住宿", "另一个来源用途", "", "60.0000", "2"]])
        other_preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="OtherERP", filename="other.csv", content=different_source, mapping={})
        importer.commit(user=accountant, preview_id=other_preview["preview_id"])
        assert invoice_key(company["company_id"], "OtherERP", "S-01") != by_source["S-01"]
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_invoice_links WHERE company_id=? AND source_invoice_id='S-01'", (company["company_id"],)).fetchone()[0] == 2


def test_unavailable_reply_stays_pending_until_explicit_review_disposition(tmp_path):
    with EnterpriseDatabase(tmp_path / "unavailable.sqlite3") as db:
        admin = create_first_admin(db, username="admin2@example.test", display_name="管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="无法提供测试", currency="GBP")
        accountant = create_user(db, username="accountant2@example.test", display_name="会计", password="correct horse battery staple")
        reviewer = create_user(db, username="reviewer2@example.test", display_name="复核", password="correct horse battery staple")
        respondent = create_user(db, username="agent2@example.test", display_name="经办人", password="correct horse battery staple")
        for account, role in ((accountant, "accountant"), (reviewer, "reviewer"), (respondent, "requester")):
            assign_membership(db, user_id=account["user_id"], company_id=company["company_id"], role=role)
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="one.csv", content=_csv([["U-01", "2026-09-01", "25.00", "GBP", "Seller", "项目", "", "", "25.0000", "1"]]), mapping={})
        importer.commit(user=accountant, preview_id=preview["preview_id"])
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        invoice_id = missing["missing"][0]["invoice_id"]
        task_id = missing["work_item"]["task_id"]
        request = service.create_request(user=accountant, task_id=task_id, respondent_user_id=respondent["user_id"], invoice_ids=[invoice_id], message="请提供这张票的用途。")
        unknown_reply = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="我先不知道，还要核实。")
        unknown = generate_reply_drafts(db, user=accountant, response_id=unknown_reply["response_id"], model=lambda **_: {"items": [{"invoice_id": invoice_id, "purpose": None, "remarks": None, "evidence_quote": "先不知道，还要核实", "needs_clarification": False}]})
        assert unknown["drafts"][0]["status"] == "clarification" and not unknown["drafts"][0]["unavailable"]
        reply = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="这张票的真实用途无法提供。")
        draft_result = generate_reply_drafts(db, user=accountant, response_id=reply["response_id"], model=lambda **_: {"items": [{"invoice_id": invoice_id, "purpose": None, "remarks": None, "evidence_quote": "真实用途无法提供", "needs_clarification": False, "unavailable": True}]})
        draft = draft_result["drafts"][0]
        assert draft["status"] == "unavailable" and draft["unavailable"]
        assert db.connection.execute("SELECT superseded_by FROM enterprise_drafts WHERE draft_id=?", (unknown["drafts"][0]["draft_id"],)).fetchone()[0] == draft["draft_id"]
        assert db.connection.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (company["company_id"], invoice_id)).fetchone()[0] is None
        confirmation = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]], expected_draft_revisions={draft["draft_id"]: draft["draft_revision"]})
        assert confirmation["items"][0]["status"] == "unavailable"
        with pytest.raises(ValueError, match="待处理"):
            service.close_work_item(user=accountant, task_id=task_id)
        service.update_company_settings(actor=admin, company_id=company["company_id"], default_currency="GBP", require_independent_review=True, allowed_dispositions=["waive_missing"])
        review_token = next(row["review_token"] for row in service.work_item(user=reviewer, task_id=task_id)["invoices"]["items"] if row["invoice_id"] == invoice_id)
        service.review_invoice(user=reviewer, task_id=task_id, invoice_id=invoice_id, action="waive", review_token=review_token, comment="经办人确认无法提供，按机构授权保留空值")
        assert service.close_work_item(user=accountant, task_id=task_id)["status"] == "closed"
        _, exported = service.export_work_item(user=accountant, task_id=task_id)
        assert "waive_missing" in exported and "真实用途" not in exported


def test_rule_threshold_drives_tasks_and_disabled_rule_never_falls_back(tmp_path):
    with EnterpriseDatabase(tmp_path / "threshold.sqlite3") as db:
        admin = create_first_admin(db, username="admin3@example.test", display_name="管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="金额门槛测试", currency="GBP")
        accountant = create_user(db, username="accountant3@example.test", display_name="会计", password="correct horse battery staple")
        assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        rule_path = tmp_path / "rule.json"
        rule_path.write_text(json.dumps([{"rule_id": "R901", "version": "1.0", "name": "达到门槛时补用途", "kind": "invoice_purpose", "company_id": company["company_id"], "effective_from": "2026-09-01", "status": "active", "content": "金额达到1000 GBP时须有用途。", "condition": {"type": "amount_gte", "amount_cents": 100000}}]), encoding="utf-8")
        RuleManager(db).import_file(rule_path)
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="threshold.csv", content=_csv([["LOW", "2026-09-01", "999.99", "GBP", "S", "项目", "", "", "1.0000", "1"], ["EDGE", "2026-09-02", "1000.00", "GBP", "S", "项目", "", "", "1.0000", "1"]]), mapping={})
        importer.commit(user=accountant, preview_id=preview["preview_id"])
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        assert missing["missing_count"] == 1
        assert [item["source_invoice_id"] for item in missing["missing"]] == ["EDGE"]
        work = service.work_item(user=accountant, task_id=missing["work_item"]["task_id"])
        assert [item["source_invoice_id"] for item in work["invoices"]["items"]] == ["EDGE"]
        RuleManager(db).set_status(rule_id="R901", version="1.0", status="inactive")
        result = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        assert result["status"] == "rule_not_found" and result["rule_status"] == "cannot_determine"


def test_model_revision_is_snapshotted_before_inference_and_requester_revocation_is_live(tmp_path):
    with EnterpriseDatabase(tmp_path / "race.sqlite3") as db:
        admin = create_first_admin(db, username="admin4@example.test", display_name="管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="版本冲突测试", currency="GBP")
        accountant = create_user(db, username="accountant4@example.test", display_name="会计", password="correct horse battery staple")
        reviewer = create_user(db, username="reviewer4@example.test", display_name="复核", password="correct horse battery staple")
        respondent = create_user(db, username="agent4@example.test", display_name="经办人", password="correct horse battery staple")
        for account, role in ((accountant, "accountant"), (reviewer, "reviewer"), (respondent, "requester")):
            assign_membership(db, user_id=account["user_id"], company_id=company["company_id"], role=role)
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="race.csv", content=_csv([["RACE", "2026-09-01", "10.00", "GBP", "S", "项目", "", "", "1.0000", "1"]]), mapping={})
        importer.commit(user=accountant, preview_id=preview["preview_id"])
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        invoice_id = missing["missing"][0]["invoice_id"]
        request = service.create_request(user=accountant, task_id=missing["work_item"]["task_id"], respondent_user_id=respondent["user_id"], invoice_ids=[invoice_id], message="请说明用途。")
        reply = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="办公用品")

        def mutating_model(*, system, input):
            db.connection.execute("UPDATE invoices SET revision=revision+1 WHERE company_id=? AND invoice_id=?", (company["company_id"], invoice_id))
            return {"items": [{"invoice_id": invoice_id, "purpose": "办公用品", "evidence_quote": "办公用品", "needs_clarification": False}]}

        drafts = generate_reply_drafts(db, user=accountant, response_id=reply["response_id"], model=mutating_model)
        assert drafts["drafts"][0]["status"] == "conflict"
        assert service.confirm_drafts(user=accountant, draft_ids=[drafts["drafts"][0]["draft_id"]], expected_draft_revisions={drafts["drafts"][0]["draft_id"]: drafts["drafts"][0]["draft_revision"]})["items"][0]["status"] == "revision_conflict"
        assign_membership(db, user_id=respondent["user_id"], company_id=company["company_id"], role="requester", active=False)
        assert service.list_requests(user=respondent)["total"] == 0
        with pytest.raises(PermissionError):
            service.request_detail(user=respondent, request_id=request["request_id"])


def test_later_reply_supersedes_stale_unconfirmed_ticket_draft(tmp_path):
    with EnterpriseDatabase(tmp_path / "multiturn.sqlite3") as db:
        admin = create_first_admin(db, username="admin5@example.test", display_name="管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="多轮更正测试", currency="GBP")
        accountant = create_user(db, username="accountant5@example.test", display_name="会计", password="correct horse battery staple")
        reviewer = create_user(db, username="reviewer5@example.test", display_name="复核", password="correct horse battery staple")
        respondent = create_user(db, username="agent5@example.test", display_name="经办人", password="correct horse battery staple")
        for account, role in ((accountant, "accountant"), (reviewer, "reviewer"), (respondent, "requester")):
            assign_membership(db, user_id=account["user_id"], company_id=company["company_id"], role=role)
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="multi.csv", content=_csv([["H1", "2026-09-01", "10.00", "GBP", "S", "住宿", "", "", "1.0000", "1"], ["H2", "2026-09-02", "11.00", "GBP", "S", "住宿", "", "", "1.0000", "1"], ["K1", "2026-09-03", "12.00", "GBP", "S", "检测", "", "", "1.0000", "1"]]), mapping={})
        importer.commit(user=accountant, preview_id=preview["preview_id"])
        missing = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-09")
        targets = {item["source_invoice_id"]: item["invoice_id"] for item in missing["missing"]}
        request = service.create_request(user=accountant, task_id=missing["work_item"]["task_id"], respondent_user_id=respondent["user_id"], invoice_ids=list(targets.values()), message="请说明三张票的实际用途。")
        first = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="H1客户拜访，H2和K1先不知道。")
        initial = generate_reply_drafts(db, user=accountant, response_id=first["response_id"], model=lambda **_: {"items": [
            {"invoice_id": targets["H1"], "purpose": "客户拜访", "evidence_quote": "H1客户拜访", "needs_clarification": False},
            {"candidate_invoice_ids": [targets["H2"]], "purpose": None, "evidence_quote": "H2先不知道", "needs_clarification": True, "clarification_question": "H2用途是什么？"},
            {"candidate_invoice_ids": [targets["K1"]], "purpose": None, "evidence_quote": "K1先不知道", "needs_clarification": True, "clarification_question": "K1用途是什么？"},
        ]})
        second = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="H2也去拜访客户，K1寄合同。")
        generate_reply_drafts(db, user=accountant, response_id=second["response_id"], model=lambda **_: {"items": [
            {"invoice_id": targets["H1"], "purpose": "客户拜访", "evidence_quote": "H1客户拜访", "needs_clarification": False},
            {"invoice_id": targets["H2"], "purpose": "客户拜访", "evidence_quote": "H2也去拜访客户", "needs_clarification": False},
            {"invoice_id": targets["K1"], "purpose": "寄送合同", "evidence_quote": "K1寄合同", "needs_clarification": False},
        ]})
        third = service.submit_reply(user=respondent, request_id=request["request_id"], reply_text="更正：K1寄的是检测样品，不是合同。")
        generate_reply_drafts(db, user=accountant, response_id=third["response_id"], model=lambda **_: {"items": [{"invoice_id": targets["K1"], "purpose": "寄送检测样品", "evidence_quote": "K1寄的是检测样品，不是合同", "needs_clarification": False}]})
        active = db.connection.execute("SELECT invoice_id,proposed_purpose FROM enterprise_drafts WHERE task_id=? AND status='proposed' AND superseded_by IS NULL", (missing["work_item"]["task_id"],)).fetchall()
        assert {row["invoice_id"]: row["proposed_purpose"] for row in active} == {targets["H1"]: "客户拜访", targets["H2"]: "客户拜访", targets["K1"]: "寄送检测样品"}
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_drafts WHERE task_id=? AND superseded_by IS NOT NULL", (missing["work_item"]["task_id"],)).fetchone()[0] >= 4


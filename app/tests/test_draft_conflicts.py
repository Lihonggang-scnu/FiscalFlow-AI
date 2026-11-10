import pytest

from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.agent import generate_reply_drafts
from taxharness.enterprise.storage import EnterpriseDatabase
from test_enterprise_conversation import _prepare


def test_editing_remarks_does_not_rebase_an_old_purpose(tmp_path):
    path = tmp_path / "draft.sqlite3"
    company, accountant, _, _, work, invoice_id, draft = _prepare(path)
    with EnterpriseDatabase(path) as db:
        service = EnterpriseService(db)
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("来源系统已确认用途", company["company_id"], invoice_id))
        updated = service.update_draft(user=accountant, draft_id=draft["draft_id"], invoice_id=invoice_id,
            purpose=draft["proposed_purpose"], remarks="经办人已核实", expected_draft_revision=draft["draft_revision"],
            source_text="备注写经办人已核实", source_turn_id="turn-remark")
        result = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: updated["draft_revision"]})
        conflict = result["items"][0]
        assert conflict["status"] == "revision_conflict"
        assert conflict["field_conflicts"]["purpose"] == {"before": None, "current": "来源系统已确认用途", "proposed": "部门培训材料", "base_known": True}
        current = db.connection.execute("SELECT purpose,remarks FROM invoices WHERE company_id=? AND invoice_id=?",
                                        (company["company_id"], invoice_id)).fetchone()
        assert current["purpose"] == "来源系统已确认用途" and current["remarks"] is None
        detail = service.work_item(user=accountant, task_id=work["task_id"])
        current_draft = next(d for d in detail["drafts"] if d["draft_id"] == draft["draft_id"])
        assert current_draft["field_context"]["purpose"]["evidence_quote"] == "部门培训材料"
        assert current_draft["field_context"]["remarks"]["evidence_quote"] == "备注写经办人已核实"
        assert current_draft["field_conflicts"]["purpose"]["current"] == "来源系统已确认用途"
        with pytest.raises(ValueError, match="又发生了变化"):
            service.update_draft(user=accountant, draft_id=draft["draft_id"], invoice_id=invoice_id,
                purpose="部门培训材料", remarks="经办人已核实", expected_draft_revision=updated["draft_revision"],
                rebase_fields={"purpose": 1})
        rechecked = service.update_draft(user=accountant, draft_id=draft["draft_id"], invoice_id=invoice_id,
            purpose="部门培训材料", remarks="经办人已核实", expected_draft_revision=updated["draft_revision"],
            rebase_fields={"purpose": current_draft["field_conflicts"]["purpose"]["current_revision"]})
        saved = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: rechecked["draft_revision"]})
        assert saved["items"][0]["status"] == "committed"
        assert saved["items"][0]["after"]["purpose"] == "部门培训材料"


def test_unrelated_field_changes_merge_and_old_receipt_shows_current_value(tmp_path):
    path = tmp_path / "merge.sqlite3"
    company, accountant, _, _, _, invoice_id, draft = _prepare(path)
    with EnterpriseDatabase(path) as db:
        service = EnterpriseService(db)
        db.connection.execute("UPDATE invoices SET remarks=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("财务已留备注", company["company_id"], invoice_id))
        missing = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]])
        assert missing["items"][0]["status"] == "missing_draft_revision"
        saved = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: draft["draft_revision"]})
        assert saved["items"][0]["status"] == "committed"
        assert saved["items"][0]["after"]["remarks"] == "财务已留备注"
        db.connection.execute("UPDATE enterprise_invoice_extra SET source_status='void' WHERE company_id=? AND invoice_id=?", (company["company_id"], invoice_id))
        void_replay = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: draft["draft_revision"]})
        assert void_replay["items"][0]["status"] == "duplicate_changed" and void_replay["items"][0]["current"]["source_status"] == "void"
        db.connection.execute("UPDATE enterprise_invoice_extra SET source_status='active' WHERE company_id=? AND invoice_id=?", (company["company_id"], invoice_id))
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("后来更正的用途", company["company_id"], invoice_id))
        replay = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: draft["draft_revision"]})
        assert replay["items"][0]["status"] == "duplicate_changed"
        assert replay["items"][0]["current"]["purpose"] == "后来更正的用途"
        assert replay["items"][0]["result"]["after"]["purpose"] == "部门培训材料"


def test_draft_correction_keeps_old_and_new_evidence(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    _, accountant, _, _, _, invoice_id, draft = _prepare(path)
    with EnterpriseDatabase(path) as db:
        service = EnterpriseService(db)
        changed = service.update_draft(user=accountant, draft_id=draft["draft_id"], invoice_id=invoice_id,
            purpose="客户培训材料", remarks=None, expected_draft_revision=draft["draft_revision"],
            source_text="更正，是客户培训材料", source_turn_id="turn-correct")
        field = service._decode_draft(changed)["field_context"]["purpose"]
        assert field["evidence_quote"] == "更正，是客户培训材料" and field["source_turn_id"] == "turn-correct"
        assert field["history"][0]["evidence_quote"] == "部门培训材料"


def test_late_extraction_from_an_older_reply_cannot_replace_a_newer_correction(tmp_path):
    path = tmp_path / "late-extraction.sqlite3"
    _, accountant, _, requester, work, invoice_id, old_draft = _prepare(path)
    with EnterpriseDatabase(path) as db:
        service = EnterpriseService(db)
        new_reply = service.submit_reply(user=requester, request_id=old_draft["request_id"], reply_text="更正：是客户培训材料。")
        newer = generate_reply_drafts(db, user=accountant, response_id=new_reply["response_id"], model=lambda **_: {"items": [
            {"invoice_id": invoice_id, "purpose": "客户培训材料", "evidence_quote": "更正：是客户培训材料", "needs_clarification": False}]})["drafts"][0]
        old_late = service.save_drafts(user=accountant, response_id=old_draft["response_id"], items=[
            {"invoice_id": invoice_id, "purpose": "部门培训材料", "evidence_quote": "部门培训材料"}])[0]
        assert old_late["status"] == "superseded" and old_late["superseded_by"] == newer["draft_id"]
        active = [d for d in service.work_item(user=accountant, task_id=work["task_id"])["drafts"] if d["status"] == "proposed"]
        assert len(active) == 1 and active[0]["proposed_purpose"] == "客户培训材料"

import json

import pytest

from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase
from test_enterprise_conversation import _prepare


def test_reviewer_approves_only_the_content_they_saw(tmp_path):
    path = tmp_path / "review.sqlite3"
    company, accountant, reviewer, _, work, invoice_id, draft = _prepare(path)
    with EnterpriseDatabase(path) as db:
        service = EnterpriseService(db)
        confirmed = service.confirm_drafts(user=accountant, draft_ids=[draft["draft_id"]],
            expected_draft_revisions={draft["draft_id"]: draft["draft_revision"]})
        assert confirmed["items"][0]["status"] == "committed"
        old_view = service.work_item(user=reviewer, task_id=work["task_id"])["invoices"]["items"][0]
        assert old_view["purpose"] == "部门培训材料"
        assert old_view["review_changes"]["purpose"] == {"before": None, "after": "部门培训材料"}
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("客户培训资料", company["company_id"], invoice_id))
        conflict = service.review_invoice(user=reviewer, task_id=work["task_id"], invoice_id=invoice_id,
            action="approve", review_token=old_view["review_token"])
        assert conflict["status"] == "review_conflict"
        assert conflict["current"]["purpose"] == "客户培训资料"
        assert db.connection.execute("SELECT COUNT(*) FROM enterprise_reviews").fetchone()[0] == 0
        with pytest.raises(ValueError, match="先查看"):
            service.review_invoice(user=reviewer, task_id=work["task_id"], invoice_id=invoice_id, action="approve", review_token="")
        new_view = service.work_item(user=reviewer, task_id=work["task_id"])["invoices"]["items"][0]
        approved = service.review_invoice(user=reviewer, task_id=work["task_id"], invoice_id=invoice_id,
            action="approve", review_token=new_view["review_token"])
        assert approved["status"] == "ok"
        recorded = db.connection.execute("SELECT review_token,reviewed_snapshot_json FROM enterprise_reviews").fetchone()
        assert recorded["review_token"] == new_view["review_token"]
        assert json.loads(recorded["reviewed_snapshot_json"])["purpose"] == "客户培训资料"

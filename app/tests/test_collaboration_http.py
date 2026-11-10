import json

from fastapi.testclient import TestClient

from taxharness.enterprise.auth import SESSION_COOKIE
from taxharness.enterprise.storage import EnterpriseDatabase, issue_session
from taxharness.enterprise.web import create_app
from test_enterprise_conversation import _prepare


def test_http_review_rejects_an_outdated_page_and_accepts_a_refreshed_one(tmp_path):
    database = tmp_path / "enterprise.sqlite3"
    company, accountant, reviewer, _, work, invoice_id, draft = _prepare(database)
    with EnterpriseDatabase(database) as db:
        accountant_cookie, accountant_csrf = issue_session(db, accountant["user_id"])
        reviewer_cookie, reviewer_csrf = issue_session(db, reviewer["user_id"])
    app = create_app(database_path=database, agent_store_path=tmp_path / "ledger.sqlite3")
    accountant_client, reviewer_client = TestClient(app), TestClient(app)
    accountant_client.cookies.set(SESSION_COOKIE, accountant_cookie)
    reviewer_client.cookies.set(SESSION_COOKIE, reviewer_cookie)
    saved = accountant_client.post("/api/drafts/confirm", headers={"X-CSRF-Token": accountant_csrf}, json={
        "draft_ids": [draft["draft_id"]], "expected_draft_revisions": {draft["draft_id"]: draft["draft_revision"]}})
    assert saved.status_code == 200 and saved.json()["confirmed"] == 1
    url = f"/api/work-items/{work['task_id']}"
    old = reviewer_client.get(url).json()["invoices"]["items"][0]
    assert old["purpose"] == "部门培训材料" and old["review_token"]
    with EnterpriseDatabase(database) as db:
        db.connection.execute("UPDATE invoices SET purpose=?,remarks=?,revision=revision+1 WHERE company_id=? AND invoice_id=?",
                              ("客户培训资料", "由会计补充", company["company_id"], invoice_id))
    rejected = reviewer_client.post(url + "/review", headers={"X-CSRF-Token": reviewer_csrf}, json={
        "invoice_id": invoice_id, "action": "approve", "review_token": old["review_token"]})
    assert rejected.status_code == 200 and rejected.json()["status"] == "review_conflict"
    assert rejected.json()["current"]["purpose"] == "客户培训资料"
    new = reviewer_client.get(url).json()["invoices"]["items"][0]
    accepted = reviewer_client.post(url + "/review", headers={"X-CSRF-Token": reviewer_csrf}, json={
        "invoice_id": invoice_id, "action": "approve", "review_token": new["review_token"]})
    assert accepted.status_code == 200 and accepted.json()["status"] == "ok"
    with EnterpriseDatabase(database) as db:
        row = db.connection.execute("SELECT reviewed_snapshot_json FROM enterprise_reviews WHERE review_id=?", (accepted.json()["review_id"],)).fetchone()
        snapshot = json.loads(row[0])
        assert snapshot["purpose"] == "客户培训资料" and snapshot["remarks"] == "由会计补充"

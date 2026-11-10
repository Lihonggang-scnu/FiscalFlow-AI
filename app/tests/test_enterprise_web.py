from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from taxharness.enterprise.web import create_app


def test_http_setup_cookie_csrf_roles_and_rule_ownership(tmp_path):
    app = create_app(database_path=tmp_path / "enterprise.sqlite3", agent_store_path=tmp_path / "ledger.sqlite3", model=lambda **_: {"items": []})
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    setup = client.post("/api/setup/admin", json={"username": "webadmin@example.test", "display_name": "管理员", "password": "correct horse battery staple"})
    assert setup.status_code == 200
    assert setup.cookies.get("tax_enterprise_session")
    csrf = setup.json()["csrf_token"]
    first = client.post("/api/companies", json={"name": "甲公司", "default_currency": "GBP"}, headers={"X-CSRF-Token": csrf})
    second = client.post("/api/companies", json={"name": "乙公司", "default_currency": "CNY"}, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 200 and second.status_code == 200
    company_a, company_b = first.json(), second.json()
    scan = client.post("/api/invoices/import/columns", data={"company_id": company_a["company_id"]}, files={"file": ("sample.csv", b"invoice_id,issue_date,amount,currency\nA1,2026-09-01,10.00,GBP\n", "text/csv")}, headers={"X-CSRF-Token": csrf})
    assert scan.status_code == 200
    assert scan.json()["suggested_mapping"]["source_invoice_id"] == "invoice_id"
    rule = {"rule_id": "R901", "version": "1.0", "name": "乙公司规则", "kind": "invoice_purpose", "effective_from": "2026-09-01", "status": "active", "content": "所有票据需有用途。", "condition": {"type": "all"}}
    created = client.post("/api/rules", json={"company_id": company_b["company_id"], "rule": rule}, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200
    cross_company = client.put(f"/api/rules/R901/1.0/status?company_id={company_a['company_id']}", json={"status": "inactive"}, headers={"X-CSRF-Token": csrf})
    assert cross_company.status_code == 403
    no_csrf = client.post("/api/companies", json={"name": "未校验公司", "default_currency": "GBP"})
    assert no_csrf.status_code == 403

    account = client.post("/api/admin/users", json={"username": "webaccount@example.test", "display_name": "会计", "password": "correct horse battery staple"}, headers={"X-CSRF-Token": csrf})
    assert account.status_code == 200
    assigned = client.put("/api/admin/membership", json={"user_id": account.json()["user_id"], "company_id": company_a["company_id"], "role": "accountant"}, headers={"X-CSRF-Token": csrf})
    assert assigned.status_code == 200
    client.post("/api/logout", json={}, headers={"X-CSRF-Token": csrf})
    logged_in = client.post("/api/login", json={"username": "webaccount@example.test", "password": "correct horse battery staple"})
    assert logged_in.status_code == 200
    current = client.get("/api/session")
    assert current.status_code == 200
    assert current.json()["user"]["username"] == "webaccount@example.test"
    assert [item["company_id"] for item in current.json()["companies"]] == [company_a["company_id"]]
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/").status_code == 200


def test_http_parallel_read_dependencies_cross_worker_threads(tmp_path):
    app = create_app(database_path=tmp_path / "parallel.sqlite3", agent_store_path=tmp_path / "ledger.sqlite3")
    client = TestClient(app)
    setup = client.post("/api/setup/admin", json={"username": "paralleladmin@example.test", "display_name": "管理员", "password": "correct horse battery staple"})
    assert setup.status_code == 200
    company = client.post("/api/companies", json={"name": "并行测试企业", "default_currency": "GBP"}, headers={"X-CSRF-Token": setup.json()["csrf_token"]}).json()
    company_id = company["company_id"]
    urls = [
        "/api/health",
        f"/api/work-items?company_id={company_id}&period=2026-09",
        f"/api/finance/summary?company_id={company_id}&period=2026-09",
        f"/api/missing?company_id={company_id}&period=2026-09",
        f"/api/invoices?company_id={company_id}&period=2026-09&page=1&page_size=10",
    ] * 3
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(client.get, urls))
    assert [response.status_code for response in responses] == [200] * len(urls)


from __future__ import annotations

from pathlib import Path

from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager, parse_import
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase
from taxharness.enterprise.document_ingest import read_structured_invoice


SAMPLES = Path(__file__).resolve().parents[2] / "datasets" / "china_mof_redacted"
PASSWORD = "test-only-password-2026"


def _bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def _parsed_source(name: str) -> dict[str, str]:
    parsed = parse_import(filename=name, content=_bytes(name), mapping={}, default_currency="CNY")
    assert parsed["issues"] == []
    assert len(parsed["rows"]) == 1
    return parsed["rows"][0]["source"]


def test_mof_invoice_amount_issue_date_and_accounting_period_are_separate():
    vat_general_issuer = _parsed_source("MOF-CN-06.xml")
    vat_general_receiver = _parsed_source("MOF-CN-07.xml")
    vat_special_issuer = _parsed_source("MOF-CN-08.xml")
    vat_special_receiver = _parsed_source("MOF-CN-09.xml")

    assert vat_general_issuer["issue_date"] == "2020-03-28"
    assert vat_general_issuer["amount"] == "266.00"
    assert vat_general_issuer["currency"] == "CNY"
    assert vat_general_issuer["net_amount"] == "235.40"
    assert vat_general_issuer["tax_amount"] == "30.60"
    assert vat_general_receiver["issue_date"] == "2020-03-28"
    assert vat_general_receiver["accounting_period"] == "2020-08"
    assert vat_general_receiver["amount"] == "266.00"
    assert vat_general_receiver["checked_status"] == "true"
    assert vat_general_receiver["booked_status"] == "true"

    assert vat_special_issuer["issue_date"] == "2021-01-21"
    assert vat_special_issuer["amount"] == "98000.00"
    assert vat_special_issuer["net_amount"] == "97029.70"
    assert vat_special_issuer["tax_amount"] == "970.30"
    assert vat_special_receiver["accounting_period"] == "2021-01"
    assert vat_special_receiver["paid_status"] == "true"
    assert vat_special_receiver["checked_status"] == "true"
    assert vat_special_receiver["booked_status"] == "true"


def test_mof_invoice_type_is_preserved_in_the_parsed_record():
    ordinary = _parsed_source("MOF-CN-06.xml")
    special = _parsed_source("MOF-CN-08.xml")

    assert ordinary.get("invoice_type") == "增值税电子普通发票", f"actual parsed source fields: {ordinary!r}"
    assert special.get("invoice_type") == "增值税电子专用发票", f"actual parsed source fields: {special!r}"


def test_same_mof_invoice_keeps_one_identity_across_issuer_and_receiver_views():
    issuer = _parsed_source("MOF-CN-06.xml")
    receiver = _parsed_source("MOF-CN-07.xml")

    assert issuer["invoice_number"] == receiver["invoice_number"]
    assert issuer["source_invoice_id"] == receiver["source_invoice_id"], (
        f"issuer identity={issuer['source_invoice_id']!r}; "
        f"receiver identity={receiver['source_invoice_id']!r}; "
        f"issuer code/number={issuer['invoice_code']!r}/{issuer['invoice_number']!r}; "
        f"receiver code/number={receiver['invoice_code']!r}/{receiver['invoice_number']!r}"
    )


def test_exact_mof_reimport_is_deduplicated_per_company(tmp_path):
    with EnterpriseDatabase(tmp_path / "mof-dedup.sqlite3") as database:
        admin = create_first_admin(database, username="admin@mof.test", display_name="管理员", password=PASSWORD)
        service = EnterpriseService(database)
        company_a = service.create_company(actor=admin, name="样例甲企业", currency="CNY")
        company_b = service.create_company(actor=admin, name="样例乙企业", currency="CNY")
        accountant = create_user(database, username="accountant@mof.test", display_name="会计", password=PASSWORD)
        for company in (company_a, company_b):
            assign_membership(database, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        manager = ImportManager(database)
        content = _bytes("MOF-CN-08.xml")

        def ingest(company, source_system):
            preview = manager.preview(
                user=accountant,
                company_id=company["company_id"],
                source_system=source_system,
                filename="MOF-CN-08.xml",
                content=content,
                mapping={},
            )
            assert preview["issue_count"] == 0
            return manager.commit(user=accountant, preview_id=preview["preview_id"])

        original = ingest(company_a, "MOF public sample")
        resend = ingest(company_a, "mail attachment")
        other_company = ingest(company_b, "mail attachment")

        assert original["counts"]["inserted"] == 1
        assert resend["counts"]["duplicate_documents"] == 1
        assert resend["counts"]["inserted"] == 0
        assert other_company["counts"]["inserted"] == 1
        assert database.connection.execute(
            "SELECT COUNT(*) FROM enterprise_document_identities WHERE invoice_number='TEST-DOCUMENT-REF'"
        ).fetchone()[0] == 2


def test_mof_accounting_records_and_bank_documents_are_not_imported_as_invoices():
    for name in ("MOF-CN-01.xml", "MOF-CN-02.xml"):
        records, issues = read_structured_invoice(name, _bytes(name))
        assert records == []
        assert len(issues) == 1
        assert issues[0]["issue_code"] == "invalid_document"

    for name in ("MOF-CN-03.xml", "MOF-CN-04.xml", "MOF-CN-05.xml"):
        records, issues = read_structured_invoice(name, _bytes(name))
        assert records == []
        assert len(issues) == 1
        assert issues[0]["issue_code"] == "invalid_document"
        assert "银行电子回单或对账单" in issues[0]["details"]

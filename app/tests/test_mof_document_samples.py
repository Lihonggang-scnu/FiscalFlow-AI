from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

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


def _invoice_xml_with_identity(name: str, *, unique_code: str, invoice_code: str = "", invoice_number: str = "") -> bytes:
    root = ElementTree.fromstring(_bytes(name))
    replacements = {"UniqueCodeOfInvoice": unique_code}
    if invoice_code:
        replacements["CodeOfInvoice"] = invoice_code
    if invoice_number:
        replacements["NumberOfInvoice"] = invoice_number
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in replacements:
            element.text = replacements[tag]
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


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


def test_redacted_mof_views_expose_the_shared_reference_as_weak():
    issuer = _parsed_source("MOF-CN-06.xml")
    receiver = _parsed_source("MOF-CN-07.xml")

    assert issuer["invoice_unique_code"] == receiver["invoice_unique_code"] == "TEST-DOCUMENT-REF"
    assert issuer["identity_status"] == receiver["identity_status"] == "needs_review"
    assert issuer["source_invoice_id"] != receiver["source_invoice_id"]
    assert "@attachment:" not in issuer["source_invoice_id"] + receiver["source_invoice_id"]


def test_mof_weak_redacted_references_are_held_for_review_without_merging(tmp_path):
    with EnterpriseDatabase(tmp_path / "mof-identity-review.sqlite3") as database:
        admin = create_first_admin(database, username="admin@mof-review.test", display_name="管理员", password=PASSWORD)
        service = EnterpriseService(database)
        company = service.create_company(actor=admin, name="样例企业", currency="CNY")
        accountant = create_user(database, username="accountant@mof-review.test", display_name="会计", password=PASSWORD)
        assign_membership(database, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        manager = ImportManager(database)

        def ingest(name: str, source_system: str) -> dict:
            preview = manager.preview(user=accountant, company_id=company["company_id"], source_system=source_system,
                                      filename=name, content=_bytes(name), mapping={})
            return manager.commit(user=accountant, preview_id=preview["preview_id"])

        original = ingest("MOF-CN-06.xml", "MOF public sample")
        receiver = ingest("MOF-CN-07.xml", "MOF public sample")
        different_invoice = ingest("MOF-CN-08.xml", "MOF public sample")

        assert original["counts"]["inserted"] == 1
        assert receiver["counts"]["identity_reviews"] == 1
        assert receiver["issues"][0]["issue_code"] == "identity_review"
        assert different_invoice["counts"]["identity_reviews"] == 1
        assert different_invoice["counts"]["duplicate_documents"] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM invoices WHERE company_id=?", (company["company_id"],)).fetchone()[0] == 1


def test_mof_strong_unique_code_matches_issuer_and_receiver_views(tmp_path):
    unique_code = "24312000000098765432"
    issuer_content = _invoice_xml_with_identity("MOF-CN-06.xml", unique_code=unique_code,
                                                invoice_code="110000000001", invoice_number="01234567")
    receiver_content = _invoice_xml_with_identity("MOF-CN-07.xml", unique_code=unique_code)
    issuer = parse_import(filename="issuer.xml", content=issuer_content, mapping={}, default_currency="CNY")["rows"][0]["source"]
    receiver = parse_import(filename="receiver.xml", content=receiver_content, mapping={}, default_currency="CNY")["rows"][0]["source"]
    assert issuer["identity_status"] == receiver["identity_status"] == "strong"
    assert issuer["source_invoice_id"] == receiver["source_invoice_id"] == unique_code

    with EnterpriseDatabase(tmp_path / "mof-strong-identity.sqlite3") as database:
        admin = create_first_admin(database, username="admin@mof-strong.test", display_name="管理员", password=PASSWORD)
        service = EnterpriseService(database)
        company = service.create_company(actor=admin, name="样例企业", currency="CNY")
        accountant = create_user(database, username="accountant@mof-strong.test", display_name="会计", password=PASSWORD)
        assign_membership(database, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        manager = ImportManager(database)

        def ingest(source_system: str, filename: str, content: bytes) -> dict:
            preview = manager.preview(user=accountant, company_id=company["company_id"], source_system=source_system,
                                      filename=filename, content=content, mapping={})
            return manager.commit(user=accountant, preview_id=preview["preview_id"])

        assert ingest("issuer", "issuer.xml", issuer_content)["counts"]["inserted"] == 1
        matched = ingest("receiver", "receiver.xml", receiver_content)
        assert matched["counts"]["duplicate_documents"] == 1
        assert matched["counts"]["inserted"] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM invoices WHERE company_id=?", (company["company_id"],)).fetchone()[0] == 1


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
        ).fetchone()[0] == 0


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

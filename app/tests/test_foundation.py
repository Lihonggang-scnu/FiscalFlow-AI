from __future__ import annotations

from pathlib import Path

import pytest

from taxharness.errors import InvalidInputError, PermissionDeniedError
from taxharness.cli import _emit
from taxharness.importer import _date_text, file_sha256
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database


PROJECT = Path(__file__).resolve().parents[1]
SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "taxharness.sqlite3"
    before = file_sha256(SOURCE_XLSX)
    result = initialize_database(path, SOURCE_XLSX, RULES_DOC)
    assert result["counts"] == {
        "companies": 2,
        "users": 2,
        "user_company_permissions": 4,
        "invoices": 40,
        "bank_records": 48,
        "rules": 5,
    }
    assert file_sha256(SOURCE_XLSX) == before
    with TaxDatabase(path) as db:
        yield db


def test_official_amounts_and_no_record_status(database):
    service = TaxService(database)
    expected = {
        ("C001", "2026-08"): ("9046.35", "5720.99", "14850.75"),
        ("C001", "2026-09"): ("9622.06", "7180.74", "13000.50"),
        ("C002", "2026-08"): ("13667.05", "9341.10", "18800.25"),
        ("C002", "2026-09"): ("12667.24", "8087.24", "13700.75"),
    }
    for (company_id, month), (invoice_total, expense, income) in expected.items():
        invoice = service.query_invoice_summary(user_id="U001", company_id=company_id, month=month)["data"]
        bank = service.query_bank_summary(user_id="U001", company_id=company_id, month=month)["data"]
        assert invoice["invoice_total"] == invoice_total
        assert bank["expense"] == expense
        assert bank["income"] == income
        assert invoice["invoice_count"] == 10
        assert bank["record_count"] == 12
    empty = service.query_invoice_summary(user_id="U001", company_id="C001", month="2026-07")["data"]
    assert empty["no_records"] is True and empty["invoice_total"] == "0.00"
    assert service.query_bank_summary(user_id="U001", company_id="C001", month="2026-07")["data"]["no_records"] is True


def test_user_company_permissions_are_distinct(database):
    service = TaxService(database)
    assert service.query_invoice_summary(user_id="U002", company_id="C001", month="2026-09")["status"] == "ok"
    with pytest.raises(PermissionDeniedError):
        service.query_invoice_summary(user_id="U002", company_id="C002", month="2026-09")
    with pytest.raises(PermissionDeniedError):
        service.update_invoice(
            user_id="U002",
            company_id="C001",
            invoice_id="F002",
            changes={"purpose": "不应写入"},
            expected_revision=1,
            idempotency_key="U002:C001:invoice:F002:denied",
        )


def test_active_rule_selection_and_missing_purposes(database):
    service = TaxService(database)
    old = database.connection.execute("SELECT status FROM rules WHERE rule_id='R002' AND version='1.0'").fetchone()
    assert old["status"] == "inactive"
    c001 = service.list_missing_purposes(user_id="U001", company_id="C001")["data"]
    assert c001["rule"]["rule_id"] == "R002"
    assert c001["rule"]["version"] == "2.0"
    assert {row["invoice_id"] for row in c001["missing"]} == {"F002", "F003", "F004", "F006", "F009"}
    c002 = service.list_missing_purposes(user_id="U001", company_id="C002")["data"]
    assert c002["rule"]["rule_id"] == "R003"
    assert c002["rule"]["version"] == "1.0"
    assert {row["invoice_id"] for row in c002["missing"]} == {"F002", "F005", "F006"}
    assert service.list_missing_purposes(user_id="U001", company_id="C001", month="2026-08")["status"] == "out_of_scope"


def test_update_rechecks_and_scopes_idempotency(database):
    service = TaxService(database)
    key = "U001:C001:invoice:F002:purpose-1"
    committed = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F002",
        changes={"purpose": "办公资料补录"}, expected_revision=1, idempotency_key=key,
    )
    assert committed["status"] == "committed"
    assert committed["committed"] is True and committed["rechecked"] is True
    assert committed["after"]["purpose"] == "办公资料补录"
    duplicate = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F002",
        changes={"purpose": "办公资料补录"}, expected_revision=1, idempotency_key=key,
    )
    assert duplicate["status"] == "duplicate" and duplicate["replayed"] is True
    conflict = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F002",
        changes={"purpose": "另一内容"}, expected_revision=1, idempotency_key=key,
    )
    assert conflict["status"] == "idempotency_conflict"
    stale = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F002",
        changes={"remarks": "补充说明"}, expected_revision=1,
        idempotency_key="U001:C001:invoice:F002:purpose-2",
    )
    assert stale["status"] == "revision_conflict" and stale["current_revision"] == 2
    assert service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F002",
        changes={"seller_name": "禁止字段"}, expected_revision=2,
        idempotency_key="U001:C001:invoice:F002:bad-field",
    )["status"] == "invalid_input"
    with TaxDatabase(database.path) as reopened:
        row = reopened.connection.execute(
            "SELECT purpose,remarks,revision FROM invoices WHERE company_id='C001' AND invoice_id='F002'"
        ).fetchone()
        assert dict(row) == {"purpose": "办公资料补录", "remarks": None, "revision": 2}
        assert reopened.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2
        audit = reopened.connection.execute("SELECT payload_json FROM audit_events ORDER BY event_id").fetchall()
        assert '"expected_revision": 1' in audit[0][0]


def test_source_metadata_and_safe_audit_shape(database):
    assert database.get_meta("source_xlsx_sha256")
    assert database.get_meta("rules_doc_sha256")
    audit = database.connection.execute("SELECT payload_json,result_json FROM audit_events").fetchall()
    assert audit == []


def test_whitespace_save_is_no_change_but_explicit_clear_existing_value_commits(database):
    service = TaxService(database)
    blank = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F003", changes={"purpose": "   "},
        expected_revision=1, idempotency_key="U001:C001:invoice:F003:blank-noop",
    )
    assert blank["status"] == "no_change" and blank["current"]["purpose"] is None
    row = database.connection.execute("SELECT purpose,revision FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()
    assert row["purpose"] is None and row["revision"] == 1
    padded = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F003", changes={"purpose": " 快递资料 "},
        expected_revision=1, idempotency_key="U001:C001:invoice:F003:padded",
    )
    assert padded["status"] == "committed" and padded["after"]["purpose"] == "快递资料"
    cleared = service.update_invoice(
        user_id="U001", company_id="C001", invoice_id="F003", changes={"purpose": ""},
        expected_revision=2, idempotency_key="U001:C001:invoice:F003:clear",
    )
    assert cleared["status"] == "committed" and cleared["after"]["purpose"] is None


def test_cli_no_change_is_success_exit_code(capsys):
    assert _emit({"status": "no_change"}) == 0
    assert _emit({"status": "duplicate", "original_status": "no_change"}) == 0
    capsys.readouterr()


def test_invalid_calendar_date_is_rejected():
    with pytest.raises(InvalidInputError, match="日期"):
        _date_text("2026-99-99")

from __future__ import annotations

import io
from pathlib import Path

from openpyxl import Workbook
from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager, parse_import
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase


SAMPLES = Path(__file__).resolve().parents[2] / "datasets/china_invoice_guard"


def test_chinese_xml_and_ofd_samples_are_parsed_and_bad_amount_is_flagged():
    expected = {"01_数电票_正常.xml": ("24312000000098765432", "106.00"),
                "02_数电票_拼音标签.xml": ("24312000000011112222", "53.00"),
                "04_发票.ofd": ("24312000000077778888", "84.80")}
    for name, (number, amount) in expected.items():
        parsed = parse_import(filename=name, content=(SAMPLES / name).read_bytes(), mapping={}, default_currency="CNY")
        assert not parsed["issues"] and len(parsed["rows"]) == 1
        source = parsed["rows"][0]["source"]
        assert source["invoice_number"] == number and source["amount"] == amount and source["currency"] == "CNY"
    bad = parse_import(filename="03_数电票_金额被篡改.xml", content=(SAMPLES / "03_数电票_金额被篡改.xml").read_bytes(), mapping={}, default_currency="CNY")
    assert not bad["rows"] and "不等于价税合计" in bad["issues"][0]["details"]
    red = (SAMPLES / "01_数电票_正常.xml").read_text(encoding="utf-8").replace("普通发票", "红字普通发票").encode("utf-8")
    red_result = parse_import(filename="red.xml", content=red, mapping={}, default_currency="CNY")
    assert not red_result["rows"] and "冲红关系" in red_result["issues"][0]["details"]


def test_chinese_excel_uses_code_and_number_and_flags_long_numeric_numbers():
    book = Workbook()
    sheet = book.active
    sheet.append(["发票代码", "发票号码", "开票日期", "价税合计"])
    sheet.append(["110000000001", "01234567", "2026年07月06日", "106.00"])
    sheet.append(["310000000001", "01234567", "2026年07月06日", "53.00"])
    sheet.append(["", 24312000000098765432, "2026年07月06日", "84.80"])
    output = io.BytesIO()
    book.save(output)
    parsed = parse_import(filename="中国票据.xlsx", content=output.getvalue(), mapping={}, default_currency="CNY")
    assert [item["source_invoice_id"] for item in parsed["rows"]] == ["110000000001-01234567", "310000000001-01234567"]
    assert len(parsed["issues"]) == 1 and "可能丢失精度" in parsed["issues"][0]["details"]


def test_same_chinese_invoice_across_sources_is_not_counted_twice(tmp_path):
    with EnterpriseDatabase(tmp_path / "enterprise.sqlite3") as db:
        admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password="test-only-password-2026")
        service = EnterpriseService(db)
        first = service.create_company(actor=admin, name="甲公司", currency="CNY")
        second = service.create_company(actor=admin, name="乙公司", currency="CNY")
        accountant = create_user(db, username="accountant@example.test", display_name="会计", password="test-only-password-2026")
        for company in (first, second):
            assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        manager = ImportManager(db)
        original = (SAMPLES / "01_数电票_正常.xml").read_bytes()
        duplicate = (SAMPLES / "05_重复上传.xml").read_bytes()

        def ingest(company, source, content):
            scan = manager.scan_columns(user=accountant, company_id=company["company_id"], filename="invoice.xml", content=content)
            assert scan["document_format"] == "xml"
            preview = manager.preview(user=accountant, company_id=company["company_id"], source_system=source, filename="invoice.xml", content=content, mapping={})
            assert preview["issue_count"] == 0
            return manager.commit(user=accountant, preview_id=preview["preview_id"])

        assert ingest(first, "邮箱", original)["counts"]["inserted"] == 1
        repeated = ingest(first, "客户门户", duplicate)
        assert repeated["counts"]["duplicate_documents"] == 1 and repeated["counts"]["inserted"] == 0
        assert service.finance_summary(user=accountant, company_id=first["company_id"], period="2026-07")["by_currency"][0]["invoice_total"] == "106.00"
        assert ingest(second, "客户门户", duplicate)["counts"]["inserted"] == 1

        changed = original.replace(b">100.00<", b">101.00<").replace(b">106.00<", b">107.00<")
        conflict = ingest(first, "另一个来源", changed)
        assert conflict["counts"]["conflicts"] == 1 and conflict["counts"]["inserted"] == 0
        assert service.finance_summary(user=accountant, company_id=first["company_id"], period="2026-07")["by_currency"][0]["invoice_total"] == "106.00"

"""Compare enterprise imports with a public multi-currency invoice dataset."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
UPGRADE = APP.parent
sys.path.insert(0, str(APP / "src"))

from taxharness.enterprise.auth import assign_membership, create_first_admin, create_user
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=UPGRADE / "datasets/multicurrency_v1")
    args = parser.parse_args()
    headers = _read(args.data / "invoices.csv")
    lines = _read(args.data / "invoice_lines.csv")
    gross = defaultdict(Decimal)
    for line in lines:
        gross[line["invoice_id"]] += Decimal(line["line_gross"])
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "invoice_status", "customer_id"])
    expected = defaultdict(Decimal)
    expected_counts = defaultdict(int)
    for invoice in headers:
        amount = gross[invoice["id"]]
        writer.writerow([invoice["invoice_number"], invoice["issued_on"], format(amount, ".2f"), invoice["currency"],
                         "公开样本软件供应商", "软件产品与服务", invoice["status"], invoice["customer_id"]])
        if invoice["status"] != "void":
            key = (invoice["issued_on"][:7], invoice["currency"])
            expected[key] += amount
            expected_counts[key] += 1
    run_root = UPGRADE / "experiments" / ("public-multicurrency-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    payload = output.getvalue().encode("utf-8-sig")
    (run_root / "converted_invoices.csv").write_bytes(payload)
    report = {"source_invoices": len(headers), "source_lines": len(lines),
              "source_void": sum(invoice["status"] == "void" for invoice in headers), "periods": len({invoice["issued_on"][:7] for invoice in headers})}
    with EnterpriseDatabase(run_root / "enterprise.sqlite3") as db:
        admin = create_first_admin(db, username="public-admin@example.test", display_name="测试管理员", password="test-only-password-2026")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="公开数据测试企业", currency="CNY")
        accountant = create_user(db, username="public-accountant@example.test", display_name="测试会计", password="test-only-password-2026")
        assign_membership(db, user_id=accountant["user_id"], company_id=company["company_id"], role="accountant")
        manager = ImportManager(db)
        preview = manager.preview(user=accountant, company_id=company["company_id"], source_system="public-vat-v1",
                                  filename="converted_invoices.csv", content=payload, mapping={})
        result = manager.commit(user=accountant, preview_id=preview["preview_id"])
        actual = {}
        actual_counts = {}
        for period in sorted({invoice["issued_on"][:7] for invoice in headers}):
            summary = service.finance_summary(user=accountant, company_id=company["company_id"], period=period)
            for item in summary["by_currency"]:
                key = (period, item["currency"])
                actual[key] = Decimal(item["invoice_total"])
                actual_counts[key] = item["invoice_count"]
        mismatches = []
        for key in sorted(set(expected) | set(actual)):
            if expected.get(key, Decimal(0)) != actual.get(key, Decimal(0)) or expected_counts.get(key, 0) != actual_counts.get(key, 0):
                mismatches.append({"period": key[0], "currency": key[1], "expected_total": str(expected.get(key, Decimal(0))),
                                   "actual_total": str(actual.get(key, Decimal(0))), "expected_count": expected_counts.get(key, 0),
                                   "actual_count": actual_counts.get(key, 0)})
        report.update({"preview_accepted": preview["accepted_rows"], "preview_issues": preview["issues"], "import_counts": result["counts"],
                       "expected_active": sum(expected_counts.values()), "actual_active": sum(actual_counts.values()), "mismatches": mismatches,
                       "passed": preview["issue_count"] == 0 and not mismatches})
    path = run_root / "results.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"公开样本：{len(headers)} 张、{len(lines)} 条明细、{report['source_void']} 张作废；导入 {report['preview_accepted']} 张、异常 {len(report['preview_issues'])} 行、汇总差异 {len(mismatches)} 组。")
    print(f"结果：{path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import load_workbook
from .storage import CURRENCY_DIGITS as _CURRENCY_DIGITS


_ALIASES = {
    "source_invoice_id": {"source_invoice_id", "invoice_id", "发票编号", "发票号码", "数电发票号码", "票据编号", "票号", "单据编号"},
    "issue_date": {"issue_date", "date", "开票日期", "日期", "票据日期"},
    "amount": {"amount", "total", "amount_total", "价税合计", "价税合计（元）", "总额", "金额"},
    "currency": {"currency", "币种", "货币"},
    "invoice_status": {"invoice_status", "status", "发票状态", "票据状态", "单据状态"},
    "invoice_number": {"invoice_number", "发票号码", "数电发票号码"},
    "invoice_code": {"invoice_code", "发票代码"},
    "invoice_unique_code": {"invoice_unique_code", "uniquecodeofinvoice", "发票唯一代码"},
    "invoice_type": {"invoice_type", "发票类型"},
    "identity_status": {"identity_status"},
    "seller_tax_id": {"seller_tax_id", "销售方纳税人识别号", "销售方税号"},
    "buyer_name": {"buyer_name", "购买方名称"},
    "buyer_tax_id": {"buyer_tax_id", "购买方纳税人识别号", "购买方税号"},
    "net_amount": {"net_amount", "不含税金额", "金额合计"},
    "tax_amount": {"tax_amount", "税额合计", "合计税额"},
    "accounting_period": {"accounting_period", "会计期间"},
    "booked_status": {"booked_status", "入账状态"},
    "checked_status": {"checked_status", "勾选状态"},
    "paid_status": {"paid_status", "支付状态"},
    "seller_name": {"seller_name", "seller", "销售方名称", "销售方", "供应商"},
    "project_name": {"project_name", "项目名称", "项目名称（票面）", "项目"},
    "purpose": {"purpose", "用途", "业务用途"},
    "remarks": {"remarks", "备注", "说明"},
    "unit_price": {"unit_price", "单价", "不含税单价"},
    "quantity": {"quantity", "数量"},
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _text(value)
    for candidate in (raw, raw[:10]):
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    for pattern in ("%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%Y年%m月%d日"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"无法识别日期：{raw!r}")


def _decimal(value: Any, label: str, *, allow_blank: bool = True) -> str | None:
    raw = _text(value).replace(",", "")
    if not raw:
        if allow_blank:
            return None
        raise ValueError(f"{label}不能为空。")
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{label}不是有效数字：{raw!r}") from error
    if not number.is_finite():
        raise ValueError(f"{label}不是有限数字。")
    return format(number, "f")


def _invoice_status(value: Any) -> tuple[str, str]:
    raw = _text(value)
    normalized = raw.casefold().replace(" ", "_")
    if normalized in {"issued", "paid", "unpaid", "open", "active", "normal", "valid", "有效", "有效发票", "正常", "正常开具", "已开具", "已付款", "未付款"}:
        return "active", raw
    if normalized in {"void", "voided", "cancelled", "canceled", "作废", "已作废"}:
        return "void", raw
    raise ValueError(f"无法识别发票状态：{raw!r}，请核对后再导入。")


def _read_table(filename: str, content: bytes) -> tuple[list[str], list[list[Any]]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("gb18030")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if not rows:
            raise ValueError("CSV 文件没有数据。")
        headers = [_text(value) for value in rows[0]]
        return headers, rows[1:]
    if suffix not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 CSV、XLSX 和 XLSM 文件。")
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book[book.sheetnames[0]]
        values = list(sheet.iter_rows(values_only=True))
    finally:
        book.close()
    if not values:
        raise ValueError("Excel 工作表没有数据。")
    return [_text(value) for value in values[0]], [list(row) for row in values[1:]]


def _default_mapping(headers: list[str]) -> dict[str, str]:
    result = {}
    for field, aliases in _ALIASES.items():
        found = next((header for header in headers if header.strip().lower() in {alias.lower() for alias in aliases}), None)
        if found is not None:
            result[field] = found
    return result


def _document_identity_key(source: dict[str, Any]) -> tuple[str, str] | None:
    code = _text(source.get("invoice_code"))
    number = _text(source.get("invoice_number"))
    unique_code = _text(source.get("invoice_unique_code"))
    if unique_code.isdigit() and len(unique_code) == 20:
        return "", unique_code
    if code.isdigit() and number.isdigit():
        return code, number
    if not code and number.isdigit() and len(number) == 20:
        return "", number
    return None


def _weak_identity_tokens(source: dict[str, Any]) -> set[tuple[str, str]]:
    tokens = set()
    for key in ("invoice_unique_code", "invoice_number"):
        value = _text(source.get(key))
        if value:
            tokens.add(("reference", value))
    code = _text(source.get("invoice_code"))
    number = _text(source.get("invoice_number"))
    if code and number:
        tokens.add(("pair", f"{code}\x1f{number}"))
    return tokens


def _has_weak_identity_candidate(connection, *, company_id: str, source: dict[str, Any]) -> bool:
    tokens = _weak_identity_tokens(source)
    if not tokens:
        return False
    for row in connection.execute("SELECT source_data_json FROM enterprise_invoice_links WHERE company_id=?", (company_id,)):
        prior_source = json.loads(row["source_data_json"])
        if tokens & _weak_identity_tokens(prior_source):
            return True
    return False


def parse_import(*, filename: str, content: bytes, mapping: dict[str, str], default_currency: str) -> dict[str, Any]:
    if Path(filename).suffix.lower() in {".xml", ".ofd"}:
        from .document_ingest import read_structured_invoice

        records, document_issues = read_structured_invoice(filename, content)
        file_hash = hashlib.sha256(content).hexdigest()
        for record in records:
            record["identity_status"] = "strong" if _document_identity_key(record) else "needs_review"
        headers = ["source_invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name",
                   "invoice_number", "invoice_code", "invoice_unique_code", "invoice_type", "identity_status",
                   "seller_tax_id", "buyer_name", "buyer_tax_id", "net_amount", "tax_amount",
                   "accounting_period", "booked_status", "checked_status", "paid_status"]
        if any("invoice_status" in record for record in records):
            headers.append("invoice_status")
        if records:
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=headers)
            writer.writeheader()
            writer.writerows(records)
            parsed = parse_import(filename="structured.csv", content=output.getvalue().encode("utf-8-sig"), mapping={}, default_currency=default_currency)
        else:
            parsed = {"headers": headers, "mapping": _default_mapping(headers), "rows": [], "issues": []}
        parsed["file_hash"] = file_hash
        parsed["issues"] = document_issues + parsed["issues"]
        parsed["document_format"] = Path(filename).suffix.lower()[1:]
        return parsed
    headers, data_rows = _read_table(filename, content)
    mapping = {str(key): str(value) for key, value in mapping.items() if value}
    defaults = _default_mapping(headers)
    for field in _ALIASES:
        if field not in mapping:
            mapping[field] = defaults.get(field, "")
    missing = [field for field in ("source_invoice_id", "issue_date", "amount") if not mapping.get(field)]
    unknown_headers = sorted({value for value in mapping.values() if value and value not in headers})
    if missing or unknown_headers:
        raise ValueError(f"字段映射无效；缺少映射={missing}，文件中不存在的列={unknown_headers}。")
    positions = {header: index for index, header in enumerate(headers)}
    parsed: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    file_hash = hashlib.sha256(content).hexdigest()
    for row_number, raw_row in enumerate(data_rows, start=2):
        if not any(_text(value) for value in raw_row):
            continue
        source_id = _text(raw_row[positions[mapping["source_invoice_id"]]]) if positions[mapping["source_invoice_id"]] < len(raw_row) else ""
        try:
            if not source_id:
                raise ValueError("缺少稳定来源单据编号。")
            read = lambda field: (raw_row[positions[mapping[field]]] if mapping.get(field) in positions and positions[mapping[field]] < len(raw_row) else None)
            invoice_number = read("invoice_number") if mapping.get("invoice_number") else None
            if isinstance(invoice_number, (int, float)) and not isinstance(invoice_number, bool) and abs(invoice_number) >= 10**15:
                raise ValueError("Excel 中的长发票号码被保存为数值，可能丢失精度；请按文本格式重新导出。")
            invoice_code = _text(read("invoice_code")) if mapping.get("invoice_code") else ""
            source_alias_is_number = (mapping.get("source_invoice_id") == mapping.get("invoice_number")
                                      or (invoice_number is not None and source_id == _text(invoice_number)))
            if invoice_code and mapping.get("source_invoice_id") == mapping.get("invoice_number"):
                source_id = f"{invoice_code}-{_text(invoice_number)}"
            issue_date = _date(read("issue_date"))
            amount_text = _decimal(read("amount"), "票面金额", allow_blank=False)
            amount = Decimal(amount_text)
            if amount < 0:
                raise ValueError("负金额/退款/红冲数据须人工确认口径，已列异常，未导入正常发票。")
            currency = (_text(read("currency")) or default_currency).upper()
            if not re.fullmatch(r"[A-Z]{3}", currency) or currency not in _CURRENCY_DIGITS:
                raise ValueError(f"币种 {currency!r} 不在当前支持映射中，请先核实并配置。")
            digits = _CURRENCY_DIGITS[currency]
            scaled = amount * (10 ** digits)
            if scaled != scaled.to_integral_value():
                raise ValueError(f"{currency}金额的小数位超过当前映射的 {digits} 位。")
            amount_cents = int((amount * 100).to_integral_value()) if digits == 2 else int((amount * 100).to_integral_value())
            if digits != 2 and amount_cents < 0:
                raise ValueError("金额超出基础存储范围。")
            unit = _decimal(read("unit_price"), "单价")
            quantity = _decimal(read("quantity"), "数量")
            source = {
                "source_invoice_id": source_id,
                "issue_date": issue_date,
                "seller_name": _text(read("seller_name")),
                "project_name": _text(read("project_name")),
                "amount": amount_text,
                "amount_cents": amount_cents,
                "currency": currency,
                "unit_price": unit,
                "quantity": quantity,
                "purpose": _text(read("purpose")) or None,
                "remarks": _text(read("remarks")) or None,
            }
            if mapping.get("invoice_status"):
                source["source_status"], source["source_status_raw"] = _invoice_status(read("invoice_status"))
            for key in ("invoice_number", "invoice_code", "invoice_unique_code", "invoice_type", "identity_status",
                        "seller_tax_id", "buyer_name", "buyer_tax_id", "net_amount", "tax_amount",
                        "accounting_period", "booked_status", "checked_status", "paid_status"):
                if mapping.get(key):
                    source[key] = _text(read(key))
            if source_alias_is_number and _document_identity_key(source) is None:
                source["identity_status"] = "needs_review"
            digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            prior = seen.get(source_id)
            duplicate = prior is not None
            if duplicate and prior != digest:
                raise ValueError("同一来源文件重复出现单号但内容不同，第二行进入冲突异常。")
            seen[source_id] = digest
            parsed.append({"row_number": row_number, "source_invoice_id": source_id, "source": source, "content_hash": digest, "duplicate_in_file": duplicate})
        except (ValueError, IndexError) as error:
            issues.append({"row_number": row_number, "source_invoice_id": source_id or None, "issue_code": "invalid_row", "details": str(error)})
    return {"headers": headers, "mapping": mapping, "file_hash": file_hash, "rows": parsed, "issues": issues}


class ImportManager:
    def __init__(self, database):
        self.database = database

    def scan_columns(self, *, user: dict, company_id: str, filename: str, content: bytes) -> dict:
        from .auth import require_role

        require_role(self.database, user, company_id, {"accountant"})
        if Path(filename).suffix.lower() in {".xml", ".ofd"}:
            parsed = parse_import(filename=filename, content=content, mapping={}, default_currency="CNY")
            return {"filename": Path(filename).name, "headers": parsed["headers"], "suggested_mapping": parsed["mapping"],
                    "document_format": parsed["document_format"], "issues": parsed["issues"]}
        headers, _ = _read_table(filename, content)
        return {"filename": Path(filename).name, "headers": headers, "suggested_mapping": _default_mapping(headers)}

    def preview(self, *, user: dict, company_id: str, source_system: str, filename: str, content: bytes, mapping: dict[str, str]) -> dict:
        from .auth import require_role
        from .storage import utc_now
        from .service import EnterpriseService

        require_role(self.database, user, company_id, {"accountant"})
        if not source_system.strip() or len(source_system.strip()) > 120:
            raise ValueError("来源系统名称必须为 1 到 120 个字符。")
        settings = self.database.connection.execute("SELECT default_currency FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
        if settings is None:
            raise ValueError("企业不存在。")
        parsed = parse_import(filename=filename, content=content, mapping=mapping, default_currency=settings["default_currency"])
        preview_id = "preview-" + uuid4().hex
        now = datetime.now(UTC).replace(microsecond=0)
        expires = now + timedelta(hours=24)
        self.database.connection.execute(
            "INSERT INTO enterprise_import_previews(preview_id,user_id,company_id,source_system,filename,file_hash,rows_json,issues_json,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (preview_id, user["user_id"], company_id, source_system.strip(), Path(filename).name[:250], parsed["file_hash"], json.dumps(parsed["rows"], ensure_ascii=False), json.dumps(parsed["issues"], ensure_ascii=False), "pending", now.isoformat(), expires.isoformat()),
        )
        return {"preview_id": preview_id, "company_id": company_id, "source_system": source_system.strip(), "filename": Path(filename).name, "file_hash": parsed["file_hash"], "headers": parsed["headers"], "mapping": parsed["mapping"], "accepted_rows": len(parsed["rows"]), "duplicate_rows": sum(bool(row["duplicate_in_file"]) for row in parsed["rows"]), "issue_count": len(parsed["issues"]), "issues": parsed["issues"], "sample_rows": [{"row_number": row["row_number"], "source_invoice_id": row["source_invoice_id"], **row["source"]} for row in parsed["rows"][:20]]}

    def commit(self, *, user: dict, preview_id: str) -> dict:
        from .auth import require_role
        from .service import EnterpriseService, invoice_key
        from .storage import utc_now
        from ..services import TaxService

        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            preview = connection.execute("SELECT * FROM enterprise_import_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if preview is None or preview["status"] != "pending" or datetime.fromisoformat(preview["expires_at"]) <= datetime.now().astimezone():
                raise ValueError("导入预览不存在、已过期或已提交。")
            if preview["user_id"] != user["user_id"] and not user["is_admin"]:
                raise PermissionError("只能确认自己创建的导入预览。")
            require_role(self.database, user, preview["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=preview["company_id"], modify=True)
            company_id = preview["company_id"]
            source_system = preview["source_system"]
            rows = json.loads(preview["rows_json"])
            issues = json.loads(preview["issues_json"])
            import_id = "import-" + uuid4().hex
            now = utc_now()
            connection.execute(
                "INSERT INTO enterprise_imports(import_id,user_id,company_id,source_system,filename,file_hash,row_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (import_id, user["user_id"], company_id, source_system, preview["filename"], preview["file_hash"], len(rows), now),
            )
            prior_import = connection.execute(
                "SELECT import_id FROM enterprise_imports WHERE company_id=? AND file_hash=? AND import_id<>? ORDER BY created_at DESC LIMIT 1",
                (company_id, preview["file_hash"], import_id),
            ).fetchone()
            counts = {"inserted": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "identity_reviews": 0,
                      "duplicate_lines": 0, "duplicate_documents": 0, "void_in_source": 0}
            affected_periods: set[str] = set()
            recheck_by_period: dict[str, set[str]] = {}
            for issue in issues:
                connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)", ("issue-" + uuid4().hex, preview_id, issue["row_number"], issue.get("source_invoice_id"), issue["issue_code"], json.dumps(issue, ensure_ascii=False), now))
            for item in rows:
                source_id = item["source_invoice_id"]
                source = item["source"]
                affected_periods.add(source["issue_date"][:7])
                digest = item["content_hash"]
                row_number = int(item["row_number"])
                if item.get("duplicate_in_file"):
                    counts["duplicate_lines"] += 1
                    connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)", (import_id, row_number, source_system, source_id, company_id, None, digest, json.dumps(source, ensure_ascii=False, sort_keys=True), "duplicate_line", now))
                    continue
                if prior_import:
                    prior_row = connection.execute(
                        "SELECT source_invoice_id,content_hash,invoice_id FROM enterprise_import_rows WHERE import_id=? AND row_number=? AND status<>'duplicate_line'",
                        (prior_import["import_id"], row_number),
                    ).fetchone()
                    if prior_row and prior_row["source_invoice_id"] == source_id and prior_row["content_hash"] == digest:
                        if prior_row["invoice_id"] is None:
                            status = "identity_review"
                            counts["identity_reviews"] += 1
                            issue = {"row_number": row_number, "source_invoice_id": source_id, "issue_code": status,
                                     "details": "同一附件的上一条记录仍待身份核对；本次未创建或合并发票。"}
                            issues.append(issue)
                            connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                               ("issue-" + uuid4().hex, preview_id, row_number, source_id, status, json.dumps(issue, ensure_ascii=False), now))
                            connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (import_id, row_number, source_system, source_id, company_id, prior_row["invoice_id"], digest,
                                 json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
                            continue
                        current_link = connection.execute(
                            "SELECT source_hash FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?",
                            (company_id, prior_row["invoice_id"]),
                        ).fetchone()
                        if current_link and current_link["source_hash"] == digest:
                            status = "duplicate_document"
                            counts["duplicate_documents"] += 1
                            connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (import_id, row_number, source_system, source_id, company_id, prior_row["invoice_id"], digest,
                                 json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
                            continue
                link = connection.execute("SELECT * FROM enterprise_invoice_links WHERE company_id=? AND source_system=? AND source_invoice_id=?", (company_id, source_system, source_id)).fetchone()
                previous_source = json.loads(link["source_data_json"]) if link else {}
                if link and "source_status" not in source:
                    for key in ("source_status", "source_status_raw"):
                        if key in previous_source:
                            source[key] = previous_source[key]
                    digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                source_status = source.get("source_status", "active")
                if source_status == "void":
                    counts["void_in_source"] += 1
                number = source.get("invoice_number", "")
                code = source.get("invoice_code", "")
                identity_key = _document_identity_key(source)
                old_source_status = previous_source.get("source_status", "active") if link else None
                source_status_changed = link is not None and old_source_status != source_status
                if link is not None and identity_key is None and _weak_identity_tokens(source) and digest != link["source_hash"]:
                    status = "identity_review"
                    counts["identity_reviews"] += 1
                    issue = {"row_number": row_number, "source_invoice_id": source_id, "issue_code": status,
                             "details": "该来源编号不足以判断是否为同一张发票；本次未覆盖已有记录，请人工核对票面。"}
                    issues.append(issue)
                    connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                       ("issue-" + uuid4().hex, preview_id, row_number, source_id, status, json.dumps(issue, ensure_ascii=False), now))
                    connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (import_id, row_number, source_system, source_id, company_id, None, digest,
                         json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
                    continue
                if link is None and identity_key is None and _has_weak_identity_candidate(connection, company_id=company_id, source=source):
                    status = "identity_review"
                    counts["identity_reviews"] += 1
                    issue = {"row_number": row_number, "source_invoice_id": source_id, "issue_code": status,
                             "details": "发票代码、号码或唯一引用不足以唯一定位；系统未合并或重复计入，请人工核对票面。"}
                    issues.append(issue)
                    connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                       ("issue-" + uuid4().hex, preview_id, row_number, source_id, status, json.dumps(issue, ensure_ascii=False), now))
                    connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (import_id, row_number, source_system, source_id, company_id, None, digest,
                         json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
                    continue
                identity = connection.execute("SELECT * FROM enterprise_document_identities WHERE company_id=? AND invoice_code=? AND invoice_number=?",
                                              (company_id, identity_key[0], identity_key[1])).fetchone() if identity_key else None
                if identity is not None and link is None:
                    current = connection.execute("""SELECT i.issue_date,x.amount_text,x.currency,x.source_status FROM invoices i
                        JOIN enterprise_invoice_extra x USING(company_id,invoice_id) WHERE i.company_id=? AND i.invoice_id=?""",
                        (company_id, identity["invoice_id"])).fetchone()
                    seller_tax = source.get("seller_tax_id", "")
                    same = current is not None and current["issue_date"] == source["issue_date"] and Decimal(current["amount_text"]) == Decimal(source["amount"]) and current["currency"] == source["currency"]
                    same = same and (not seller_tax or not identity["seller_tax_id"] or seller_tax == identity["seller_tax_id"])
                    same = same and ("source_status" not in source or source_status == current["source_status"])
                    status = "duplicate_document" if same else "identity_conflict"
                    if same:
                        counts["duplicate_documents"] += 1
                    else:
                        counts["conflicts"] += 1
                        issue = {"row_number": row_number, "source_invoice_id": source_id, "issue_code": status,
                                 "details": "相同发票代码和号码的票面信息不一致，请人工核对。"}
                        issues.append(issue)
                        connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                           ("issue-" + uuid4().hex, preview_id, row_number, source_id, status, json.dumps(issue, ensure_ascii=False), now))
                    connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (import_id, row_number, source_system, source_id, company_id, identity["invoice_id"], digest,
                         json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
                    continue
                invoice_id = link["invoice_id"] if link else invoice_key(company_id, source_system, source_id)
                current_invoice = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, invoice_id)).fetchone()
                if current_invoice is None:
                    connection.execute(
                        "INSERT INTO invoices(company_id,invoice_id,issue_date,seller_name,project_name,amount_cents,purpose,remarks,revision) VALUES(?,?,?,?,?,?,?,?,1)",
                        (company_id, invoice_id, source["issue_date"], source["seller_name"], source["project_name"], source["amount_cents"], source["purpose"], source["remarks"]),
                    )
                    owners = {"purpose": "source", "remarks": "source"}
                    connection.execute(
                        "INSERT INTO enterprise_invoice_links(company_id,source_system,source_invoice_id,invoice_id,source_hash,source_data_json,field_owners_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (company_id, source_system, source_id, invoice_id, digest, json.dumps(source, ensure_ascii=False, sort_keys=True), json.dumps(owners), now, now),
                    )
                    connection.execute(
                        "INSERT INTO enterprise_invoice_extra(company_id,invoice_id,currency,source_status,amount_text,unit_price_text,quantity_text,purpose_review_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (company_id, invoice_id, source["currency"], source_status, source["amount"], source["unit_price"], source["quantity"], "approved" if source["purpose"] else "pending", now),
                    )
                    counts["inserted"] += 1
                    status = "inserted"
                else:
                    old_source = previous_source
                    affected_periods.add(current_invoice["issue_date"][:7])
                    owners = json.loads(link["field_owners_json"] or "{}")
                    source_version_changed = digest != link["source_hash"]
                    changes = {field: source[field] for field in ("issue_date", "seller_name", "project_name", "amount_cents") if current_invoice[field] != source[field]}
                    conflict_count = 0
                    for field in ("purpose", "remarks"):
                        incoming = source[field]
                        current_value = current_invoice[field]
                        if incoming is None:
                            continue
                        if owners.get(field) == "local_confirmed" and incoming != current_value:
                            known_conflict = connection.execute(
                                """SELECT 1 FROM enterprise_import_conflicts WHERE company_id=? AND invoice_id=? AND field_name=?
                                   AND incoming_source_value=? AND (status='open' OR resolution='keep_local') LIMIT 1""",
                                (company_id, invoice_id, field, incoming),
                            ).fetchone()
                            if known_conflict is None:
                                connection.execute(
                                    "INSERT INTO enterprise_import_conflicts(conflict_id,import_id,company_id,invoice_id,field_name,current_value,prior_source_value,incoming_source_value,status,created_at) VALUES(?,?,?,?,?,?,?,?, 'open',?)",
                                    ("conflict-" + uuid4().hex, import_id, company_id, invoice_id, field, current_value, old_source.get(field), incoming, now),
                                )
                            conflict_count += 1
                        elif owners.get(field) != "local_confirmed" and incoming != current_value:
                            changes[field] = incoming
                            owners[field] = "source"
                    if changes:
                        assignments = ",".join(f"{field}=?" for field in changes)
                        connection.execute(f"UPDATE invoices SET {assignments},revision=revision+1 WHERE company_id=? AND invoice_id=?", (*changes.values(), company_id, invoice_id))
                    elif source_version_changed:
                        connection.execute("UPDATE invoices SET revision=revision+1 WHERE company_id=? AND invoice_id=?", (company_id, invoice_id))
                    if changes or source_version_changed:
                        counts["updated"] += 1
                        status = "updated"
                    else:
                        counts["unchanged"] += 1
                        status = "unchanged"
                    if conflict_count:
                        counts["conflicts"] += conflict_count
                        status = "conflict"
                    connection.execute(
                        "UPDATE enterprise_invoice_links SET source_hash=?,source_data_json=?,field_owners_json=?,updated_at=? WHERE company_id=? AND invoice_id=?",
                        (digest, json.dumps(source, ensure_ascii=False, sort_keys=True), json.dumps(owners), now, company_id, invoice_id),
                    )
                    connection.execute(
                        "UPDATE enterprise_invoice_extra SET currency=?,source_status=?,amount_text=?,unit_price_text=?,quantity_text=?,updated_at=? WHERE company_id=? AND invoice_id=?",
                        (source["currency"], source_status, source["amount"], source["unit_price"], source["quantity"], now, company_id, invoice_id),
                    )
                    if source_version_changed:
                        connection.execute("UPDATE enterprise_invoice_extra SET purpose_review_status='stale',disposition=NULL,updated_at=? WHERE company_id=? AND invoice_id=?", (now, company_id, invoice_id))
                        if source_status_changed:
                            connection.execute("UPDATE enterprise_drafts SET status='conflict',draft_revision=draft_revision+1 WHERE company_id=? AND invoice_id=? AND status IN ('proposed','clarification')",
                                               (company_id, invoice_id))
                        connection.execute(
                            """UPDATE enterprise_task_invoices SET status='pending',disposition=NULL,updated_at=?
                               WHERE company_id=? AND invoice_id=? AND task_id IN
                                 (SELECT task_id FROM enterprise_work_items WHERE company_id=? AND status<>'closed')""",
                            (now, company_id, invoice_id, company_id),
                        )
                        connection.execute(
                            """UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=?
                               WHERE status<>'closed' AND task_id IN
                                 (SELECT task_id FROM enterprise_task_invoices WHERE company_id=? AND invoice_id=?)""",
                            (now, company_id, invoice_id),
                        )
                        closed = connection.execute(
                            """SELECT 1 FROM enterprise_task_invoices ti JOIN enterprise_work_items w USING(task_id)
                               WHERE ti.company_id=? AND ti.invoice_id=? AND w.status='closed' LIMIT 1""",
                            (company_id, invoice_id),
                        ).fetchone()
                        if closed:
                            recheck_by_period.setdefault(source["issue_date"][:7], set()).add(invoice_id)
                if identity_key:
                    connection.execute("""INSERT OR IGNORE INTO enterprise_document_identities
                        (company_id,invoice_code,invoice_number,invoice_id,seller_tax_id,created_at) VALUES(?,?,?,?,?,?)""",
                        (company_id, identity_key[0], identity_key[1], invoice_id, source.get("seller_tax_id", ""), now))
                connection.execute("INSERT INTO enterprise_import_rows VALUES(?,?,?,?,?,?,?,?,?,?)", (import_id, row_number, source_system, source_id, company_id, invoice_id, digest, json.dumps(source, ensure_ascii=False, sort_keys=True), status, now))
            service = EnterpriseService(self.database)
            followups = [service._ensure_task(company_id=company_id, period=period, user_id=user["user_id"], invoice_ids=sorted(invoice_ids), origin="source_change") for period, invoice_ids in sorted(recheck_by_period.items())]
            connection.execute("UPDATE enterprise_import_previews SET status='committed' WHERE preview_id=?", (preview_id,))
            connection.commit()
            refresh = [service.list_missing(user=user, company_id=company_id, period=period, page=1, page_size=1) for period in sorted(affected_periods)]
            return {"status": "ok", "import_id": import_id, "preview_id": preview_id, "company_id": company_id, "source_system": source_system, "counts": counts, "issues": issues, "period_refresh": [{"period": item.get("period"), "status": item.get("status"), "missing_count": item.get("missing_count"), "summary": item.get("summary")} for item in refresh], "source_change_followups": [item["task_id"] for item in followups]}
        except Exception:
            connection.rollback()
            raise

    def resolve_conflict(self, *, user: dict, conflict_id: str, resolution: str) -> dict:
        from .auth import require_role
        from .storage import utc_now
        from ..services import TaxService

        if resolution not in {"keep_local", "accept_source"}:
            raise ValueError("resolution 必须是 keep_local 或 accept_source。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            conflict = connection.execute("SELECT * FROM enterprise_import_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
            if conflict is None or conflict["status"] != "open":
                raise ValueError("冲突不存在或已经处理。")
            require_role(self.database, user, conflict["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=conflict["company_id"], modify=True)
            if resolution == "accept_source":
                current = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (conflict["company_id"], conflict["invoice_id"])).fetchone()
                if current is None:
                    raise ValueError("来源发票已经不存在。")
                connection.execute(f"UPDATE invoices SET {conflict['field_name']}=?,revision=revision+1 WHERE company_id=? AND invoice_id=?", (conflict["incoming_source_value"], conflict["company_id"], conflict["invoice_id"]))
                link = connection.execute("SELECT field_owners_json FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (conflict["company_id"], conflict["invoice_id"])).fetchone()
                owners = json.loads(link["field_owners_json"] or "{}")
                owners[conflict["field_name"]] = "source"
                connection.execute("UPDATE enterprise_invoice_links SET field_owners_json=?,updated_at=? WHERE company_id=? AND invoice_id=?", (json.dumps(owners), utc_now(), conflict["company_id"], conflict["invoice_id"]))
                connection.execute("UPDATE enterprise_invoice_extra SET purpose_review_status='stale',disposition=NULL,updated_at=? WHERE company_id=? AND invoice_id=?", (utc_now(), conflict["company_id"], conflict["invoice_id"]))
                connection.execute(
                    """UPDATE enterprise_task_invoices SET status='pending',disposition=NULL,updated_at=?
                       WHERE company_id=? AND invoice_id=? AND task_id IN
                         (SELECT task_id FROM enterprise_work_items WHERE company_id=? AND status<>'closed')""",
                    (utc_now(), conflict["company_id"], conflict["invoice_id"], conflict["company_id"]),
                )
                connection.execute(
                    """UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=?
                       WHERE status<>'closed' AND task_id IN
                         (SELECT task_id FROM enterprise_task_invoices WHERE company_id=? AND invoice_id=?)""",
                    (utc_now(), conflict["company_id"], conflict["invoice_id"]),
                )
                closed = connection.execute(
                    """SELECT 1 FROM enterprise_task_invoices ti JOIN enterprise_work_items w USING(task_id)
                       WHERE ti.company_id=? AND ti.invoice_id=? AND w.status='closed' LIMIT 1""",
                    (conflict["company_id"], conflict["invoice_id"]),
                ).fetchone()
                if closed:
                    from .service import EnterpriseService
                    period = str(current["issue_date"])[:7]
                    EnterpriseService(self.database)._ensure_task(company_id=conflict["company_id"], period=period, user_id=user["user_id"], invoice_ids=[conflict["invoice_id"]], origin="source_change")
            connection.execute("UPDATE enterprise_import_conflicts SET status='resolved',resolved_by=?,resolution=?,resolved_at=? WHERE conflict_id=?", (user["user_id"], resolution, utc_now(), conflict_id))
            connection.commit()
            return {"status": "resolved", "conflict_id": conflict_id, "resolution": resolution}
        except Exception:
            connection.rollback()
            raise


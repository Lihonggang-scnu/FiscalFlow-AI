from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
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


class IdentityReviewConflict(RuntimeError):
    """An identity decision was based on an older review revision or command."""


class ImportConflictChanged(RuntimeError):
    """An import conflict decision was based on an older invoice state."""


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


def _insert_import_row(connection, *, import_id: str, row_number: int, source_system: str, source_invoice_id: str,
                       company_id: str, invoice_id: str | None, content_hash: str, source: dict[str, Any],
                       status: str, identity_review_id: str | None, created_at: str) -> None:
    connection.execute(
        """INSERT INTO enterprise_import_rows
           (import_id,row_number,source_system,source_invoice_id,company_id,invoice_id,content_hash,source_json,status,identity_review_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (import_id, row_number, source_system, source_invoice_id, company_id, invoice_id, content_hash,
         json.dumps(source, ensure_ascii=False, sort_keys=True), status, identity_review_id, created_at),
    )


def _insert_source_alias(connection, *, company_id: str, source_system: str, source_invoice_id: str,
                         invoice_id: str | None, document_id: str, identity_review_id: str | None,
                         row_number: int, locator: dict[str, Any], content_hash: str,
                         source: dict[str, Any], status: str, created_at: str) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO enterprise_source_aliases
           (alias_id,company_id,source_system,source_invoice_id,invoice_id,document_id,identity_review_id,
            row_number,locator_json,content_hash,source_json,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("alias-" + uuid4().hex, company_id, source_system, source_invoice_id, invoice_id, document_id,
         identity_review_id, row_number, json.dumps(locator, ensure_ascii=False, sort_keys=True), content_hash,
         json.dumps(source, ensure_ascii=False, sort_keys=True), status, created_at),
    )


def _open_identity_review(connection, *, company_id: str, import_id: str, row_number: int,
                          source_system: str, source_invoice_id: str, content_hash: str, file_hash: str,
                          source: dict[str, Any], locator: dict[str, Any], document_id: str,
                          reason: str, created_at: str) -> str:
    existing = connection.execute(
        """SELECT identity_review_id FROM enterprise_identity_reviews
           WHERE company_id=? AND source_invoice_id=? AND content_hash=? AND status IN ('open','deferred')
           ORDER BY created_at LIMIT 1""",
        (company_id, source_invoice_id, content_hash),
    ).fetchone()
    if existing:
        return existing["identity_review_id"]
    identity_review_id = "identity-" + uuid4().hex
    connection.execute(
        """INSERT INTO enterprise_identity_reviews
           (identity_review_id,company_id,import_id,row_number,source_system,source_invoice_id,content_hash,file_hash,
            source_json,locator_json,document_id,reason,status,revision,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open',1,?,?)""",
        (identity_review_id, company_id, import_id, row_number, source_system, source_invoice_id, content_hash,
         file_hash, json.dumps(source, ensure_ascii=False, sort_keys=True),
         json.dumps(locator, ensure_ascii=False, sort_keys=True), document_id, reason, created_at, created_at),
    )
    return identity_review_id


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
            writer.writerows({key: value for key, value in record.items() if key != "_source_locator"} for record in records)
            parsed = parse_import(filename="structured.csv", content=output.getvalue().encode("utf-8-sig"), mapping={}, default_currency=default_currency)
        else:
            parsed = {"headers": headers, "mapping": _default_mapping(headers), "rows": [], "issues": []}
        parsed["file_hash"] = file_hash
        parsed["issues"] = document_issues + parsed["issues"]
        parsed["document_format"] = Path(filename).suffix.lower()[1:]
        for item, record in zip(parsed["rows"], records):
            item["locator"] = record.get("_source_locator", {"kind": parsed["document_format"], "record_index": item["row_number"] - 1})
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
            digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            prior = seen.get(source_id)
            duplicate = prior is not None
            if duplicate and prior != digest:
                raise ValueError("同一来源文件重复出现单号但内容不同，第二行进入冲突异常。")
            seen[source_id] = digest
            parsed.append({"row_number": row_number, "source_invoice_id": source_id, "source": source,
                           "content_hash": digest, "duplicate_in_file": duplicate,
                           "locator": {"kind": "table_row", "row_number": row_number}})
        except (ValueError, IndexError) as error:
            issues.append({"row_number": row_number, "source_invoice_id": source_id or None, "issue_code": "invalid_row", "details": str(error)})
    return {"headers": headers, "mapping": mapping, "file_hash": file_hash, "rows": parsed, "issues": issues}


class ImportManager:
    def __init__(self, database):
        self.database = database

    def source_policy(self, *, user: dict, company_id: str, source_system: str) -> dict:
        from .auth import require_role

        require_role(self.database, user, company_id, {"accountant", "reviewer"})
        source_system = source_system.strip()
        if not source_system or len(source_system) > 120:
            raise ValueError("来源系统名称必须为 1 到 120 个字符。")
        row = self.database.connection.execute(
            "SELECT mode,revision,updated_by,updated_at FROM enterprise_import_source_policies WHERE company_id=? AND source_system=?",
            (company_id, source_system),
        ).fetchone()
        return {"company_id": company_id, "source_system": source_system,
                **(dict(row) if row else {"mode": "review", "revision": 0, "updated_by": None, "updated_at": None})}

    def set_source_policy(self, *, user: dict, company_id: str, source_system: str, mode: str, expected_revision: int) -> dict:
        from .auth import require_role
        from .storage import utc_now

        membership = require_role(self.database, user, company_id, {"accountant"})
        if not membership["is_admin"]:
            raise PermissionError("只有管理员能配置来源的同步方式。")
        if mode not in {"review", "trusted_sync"} or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("请选择有效的来源同步方式和版本。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.source_policy(user=user, company_id=company_id, source_system=source_system)
            if current["revision"] != expected_revision:
                raise ValueError("来源配置已变化，请刷新后重新设置。")
            revision = expected_revision + 1
            connection.execute("""INSERT INTO enterprise_import_source_policies
                (company_id,source_system,mode,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(company_id,source_system) DO UPDATE SET mode=excluded.mode,revision=excluded.revision,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (company_id, current["source_system"], mode, revision, user["user_id"], utc_now()))
            connection.commit()
            return self.source_policy(user=user, company_id=company_id, source_system=source_system)
        except Exception:
            connection.rollback()
            raise

    def _queue_source_draft(self, *, user: dict, service, import_id: str, document_id: str, row_number: int,
                            company_id: str, invoice_id: str, source: dict, fields: list[str],
                            mode: str, policy_revision: int, conflict_id: str | None = None) -> str | None:
        """Attach proposed source fields to the existing work and confirmation flow."""
        from .storage import utc_now

        connection = self.database.connection
        current = connection.execute("""SELECT i.*,x.source_status FROM invoices i JOIN enterprise_invoice_extra x
            USING(company_id,invoice_id) WHERE i.company_id=? AND i.invoice_id=?""", (company_id, invoice_id)).fetchone()
        if current["source_status"] == "void":
            return None
        fields = [field for field in fields if source.get(field) and source[field] != current[field]]
        if not fields:
            return None
        alias = connection.execute("""SELECT a.alias_id,a.source_system,a.source_invoice_id,a.content_hash,a.locator_json,d.file_hash
            FROM enterprise_source_aliases a JOIN enterprise_source_documents d USING(document_id)
            WHERE a.document_id=? AND a.row_number=? AND a.company_id=? AND a.invoice_id=? AND a.status='linked'""",
            (document_id, row_number, company_id, invoice_id)).fetchone()
        if alias is None:
            raise ValueError("来源票据尚未关联，无法生成确认草稿。")
        task = service._ensure_task(company_id=company_id, period=current["issue_date"][:7],
                                    user_id=user["user_id"], invoice_ids=[invoice_id], origin="source_change")
        draft_id = "draft-" + uuid4().hex
        evidence = {"type": "source_document", "document_id": document_id, "file_hash": alias["file_hash"],
                    "alias_id": alias["alias_id"], "content_hash": alias["content_hash"],
                    "source_system": alias["source_system"], "source_invoice_id": alias["source_invoice_id"],
                    "locator": json.loads(alias["locator_json"])}
        contexts = {field: {"base_value": current[field], "base_revision": int(current["revision"]),
                            "evidence_quote": source[field], "evidence_source": evidence,
                            "mode": "draft", "history": []} for field in fields}
        connection.execute("""INSERT INTO enterprise_drafts
            (draft_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,proposed_purpose,proposed_remarks,
             evidence_quote,status,source_revision,field_context_json,origin,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?, 'proposed',?,?, 'import',?,?)""",
            (draft_id, task["task_id"], company_id, invoice_id, json.dumps([invoice_id]),
             source.get("purpose") if "purpose" in fields else None, source.get("remarks") if "remarks" in fields else None,
             source[fields[0]], int(current["revision"]), json.dumps(contexts, ensure_ascii=False), user["user_id"], utc_now()))
        connection.execute("""INSERT INTO enterprise_import_drafts(draft_id,import_id,row_number,conflict_id,auto_mode,policy_revision)
            VALUES(?,?,?,?,?,?)""", (draft_id, import_id, row_number, conflict_id, mode, policy_revision))
        older = connection.execute("""SELECT d.draft_id,d.proposed_purpose,d.proposed_remarks,d.field_context_json
            FROM enterprise_import_drafts q JOIN enterprise_drafts d USING(draft_id)
            WHERE d.company_id=? AND d.invoice_id=? AND d.draft_id<>? AND d.status='proposed'
            AND d.superseded_by IS NULL AND d.draft_revision=1 AND q.conflict_id IS NULL""",
            (company_id, invoice_id, draft_id)).fetchall()
        for old in older:
            old_fields = [field for field in ("purpose", "remarks") if old["proposed_" + field] is not None]
            context = json.loads(old["field_context_json"] or "{}")
            if old_fields and set(old_fields).issubset(fields) and all(
                    context.get(field, {}).get("evidence_source", {}).get("source_system") == alias["source_system"] and
                    context.get(field, {}).get("evidence_source", {}).get("source_invoice_id") == alias["source_invoice_id"]
                    for field in old_fields):
                connection.execute("UPDATE enterprise_drafts SET status='rejected',superseded_by=?,draft_revision=draft_revision+1 WHERE draft_id=?",
                                   (draft_id, old["draft_id"]))
        connection.execute("UPDATE enterprise_task_invoices SET status='pending',disposition=NULL,updated_at=? WHERE task_id=? AND invoice_id=?",
                           (utc_now(), task["task_id"], invoice_id))
        connection.execute("UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=? WHERE task_id=? AND status<>'closed'",
                           (utc_now(), task["task_id"]))
        service._mark_field_decisions(connection, task["task_id"], invoice_id, fields)
        return draft_id

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
        from ..services import TaxService

        require_role(self.database, user, company_id, {"accountant"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=company_id, modify=True)
        if not source_system.strip() or len(source_system.strip()) > 120:
            raise ValueError("来源系统名称必须为 1 到 120 个字符。")
        policy = self.source_policy(user=user, company_id=company_id, source_system=source_system)
        settings = self.database.connection.execute("SELECT default_currency FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
        if settings is None:
            raise ValueError("企业不存在。")
        parsed = parse_import(filename=filename, content=content, mapping=mapping, default_currency=settings["default_currency"])
        preview_id = "preview-" + uuid4().hex
        now = datetime.now(UTC).replace(microsecond=0)
        expires = now + timedelta(hours=24)
        self.database.connection.execute(
            "UPDATE enterprise_import_previews SET file_content=NULL WHERE status='pending' AND expires_at<=?",
            (now.isoformat(),),
        )
        self.database.connection.execute(
            "INSERT INTO enterprise_import_previews(preview_id,user_id,company_id,source_system,filename,file_hash,rows_json,issues_json,file_content,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (preview_id, user["user_id"], company_id, source_system.strip(), Path(filename).name[:250], parsed["file_hash"], json.dumps(parsed["rows"], ensure_ascii=False), json.dumps(parsed["issues"], ensure_ascii=False), content, "pending", now.isoformat(), expires.isoformat()),
        )
        self.database.connection.execute("INSERT INTO enterprise_import_preview_policies(preview_id,mode,revision) VALUES(?,?,?)",
                                         (preview_id, policy["mode"], policy["revision"]))
        return {"preview_id": preview_id, "company_id": company_id, "source_system": source_system.strip(), "source_policy": policy, "filename": Path(filename).name, "file_hash": parsed["file_hash"], "headers": parsed["headers"], "mapping": parsed["mapping"], "accepted_rows": len(parsed["rows"]), "duplicate_rows": sum(bool(row["duplicate_in_file"]) for row in parsed["rows"]), "issue_count": len(parsed["issues"]), "issues": parsed["issues"], "sample_rows": [{"row_number": row["row_number"], "source_invoice_id": row["source_invoice_id"], **row["source"]} for row in parsed["rows"][:20]]}

    def commit(self, *, user: dict, preview_id: str) -> dict:
        from .auth import require_role
        from .service import EnterpriseService, invoice_key
        from .storage import utc_now
        from ..services import TaxService

        connection = self.database.connection
        service = EnterpriseService(self.database)
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
            policy = self.source_policy(user=user, company_id=company_id, source_system=source_system)
            observed = connection.execute("SELECT mode,revision FROM enterprise_import_preview_policies WHERE preview_id=?", (preview_id,)).fetchone()
            if observed and (observed["mode"] != policy["mode"] or observed["revision"] != policy["revision"]):
                raise ValueError("来源同步配置已变化，请重新预览后导入。")
            if not observed:
                policy = {**policy, "mode": "review", "revision": 0}
            rows = json.loads(preview["rows_json"])
            issues = json.loads(preview["issues_json"])
            import_id = "import-" + uuid4().hex
            now = utc_now()
            connection.execute(
                "INSERT INTO enterprise_imports(import_id,user_id,company_id,source_system,filename,file_hash,row_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (import_id, user["user_id"], company_id, source_system, preview["filename"], preview["file_hash"], len(rows), now),
            )
            document_id = "source-" + uuid4().hex
            file_content = preview["file_content"]
            connection.execute(
                """INSERT INTO enterprise_source_documents
                   (document_id,company_id,import_id,source_system,filename,file_hash,file_content,original_available,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (document_id, company_id, import_id, source_system, preview["filename"], preview["file_hash"],
                 file_content, int(file_content is not None), now),
            )
            prior_import = connection.execute(
                "SELECT import_id FROM enterprise_imports WHERE company_id=? AND file_hash=? AND import_id<>? ORDER BY created_at DESC LIMIT 1",
                (company_id, preview["file_hash"], import_id),
            ).fetchone()
            counts = {"inserted": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "identity_reviews": 0,
                      "duplicate_lines": 0, "duplicate_documents": 0, "void_in_source": 0, "proposals": 0}
            queued_drafts = []
            affected_periods: set[str] = set()
            recheck_by_period: dict[str, set[str]] = {}
            resolved_in_batch: dict[tuple[str, str], tuple[str | None, str | None]] = {}

            def record_row(item, *, status, invoice_id=None, identity_review_id=None, alias_status="linked"):
                row_number = int(item["row_number"])
                locator = item.get("locator", {"kind": "table_row", "row_number": row_number})
                _insert_import_row(connection, import_id=import_id, row_number=row_number, source_system=source_system,
                                   source_invoice_id=item["source_invoice_id"], company_id=company_id,
                                   invoice_id=invoice_id, content_hash=item["content_hash"], source=item["source"],
                                   status=status, identity_review_id=identity_review_id, created_at=now)
                _insert_source_alias(connection, company_id=company_id, source_system=source_system,
                                     source_invoice_id=item["source_invoice_id"], invoice_id=invoice_id,
                                     document_id=document_id, identity_review_id=identity_review_id,
                                     row_number=row_number, locator=locator, content_hash=item["content_hash"],
                                     source=item["source"], status=alias_status, created_at=now)
                resolved_in_batch[(item["source_invoice_id"], item["content_hash"])] = (invoice_id, identity_review_id)

            def queue_identity(item, *, reason, issue_code="identity_review", issue_message=None, count_conflict=False):
                review_id = _open_identity_review(
                    connection, company_id=company_id, import_id=import_id, row_number=int(item["row_number"]),
                    source_system=source_system, source_invoice_id=item["source_invoice_id"],
                    content_hash=item["content_hash"], file_hash=preview["file_hash"], source=item["source"],
                    locator=item.get("locator", {"kind": "table_row", "row_number": int(item["row_number"])}),
                    document_id=document_id, reason=reason, created_at=now,
                )
                counts["identity_reviews"] += 1
                if count_conflict:
                    counts["conflicts"] += 1
                if issue_message:
                    issue = {"row_number": item["row_number"], "source_invoice_id": item["source_invoice_id"],
                             "issue_code": issue_code, "details": issue_message}
                    issues.append(issue)
                    connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                       ("issue-" + uuid4().hex, preview_id, item["row_number"], item["source_invoice_id"], issue_code, json.dumps(issue, ensure_ascii=False), now))
                record_row(item, status=issue_code, identity_review_id=review_id, alias_status="pending_identity")
                return review_id

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
                    target, review_id = resolved_in_batch.get((source_id, digest), (None, None))
                    record_row(item, status="duplicate_line", invoice_id=target, identity_review_id=review_id,
                               alias_status="linked" if target else "pending_identity" if review_id else "rejected")
                    continue
                if prior_import:
                    prior_row = connection.execute(
                        "SELECT source_invoice_id,content_hash,invoice_id,identity_review_id FROM enterprise_import_rows WHERE import_id=? AND row_number=? AND status<>'duplicate_line'",
                        (prior_import["import_id"], row_number),
                    ).fetchone()
                    if prior_row and prior_row["source_invoice_id"] == source_id and prior_row["content_hash"] == digest:
                        if prior_row["invoice_id"] is None:
                            status = "identity_review"
                            review_id = prior_row["identity_review_id"] or _open_identity_review(
                                connection, company_id=company_id, import_id=import_id, row_number=row_number,
                                source_system=source_system, source_invoice_id=source_id, content_hash=digest,
                                file_hash=preview["file_hash"], source=source, locator=item.get("locator", {"kind": "table_row", "row_number": row_number}),
                                document_id=document_id, reason="同一附件的上一条记录仍待身份核对。", created_at=now,
                            )
                            counts["identity_reviews"] += 1
                            issue = {"row_number": row_number, "source_invoice_id": source_id, "issue_code": status,
                                     "details": "同一附件的上一条记录仍待身份核对；本次未创建或合并发票。"}
                            issues.append(issue)
                            connection.execute("INSERT INTO enterprise_import_issues(issue_id,preview_id,row_number,source_invoice_id,issue_code,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                               ("issue-" + uuid4().hex, preview_id, row_number, source_id, status, json.dumps(issue, ensure_ascii=False), now))
                            record_row(item, status=status, identity_review_id=review_id, alias_status="pending_identity")
                            continue
                        current_link = connection.execute(
                            "SELECT source_hash FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?",
                            (company_id, prior_row["invoice_id"]),
                        ).fetchone()
                        if current_link and current_link["source_hash"] == digest:
                            status = "duplicate_document"
                            counts["duplicate_documents"] += 1
                            record_row(item, status=status, invoice_id=prior_row["invoice_id"])
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
                resolved_alias = connection.execute(
                    """SELECT invoice_id FROM enterprise_source_aliases WHERE company_id=? AND source_system=?
                       AND source_invoice_id=? AND content_hash=? AND identity_review_id IS NOT NULL
                       AND status='linked' AND invoice_id IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
                    (company_id, source_system, source_id, digest),
                ).fetchone()
                if resolved_alias:
                    counts["duplicate_documents"] += 1
                    record_row(item, status="duplicate_document", invoice_id=resolved_alias["invoice_id"])
                    continue
                if link is not None and identity_key is None and _weak_identity_tokens(source) and digest != link["source_hash"]:
                    queue_identity(item, reason="同一来源编号对应的弱票据身份发生变化。",
                                   issue_message="该来源编号不足以判断是否为同一张发票；本次未覆盖已有记录，请人工核对票面。")
                    continue
                if link is None and identity_key is None and (
                    source.get("identity_status") == "needs_review"
                    or _has_weak_identity_candidate(connection, company_id=company_id, source=source)
                ):
                    queue_identity(item, reason="发票代码、号码或唯一引用不足以安全确定票据身份。",
                                   issue_message="发票代码、号码或唯一引用不足以唯一定位；系统未合并或重复计入，请人工核对票面。")
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
                        queue_identity(item, reason="发票代码和号码与已有票据匹配，但票面字段存在差异。",
                                       issue_code=status, issue_message="相同发票代码和号码的票面信息不一致，请人工核对。",
                                       count_conflict=True)
                        continue
                    record_row(item, status=status, invoice_id=identity["invoice_id"])
                    continue
                invoice_id = link["invoice_id"] if link else invoice_key(company_id, source_system, source_id)
                current_invoice = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, invoice_id)).fetchone()
                proposal_fields = []
                if current_invoice is None:
                    connection.execute(
                        "INSERT INTO invoices(company_id,invoice_id,issue_date,seller_name,project_name,amount_cents,purpose,remarks,revision) VALUES(?,?,?,?,?,?,?,?,1)",
                        (company_id, invoice_id, source["issue_date"], source["seller_name"], source["project_name"], source["amount_cents"], None, None),
                    )
                    proposal_fields = [field for field in ("purpose", "remarks") if source.get(field)]
                    owners = {"purpose": "source", "remarks": "source"}
                    connection.execute(
                        "INSERT INTO enterprise_invoice_links(company_id,source_system,source_invoice_id,invoice_id,source_hash,source_data_json,field_owners_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (company_id, source_system, source_id, invoice_id, digest, json.dumps(source, ensure_ascii=False, sort_keys=True), json.dumps(owners), now, now),
                    )
                    connection.execute(
                        "INSERT INTO enterprise_invoice_extra(company_id,invoice_id,currency,source_status,amount_text,unit_price_text,quantity_text,purpose_review_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (company_id, invoice_id, source["currency"], source_status, source["amount"], source["unit_price"], source["quantity"], "pending", now),
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
                            proposal_fields.append(field)
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
                            connection.execute("""UPDATE enterprise_drafts SET status='conflict',draft_revision=draft_revision+1,
                                field_context_json=json_set(field_context_json,'$._conflict_reason',json_object('kind','source_status'))
                                WHERE company_id=? AND invoice_id=? AND status IN ('proposed','clarification')""",
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
                record_row(item, status=status, invoice_id=invoice_id)
                draft_id = self._queue_source_draft(user=user, service=service, import_id=import_id, document_id=document_id,
                    row_number=row_number, company_id=company_id, invoice_id=invoice_id, source=source,
                    fields=proposal_fields, mode=policy["mode"], policy_revision=policy["revision"])
                if draft_id:
                    queued_drafts.append(draft_id)
                    counts["proposals"] += 1
            if policy["mode"] == "trusted_sync":
                retry_rows = connection.execute("""SELECT q.draft_id FROM enterprise_import_drafts q
                    JOIN enterprise_imports i USING(import_id) JOIN enterprise_drafts d USING(draft_id)
                    WHERE i.company_id=? AND i.source_system=? AND i.file_hash=? AND i.import_id<>?
                    AND q.auto_mode='trusted_sync' AND q.policy_revision=? AND d.status='proposed' AND d.draft_revision=1""",
                    (company_id, source_system, preview["file_hash"], import_id, policy["revision"])).fetchall()
                queued_drafts.extend(row["draft_id"] for row in retry_rows)
                counts["retry_proposals"] = len(retry_rows)
            followups = [service._ensure_task(company_id=company_id, period=period, user_id=user["user_id"], invoice_ids=sorted(invoice_ids), origin="source_change") for period, invoice_ids in sorted(recheck_by_period.items())]
            connection.execute("UPDATE enterprise_import_previews SET status='committed',file_content=NULL WHERE preview_id=?", (preview_id,))
            connection.commit()
            automatic = None
            if policy["mode"] == "trusted_sync" and queued_drafts:
                try:
                    automatic = service.confirm_drafts(user=user, draft_ids=queued_drafts,
                        expected_draft_revisions={draft_id: 1 for draft_id in queued_drafts}, automatic_import=True)
                except Exception:
                    logging.getLogger(__name__).exception("自动同步中断，票据导入已提交")
                    automatic = {"status": "interrupted", "summary": "票据已导入；自动同步中断，请查看工作清单或重新导入同一文件继续。"}
            refresh = [service.list_missing(user=user, company_id=company_id, period=period, page=1, page_size=1) for period in sorted(affected_periods)]
            return {"status": "ok", "import_id": import_id, "preview_id": preview_id, "company_id": company_id, "source_system": source_system, "source_policy": policy, "counts": counts, "draft_ids": queued_drafts, "automatic_confirmation": automatic, "issues": issues, "period_refresh": [{"period": item.get("period"), "status": item.get("status"), "missing_count": item.get("missing_count"), "summary": item.get("summary")} for item in refresh], "source_change_followups": [item["task_id"] for item in followups]}
        except Exception:
            connection.rollback()
            raise

    def list_identity_reviews(self, *, user: dict, company_id: str, status: str = "pending", page: int = 1,
                              page_size: int = 50) -> dict:
        from .auth import require_role

        require_role(self.database, user, company_id, {"accountant", "reviewer"})
        if status not in {"pending", "all", "open", "deferred", "linked", "new_invoice", "non_invoice", "rejected"}:
            raise ValueError("身份核对状态不受支持。")
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        where = "r.company_id=?"
        params: tuple[Any, ...] = (company_id,)
        if status == "pending":
            where += " AND status IN ('open','deferred')"
        elif status != "all":
            where += " AND status=?"
            params += (status,)
        total = int(self.database.connection.execute(f"SELECT COUNT(*) FROM enterprise_identity_reviews r WHERE {where}", params).fetchone()[0])
        rows = self.database.connection.execute(
            f"""SELECT r.*,d.filename,d.original_available,d.file_hash AS stored_file_hash
                FROM enterprise_identity_reviews r JOIN enterprise_source_documents d USING(document_id)
                WHERE {where} ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'deferred' THEN 1 ELSE 2 END,r.created_at DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"status": "ok", "company_id": company_id, "page": page, "page_size": page_size, "total": total,
                "items": [self._identity_review_view(dict(row), include_aliases=True) for row in rows]}

    def identity_review_detail(self, *, user: dict, identity_review_id: str) -> dict:
        from .auth import require_role

        row = self.database.connection.execute(
            """SELECT r.*,d.filename,d.original_available,d.file_hash AS stored_file_hash
               FROM enterprise_identity_reviews r JOIN enterprise_source_documents d USING(document_id)
               WHERE r.identity_review_id=?""", (identity_review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("身份核对记录不存在。")
        require_role(self.database, user, row["company_id"], {"accountant", "reviewer"})
        return self._identity_review_view(dict(row), include_aliases=True)

    def identity_review_original(self, *, user: dict, identity_review_id: str) -> tuple[str, str, str, bytes] | None:
        from .auth import require_role

        review = self.database.connection.execute(
            "SELECT company_id,document_id FROM enterprise_identity_reviews WHERE identity_review_id=?",
            (identity_review_id,),
        ).fetchone()
        if review is None:
            raise ValueError("身份核对记录不存在。")
        require_role(self.database, user, review["company_id"], {"accountant", "reviewer"})
        row = self.database.connection.execute(
            "SELECT filename,media_type,file_hash,file_content,original_available FROM enterprise_source_documents WHERE document_id=? AND company_id=?",
            (review["document_id"], review["company_id"]),
        ).fetchone()
        if not row["original_available"] or row["file_content"] is None:
            return None
        return row["filename"], row["media_type"], row["file_hash"], bytes(row["file_content"])

    def source_document_original(self, *, user: dict, document_id: str) -> tuple[str, str, str, bytes] | None:
        from .auth import require_role

        row = self.database.connection.execute(
            "SELECT company_id FROM enterprise_source_documents WHERE document_id=?", (document_id,),
        ).fetchone()
        if row is None:
            raise ValueError("来源原件不存在。")
        require_role(self.database, user, row["company_id"], {"accountant", "reviewer"})
        row = self.database.connection.execute(
            """SELECT filename,media_type,file_hash,file_content,original_available
               FROM enterprise_source_documents WHERE document_id=? AND company_id=?""",
            (document_id, row["company_id"]),
        ).fetchone()
        if not row["original_available"] or row["file_content"] is None:
            return None
        return row["filename"], row["media_type"], row["file_hash"], bytes(row["file_content"])

    def _identity_review_view(self, review: dict[str, Any], *, include_aliases: bool) -> dict[str, Any]:
        connection = self.database.connection
        source = json.loads(review["source_json"])
        locator = json.loads(review["locator_json"])
        candidates: dict[str, str] = {}
        tokens = _weak_identity_tokens(source)
        for row in connection.execute(
            "SELECT invoice_id,source_data_json FROM enterprise_invoice_links WHERE company_id=?",
            (review["company_id"],),
        ):
            if tokens & _weak_identity_tokens(json.loads(row["source_data_json"])):
                candidates[row["invoice_id"]] = "来源编号或发票号码相符"
        for row in connection.execute(
            "SELECT invoice_id,source_json FROM enterprise_source_aliases WHERE company_id=? AND invoice_id IS NOT NULL",
            (review["company_id"],),
        ):
            if tokens & _weak_identity_tokens(json.loads(row["source_json"])):
                candidates[row["invoice_id"]] = "其他来源的票据编号相符"
        key = _document_identity_key(source)
        if key:
            match = connection.execute(
                "SELECT invoice_id FROM enterprise_document_identities WHERE company_id=? AND invoice_code=? AND invoice_number=?",
                (review["company_id"], key[0], key[1]),
            ).fetchone()
            if match:
                candidates[match["invoice_id"]] = "发票代码与号码相符"
        if not candidates and source.get("seller_name"):
            for row in connection.execute(
                """SELECT i.invoice_id,i.issue_date,i.seller_name,x.amount_text,x.currency
                   FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                   WHERE i.company_id=?""", (review["company_id"],),
            ):
                if (row["issue_date"] == source.get("issue_date") and row["seller_name"] == source.get("seller_name")
                        and row["currency"] == source.get("currency") and Decimal(row["amount_text"]) == Decimal(source.get("amount", "0"))):
                    candidates[row["invoice_id"]] = "日期、销售方和金额相符"
        candidate_rows = []
        for invoice_id, match_reason in candidates.items():
            row = connection.execute(
                """SELECT i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.amount_cents,x.amount_text,x.currency,
                          l.source_system,l.source_invoice_id
                   FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                   JOIN enterprise_invoice_links l USING(company_id,invoice_id)
                   WHERE i.company_id=? AND i.invoice_id=?""", (review["company_id"], invoice_id),
            ).fetchone()
            if row:
                candidate_rows.append({**dict(row), "match_reason": match_reason})
        result = {"identity_review_id": review["identity_review_id"], "company_id": review["company_id"],
                  "import_id": review["import_id"], "row_number": review["row_number"],
                  "source_system": review["source_system"], "source_invoice_id": review["source_invoice_id"],
                  "content_hash": review["content_hash"], "file_hash": review["file_hash"],
                  "filename": review["filename"], "original_available": bool(review["original_available"]),
                  "source": source, "locator": locator, "reason": review["reason"], "status": review["status"],
                  "revision": review["revision"], "invoice_id": review["invoice_id"],
                  "resolution_action": review["resolution_action"], "resolution_note": review["resolution_note"],
                  "candidates": candidate_rows, "created_at": review["created_at"], "updated_at": review["updated_at"]}
        if include_aliases:
            aliases = connection.execute(
                """SELECT a.source_system,a.source_invoice_id,a.invoice_id,a.row_number,a.locator_json,a.status,
                          d.document_id,d.filename,d.file_hash,d.original_available
                   FROM enterprise_source_aliases a JOIN enterprise_source_documents d USING(document_id)
                   WHERE a.identity_review_id=? AND a.company_id=? ORDER BY a.created_at,a.alias_id""",
                (review["identity_review_id"], review["company_id"]),
            ).fetchall()
            result["aliases"] = [{**dict(alias), "locator": json.loads(alias["locator_json"])} for alias in aliases]
        return result

    def resolve_identity_review(self, *, user: dict, identity_review_id: str, action: str,
                                expected_revision: int, command_id: str, invoice_id: str | None = None,
                                reason: str = "") -> dict:
        from .auth import require_role
        from .service import invoice_key
        from .storage import utc_now

        if not isinstance(action, str) or action not in {"associate", "new_invoice", "non_invoice", "reject", "defer"}:
            raise ValueError("处置动作必须是关联已有票据、认定新票、非票据、拒收/退回或暂缓。")
        if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
            raise ValueError("处置请求必须带 1 到 200 个字符的幂等编号。")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision 必须是正整数。")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("处置说明最多 1000 个字符。")
        if invoice_id is not None and not isinstance(invoice_id, str):
            raise ValueError("invoice_id 必须是票据编号文本。")
        if action in {"non_invoice", "reject", "defer"} and not reason.strip():
            raise ValueError("请填写非票据说明、拒收原因或暂缓事项。")
        connection = self.database.connection
        request_json = json.dumps({"action": action, "invoice_id": invoice_id, "reason": reason,
                                   "expected_revision": expected_revision}, ensure_ascii=False, sort_keys=True)
        connection.execute("BEGIN IMMEDIATE")
        try:
            review = connection.execute("SELECT * FROM enterprise_identity_reviews WHERE identity_review_id=?", (identity_review_id,)).fetchone()
            if review is None:
                raise ValueError("身份核对记录不存在。")
            require_role(self.database, user, review["company_id"], {"accountant", "reviewer"})
            if action == "new_invoice":
                from ..services import TaxService
                require_role(self.database, user, review["company_id"], {"accountant"})
                TaxService(self.database).require_access(user_id=user["user_id"], company_id=review["company_id"], modify=True)
            prior_action = connection.execute("SELECT * FROM enterprise_identity_review_actions WHERE command_id=?", (command_id,)).fetchone()
            if prior_action:
                if prior_action["identity_review_id"] != identity_review_id or prior_action["request_json"] != request_json:
                    raise ValueError("幂等编号已用于另一项处置请求。")
                connection.commit()
                return json.loads(prior_action["result_json"])
            if review["revision"] != expected_revision:
                raise IdentityReviewConflict("身份核对内容已更新，请刷新后重新选择处置方式。")
            if review["status"] not in {"open", "deferred"}:
                raise IdentityReviewConflict("此身份核对事项已办结，请刷新当前状态。")

            resolved_invoice_id = None
            if action == "associate":
                if not invoice_id:
                    raise ValueError("关联已有票据时必须选择目标票据。")
                target = connection.execute(
                    """SELECT i.invoice_id FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                       JOIN enterprise_invoice_links l USING(company_id,invoice_id)
                       WHERE i.company_id=? AND i.invoice_id=?""", (review["company_id"], invoice_id),
                ).fetchone()
                if target is None:
                    raise ValueError("目标票据不存在或不属于当前企业。")
                resolved_invoice_id = target["invoice_id"]
                connection.execute(
                    "UPDATE enterprise_source_aliases SET invoice_id=?,status='linked' WHERE identity_review_id=? AND company_id=?",
                    (resolved_invoice_id, identity_review_id, review["company_id"]),
                )
                connection.execute(
                    """UPDATE enterprise_import_rows SET invoice_id=?,status='identity_linked'
                       WHERE identity_review_id=? AND company_id=?""", (resolved_invoice_id, identity_review_id, review["company_id"]),
                )
            elif action == "new_invoice":
                source = json.loads(review["source_json"])
                resolved_invoice_id = invoice_key(review["company_id"], "identity-review", identity_review_id)
                connection.execute(
                    """INSERT INTO invoices(company_id,invoice_id,issue_date,seller_name,project_name,amount_cents,purpose,remarks,revision)
                       VALUES(?,?,?,?,?,?,?,?,1)""",
                    (review["company_id"], resolved_invoice_id, source["issue_date"], source.get("seller_name", ""),
                     source.get("project_name", ""), int(source["amount_cents"]), None, None),
                )
                source_status = source.get("source_status", "active")
                owners = {"purpose": "source", "remarks": "source"}
                existing_main = connection.execute(
                    """SELECT 1 FROM enterprise_invoice_links WHERE company_id=? AND source_system=? AND source_invoice_id=?""",
                    (review["company_id"], review["source_system"], review["source_invoice_id"]),
                ).fetchone()
                main_source_system = "identity_review" if existing_main else review["source_system"]
                main_source_id = identity_review_id if existing_main else review["source_invoice_id"]
                connection.execute(
                    """INSERT INTO enterprise_invoice_links(company_id,source_system,source_invoice_id,invoice_id,source_hash,
                       source_data_json,field_owners_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (review["company_id"], main_source_system, main_source_id, resolved_invoice_id, review["content_hash"],
                     json.dumps(source, ensure_ascii=False, sort_keys=True), json.dumps(owners), utc_now(), utc_now()),
                )
                connection.execute(
                    """INSERT INTO enterprise_invoice_extra(company_id,invoice_id,currency,source_status,amount_text,unit_price_text,
                       quantity_text,purpose_review_status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (review["company_id"], resolved_invoice_id, source["currency"], source_status, source["amount"],
                     source.get("unit_price"), source.get("quantity"), "pending", utc_now()),
                )
                key = _document_identity_key(source)
                if key:
                    connection.execute(
                        """INSERT OR IGNORE INTO enterprise_document_identities
                           (company_id,invoice_code,invoice_number,invoice_id,seller_tax_id,created_at) VALUES(?,?,?,?,?,?)""",
                        (review["company_id"], key[0], key[1], resolved_invoice_id, source.get("seller_tax_id", ""), utc_now()),
                    )
                connection.execute(
                    "UPDATE enterprise_source_aliases SET invoice_id=?,status='linked' WHERE identity_review_id=? AND company_id=?",
                    (resolved_invoice_id, identity_review_id, review["company_id"]),
                )
                connection.execute(
                    """UPDATE enterprise_import_rows SET invoice_id=?,status='identity_linked'
                       WHERE identity_review_id=? AND company_id=?""", (resolved_invoice_id, identity_review_id, review["company_id"]),
                )

            draft_id = None
            auto_mode = "review"
            if resolved_invoice_id and action in {"associate", "new_invoice"}:
                from .service import EnterpriseService

                source = json.loads(review["source_json"])
                policy = self.source_policy(user=user, company_id=review["company_id"], source_system=review["source_system"])
                auto_mode = "review" if action == "associate" else policy["mode"]
                service = EnterpriseService(self.database)
                draft_id = self._queue_source_draft(user=user, service=service, import_id=review["import_id"],
                    document_id=review["document_id"], row_number=review["row_number"], company_id=review["company_id"],
                    invoice_id=resolved_invoice_id, source=source, fields=["purpose", "remarks"],
                    mode=auto_mode, policy_revision=policy["revision"])

            status = {"associate": "linked", "new_invoice": "new_invoice", "non_invoice": "non_invoice",
                      "reject": "rejected", "defer": "deferred"}[action]
            if action in {"non_invoice", "reject"}:
                alias_status = "non_invoice" if action == "non_invoice" else "rejected"
                connection.execute("UPDATE enterprise_source_aliases SET status=? WHERE identity_review_id=? AND company_id=?",
                                   (alias_status, identity_review_id, review["company_id"]))
                connection.execute("UPDATE enterprise_import_rows SET status=? WHERE identity_review_id=? AND company_id=?",
                                   (status, identity_review_id, review["company_id"]))
            revision = expected_revision + 1
            now = utc_now()
            result = {"status": "ok", "identity_review_id": identity_review_id, "action": action,
                      "review_status": status, "revision": revision, "invoice_id": resolved_invoice_id,
                      "command_id": command_id, "draft_id": draft_id}
            connection.execute(
                """UPDATE enterprise_identity_reviews SET status=?,revision=?,invoice_id=?,resolution_action=?,resolution_note=?,
                   resolved_by=?,resolved_at=?,last_command_id=?,last_result_json=?,updated_at=?
                   WHERE identity_review_id=? AND revision=? AND status IN ('open','deferred')""",
                (status, revision, resolved_invoice_id, action, reason.strip(), user["user_id"],
                 now if status not in {"deferred"} else None, command_id, json.dumps(result, ensure_ascii=False, sort_keys=True),
                 now, identity_review_id, expected_revision),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise IdentityReviewConflict("身份核对内容已更新，请刷新后重新选择处置方式。")
            connection.execute(
                """INSERT INTO enterprise_identity_review_actions
                   (action_id,identity_review_id,command_id,actor_user_id,action,request_json,result_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                ("identity-action-" + uuid4().hex, identity_review_id, command_id, user["user_id"], action,
                 request_json, json.dumps(result, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
            if draft_id and auto_mode == "trusted_sync":
                try:
                    result["automatic_confirmation"] = service.confirm_drafts(user=user, draft_ids=[draft_id],
                        expected_draft_revisions={draft_id: 1}, automatic_import=True)
                except Exception:
                    logging.getLogger(__name__).exception("身份核对已保存，但自动同步中断")
                    result["automatic_confirmation"] = {"status": "interrupted", "summary": "身份核对已保存，自动同步中断；请查看工作清单或重新导入同一文件继续。"}
                encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
                connection.execute("UPDATE enterprise_identity_review_actions SET result_json=? WHERE command_id=?", (encoded, command_id))
                connection.execute("UPDATE enterprise_identity_reviews SET last_result_json=? WHERE identity_review_id=?", (encoded, identity_review_id))
            return result
        except Exception:
            connection.rollback()
            raise

    def resolve_conflict(self, *, user: dict, conflict_id: str, resolution: str, expected_revision: int | None = None) -> dict:
        from .auth import require_role
        from .storage import utc_now
        from ..services import TaxService

        if resolution not in {"keep_local", "accept_source"}:
            raise ValueError("resolution 必须是 keep_local 或 accept_source。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            conflict = connection.execute("SELECT * FROM enterprise_import_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
            if conflict is None:
                raise ValueError("冲突不存在。")
            require_role(self.database, user, conflict["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=conflict["company_id"], modify=True)
            if conflict["field_name"] not in {"purpose", "remarks"}:
                raise ValueError("该冲突字段不支持处理。")
            current = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (conflict["company_id"], conflict["invoice_id"])).fetchone()
            if current is None:
                raise ValueError("来源发票已经不存在。")
            if conflict["status"] != "open":
                if conflict["resolved_by"] == user["user_id"] and conflict["resolution"] == resolution:
                    connection.rollback()
                    return {"status": "already_resolved", "conflict_id": conflict_id, "resolution": resolution,
                            "current_value": current[conflict["field_name"]], "current_revision": current["revision"]}
                raise ValueError("冲突已经处理，请查看当前结果。")
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
                raise ValueError("请提供有效的当前发票版本。")
            if expected_revision is None:
                if resolution == "accept_source":
                    raise ValueError("请先查看当前发票内容和版本，再决定是否采用来源值。")
                if current[conflict["field_name"]] != conflict["current_value"]:
                    raise ImportConflictChanged("发票内容已变化，请刷新后重新处理冲突。")
            elif current["revision"] != expected_revision:
                raise ImportConflictChanged("发票内容已变化，请刷新后重新处理冲突。")
            if resolution == "keep_local":
                connection.execute("""UPDATE enterprise_drafts SET status='rejected',draft_revision=draft_revision+1
                    WHERE draft_id IN (SELECT draft_id FROM enterprise_import_drafts WHERE conflict_id=?)
                    AND status IN ('proposed','clarification','conflict')""", (conflict_id,))
                connection.execute("UPDATE enterprise_import_conflicts SET status='resolved',resolved_by=?,resolution=?,resolved_at=? WHERE conflict_id=?",
                                   (user["user_id"], resolution, utc_now(), conflict_id))
                connection.commit()
                return {"status": "resolved", "conflict_id": conflict_id, "resolution": resolution}

            from .service import EnterpriseService

            service = EnterpriseService(self.database)
            prior = connection.execute("""SELECT d.draft_id,d.draft_revision FROM enterprise_import_drafts q
                JOIN enterprise_drafts d USING(draft_id) WHERE q.conflict_id=? AND d.status IN ('proposed','conflict')
                ORDER BY d.created_at DESC LIMIT 1""", (conflict_id,)).fetchone()
            if prior:
                draft_id, draft_revision = prior["draft_id"], prior["draft_revision"]
            else:
                row = connection.execute("""SELECT r.row_number,r.source_json,d.document_id FROM enterprise_import_rows r
                    JOIN enterprise_source_documents d USING(import_id) WHERE r.import_id=? AND r.company_id=? AND r.invoice_id=?
                    AND json_extract(r.source_json, '$.' || ?)=? ORDER BY r.row_number LIMIT 1""",
                    (conflict["import_id"], conflict["company_id"], conflict["invoice_id"],
                     conflict["field_name"], conflict["incoming_source_value"])).fetchone()
                if row is None:
                    raise ValueError("原始来源记录不可用，请重新导入后处理。")
                draft_id = self._queue_source_draft(user=user, service=service, import_id=conflict["import_id"],
                    document_id=row["document_id"], row_number=row["row_number"], company_id=conflict["company_id"],
                    invoice_id=conflict["invoice_id"], source=json.loads(row["source_json"]), fields=[conflict["field_name"]],
                    mode="review", policy_revision=0, conflict_id=conflict_id)
                draft_revision = 1
            if draft_id is None:
                raise ValueError("来源值已是当前值，请刷新后保留当前内容。")
            connection.commit()
            confirmed = service.confirm_drafts(user=user, draft_ids=[draft_id],
                expected_draft_revisions={draft_id: draft_revision}, conflict_revisions_by_draft={draft_id: expected_revision})
            item = confirmed["items"][0]
            if item["status"] not in {"committed", "duplicate", "no_change"}:
                return {"status": item["status"], "conflict_id": conflict_id, "draft_id": draft_id,
                        "summary": item.get("summary", "来源值尚未保存，请查看待处理草稿。")}
            return {"status": "resolved", "conflict_id": conflict_id, "resolution": resolution,
                    "draft_id": draft_id, "confirmation": item}
        except Exception:
            connection.rollback()
            raise


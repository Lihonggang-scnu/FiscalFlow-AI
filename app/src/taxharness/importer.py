"""Read-only importer for the competition workbook."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import InvalidInputError
from .models import WorkbookData
from .money import amount_to_cents


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _text(value)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T].*)?$", raw)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError as error:
            raise InvalidInputError(f"日期不是有效日历日期：{raw!r}") from error
    raise InvalidInputError(f"日期格式无法识别：{raw!r}")


def _rows(sheet) -> list[list[Any]]:
    return [list(row) for row in sheet.iter_rows(values_only=True)]


def _header(rows: list[list[Any]], required: set[str]) -> tuple[int, dict[str, int]]:
    for index, row in enumerate(rows):
        mapping = {_text(value): column for column, value in enumerate(row) if _text(value)}
        if required <= set(mapping):
            return index, mapping
    raise InvalidInputError(f"工作表缺少字段：{sorted(required)}")


def _cell(row: list[Any], mapping: dict[str, int], name: str) -> Any:
    position = mapping.get(name)
    return row[position] if position is not None and position < len(row) else None


def _permission_value(value: Any) -> tuple[bool, bool]:
    text = _text(value)
    if text == "可查看并修改":
        return True, True
    if text == "仅查看":
        return True, False
    if text == "无权访问":
        return False, False
    raise InvalidInputError(f"未知权限值：{text!r}")


def _company_id_from_header(header: str) -> str | None:
    match = re.search(r"[（(](C\d+)[）)]", header)
    return match.group(1) if match else None


def _load_openpyxl(path: Path):
    try:
        from openpyxl import load_workbook
    except ImportError as error:
        raise InvalidInputError("当前解释器缺少 openpyxl，无法读取官方 Excel。") from error
    if not path.is_file():
        raise InvalidInputError(f"官方 Excel 不存在：{path}")
    return load_workbook(path, read_only=True, data_only=True)


def load_workbook_data(path: str | Path) -> WorkbookData:
    """Load all competition rows without writing to the source workbook."""

    source = Path(path).expanduser().resolve()
    workbook = _load_openpyxl(source)
    try:
        missing = {"企业与用户", "发票", "银行流水"} - set(workbook.sheetnames)
        if missing:
            raise InvalidInputError(f"Excel 缺少工作表：{sorted(missing)}")
        companies, users, permissions = _load_identity(_rows(workbook["企业与用户"]))
        invoices = _load_invoices(_rows(workbook["发票"]))
        bank_records = _load_bank(_rows(workbook["银行流水"]))
    finally:
        workbook.close()
    return WorkbookData(
        source_path=str(source),
        source_sha256=file_sha256(source),
        companies=companies,
        users=users,
        permissions=permissions,
        invoices=invoices,
        bank_records=bank_records,
    )


def _load_identity(rows: list[list[Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    company_index, company_headers = _header(rows, {"企业编号", "企业名称"})
    companies = []
    for row in rows[company_index + 1 :]:
        company_id = _text(_cell(row, company_headers, "企业编号"))
        name = _text(_cell(row, company_headers, "企业名称"))
        if not re.fullmatch(r"C\d+", company_id):
            continue
        companies.append({"company_id": company_id, "name": name, "short_name": _text(_cell(row, company_headers, "常用简称"))})
    user_index, user_headers = _header(rows, {"用户编号", "用户名称"})
    users, permissions = [], []
    company_columns = {
        _company_id_from_header(header): column
        for header, column in user_headers.items()
        if _company_id_from_header(header)
    }
    for row in rows[user_index + 1 :]:
        user_id = _text(_cell(row, user_headers, "用户编号"))
        if not re.fullmatch(r"U\d+", user_id):
            continue
        users.append({"user_id": user_id, "name": _text(_cell(row, user_headers, "用户名称"))})
        for company_id, column in company_columns.items():
            can_view, can_modify = _permission_value(row[column] if column < len(row) else None)
            permissions.append({"user_id": user_id, "company_id": company_id, "can_view": can_view, "can_modify": can_modify})
    return companies, users, permissions


def _load_invoices(rows: list[list[Any]]) -> list[dict]:
    _, headers = _header(rows, {"企业编号", "发票编号", "开票日期", "价税合计（元）"})
    result = []
    for row in rows[_header(rows, {"企业编号", "发票编号", "开票日期", "价税合计（元）"})[0] + 1 :]:
        company_id = _text(_cell(row, headers, "企业编号"))
        invoice_id = _text(_cell(row, headers, "发票编号"))
        if not re.fullmatch(r"C\d+", company_id) or not invoice_id:
            continue
        result.append({
            "company_id": company_id,
            "invoice_id": invoice_id,
            "issue_date": _date_text(_cell(row, headers, "开票日期")),
            "seller_name": _text(_cell(row, headers, "销售方名称")),
            "project_name": _text(_cell(row, headers, "项目名称（票面）")),
            "amount_cents": amount_to_cents(_cell(row, headers, "价税合计（元）")),
            "purpose": _nullable_text(_cell(row, headers, "用途")),
            "remarks": _nullable_text(_cell(row, headers, "备注")),
        })
    return result


def _load_bank(rows: list[list[Any]]) -> list[dict]:
    header_index, headers = _header(rows, {"企业编号", "流水编号", "交易日期", "收支方向", "金额（元）"})
    result = []
    for row in rows[header_index + 1 :]:
        company_id = _text(_cell(row, headers, "企业编号"))
        transaction_id = _text(_cell(row, headers, "流水编号"))
        direction = _text(_cell(row, headers, "收支方向"))
        if not re.fullmatch(r"C\d+", company_id) or not transaction_id:
            continue
        if direction not in {"收入", "支出"}:
            raise InvalidInputError(f"未知收支方向：{direction!r}")
        result.append({
            "company_id": company_id,
            "transaction_id": transaction_id,
            "transaction_date": _date_text(_cell(row, headers, "交易日期")),
            "direction": direction,
            "amount_cents": amount_to_cents(_cell(row, headers, "金额（元）")),
            "counterparty": _text(_cell(row, headers, "对方名称")),
            "remarks": _nullable_text(_cell(row, headers, "备注")),
        })
    return result


def _nullable_text(value: Any) -> str | None:
    text = _text(value)
    return text if text else None

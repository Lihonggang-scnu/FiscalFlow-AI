"""Extract structured Chinese e-invoice data from XML and OFD attachments."""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree


def _field(values: dict[str, list[str]], *names: str) -> str:
    for name in names:
        for value in values.get(name.casefold(), []):
            if value.strip():
                return value.strip()
    return ""


def _money(value: str, label: str) -> Decimal:
    raw = value.replace(",", "").replace("¥", "").replace("￥", "").strip()
    try:
        amount = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{label}不是有效金额。") from error
    if not amount.is_finite():
        raise ValueError(f"{label}不是有限金额。")
    return amount


def _invoice_xml(content: bytes) -> dict[str, str] | None:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("票据 XML 含不支持的文档类型或实体声明。")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise ValueError("票据 XML 格式无法解析。") from error
    values: dict[str, list[str]] = {}
    for element in root.iter():
        namespace = str(element.tag).split("}")[0].lstrip("{") if "}" in str(element.tag) else ""
        if namespace.startswith("http://xbrl.mof.gov.cn/taxonomy/") and namespace.rsplit("/", 1)[-1] in {"bker", "bkrs"}:
            raise ValueError("这是银行电子回单或对账单，不是发票，请按银行资料单独处理。")
        tag = str(element.tag).split("}")[-1].casefold()
        if element.text and element.text.strip():
            values.setdefault(tag, []).append(element.text.strip())
    number = _field(values, "InvoiceNumber", "NumberOfInvoice", "UniqueCodeOfInvoice", "Fphm", "发票号码")
    if not number:
        return None
    code = _field(values, "InvoiceCode", "CodeOfInvoice", "Fpdm", "发票代码")
    invoice_type = _field(values, "InvoiceType", "TypeOfInvoice", "Fplx", "发票类型")
    red_flag = _field(values, "RedFlag", "IsRed", "WhetherEinvoiceIsRedEinvoice", "WhetherInvoiceIsRedInvoice", "红字标识", "红蓝字标志").casefold()
    if "红字" in invoice_type or invoice_type.casefold() == "red" or red_flag in {"1", "true", "red", "红字", "是", "y"}:
        raise ValueError("红字票据需要单独核对原票与冲红关系，未导入普通发票。")
    issue_date = _field(values, "IssueTime", "IssueDate", "DateOfIssue", "Kprq", "开票日期")
    total = _field(values, "TotalTaxIncludedAmount", "TotalTax-includedAmount", "TaxIncludedAmountInFigures", "Jshj", "价税合计", "价税合计小写")
    net = _field(values, "TotalAmWithoutTax", "TotalAmountExcludingTax", "Hjje", "不含税金额", "金额合计")
    tax = _field(values, "TotalTaxAm", "TotalTaxAmount", "Hjse", "合计税额", "税额合计")
    if not issue_date or not total:
        raise ValueError("缺少开票日期或价税合计，请核对票据。")
    total_value = _money(total, "价税合计")
    if net and tax and abs(_money(net, "不含税金额") + _money(tax, "税额") - total_value) > Decimal("0.01"):
        raise ValueError("不含税金额与税额之和不等于价税合计，请人工核对。")
    if total_value < 0:
        raise ValueError("红字或负金额票据需要单独核对原票与冲红关系，未导入普通发票。")
    seller = _field(values, "SellerName", "NameOfSeller", "XsfMc", "销售方名称")
    seller_tax_id = _field(values, "SellerTaxNo", "SellerIdNum", "TaxpayerIdentificationNumberUnifiedSocialCreditCodeOfSeller", "XsfNsrsbh", "销售方纳税人识别号")
    buyer = _field(values, "BuyerName", "NameOfPurchaser", "GmfMc", "购买方名称")
    buyer_tax_id = _field(values, "BuyerTaxNo", "TaxpayerIdentificationNumberUnifiedSocialCreditCodeOfPurchaser", "GmfNsrsbh", "购买方纳税人识别号")
    item = _field(values, "ItemName", "NameOfTaxableGoodsOrServices", "Xmmc", "项目名称", "货物或应税劳务名称")
    raw_currency = _field(values, "Currency", "币种") or "CNY"
    currency = "CNY" if raw_currency in {"人民币", "RMB", "¥", "￥"} else raw_currency.upper()
    result = {"source_invoice_id": f"{code}-{number}" if code else number, "issue_date": issue_date,
            "amount": format(total_value, "f"), "currency": currency, "seller_name": seller, "project_name": item,
            "invoice_number": number, "invoice_code": code, "seller_tax_id": seller_tax_id,
            "buyer_name": buyer, "buyer_tax_id": buyer_tax_id, "net_amount": net, "tax_amount": tax,
            "accounting_period": _field(values, "AccountingPeriod", "会计期间"),
            "booked_status": _field(values, "WhetherEinvoiceHasBeenBooked", "WhetherInvoiceHasBeenBooked"),
            "checked_status": _field(values, "WhetherEinvoiceHasBeenChecked", "WhetherInvoiceHasBeenChecked"),
            "paid_status": _field(values, "WhetherEinvoiceHasBeenPaid", "WhetherInvoiceHasBeenPaid")}
    status = _field(values, "InvoiceStatus", "Fpzt", "发票状态")
    if status:
        result["invoice_status"] = status
    return result


def read_structured_invoice(filename: str, content: bytes) -> tuple[list[dict[str, str]], list[dict]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xml":
        try:
            record = _invoice_xml(content)
            if record is None:
                raise ValueError("未找到发票号码，请核对 XML 是否为票据数据。")
            return [record], []
        except ValueError as error:
            return [], [{"row_number": 1, "source_invoice_id": None, "issue_code": "invalid_document", "details": str(error)}]
    if suffix != ".ofd":
        raise ValueError("仅支持结构化 XML 或包含票据 XML 的 OFD 文件。")
    records, issues = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            candidates = [info for info in archive.infolist() if info.filename.lower().endswith(".xml") and not info.is_dir()]
            for index, info in enumerate(candidates, 1):
                if info.file_size > 10 * 1024 * 1024:
                    issues.append({"row_number": index, "source_invoice_id": None, "issue_code": "invalid_document", "details": "OFD 内部 XML 过大。"})
                    continue
                try:
                    record = _invoice_xml(archive.read(info))
                    if record is not None:
                        records.append(record)
                except ValueError as error:
                    if "invoice" in info.filename.casefold() or "fapiao" in info.filename.casefold():
                        issues.append({"row_number": index, "source_invoice_id": None, "issue_code": "invalid_document", "details": str(error)})
    except zipfile.BadZipFile as error:
        raise ValueError("OFD 文件无法打开，请核对文件。") from error
    if not records and not issues:
        issues.append({"row_number": 1, "source_invoice_id": None, "issue_code": "structured_data_missing", "details": "OFD 中没有可读取的结构化发票数据。"})
    return records, issues

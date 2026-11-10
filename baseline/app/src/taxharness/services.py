"""Deterministic tax queries and the only stage-one business write."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from .errors import InvalidInputError, NotFoundError, PermissionDeniedError, RuleConflictError, RuleExecutionError, RuleNotFoundError
from .money import cents_to_text
from .rules import applicable_rule, applicable_rules, evaluate_invoice_purpose
from .safe_json import safe_case_json
from .storage import TaxDatabase, row_dict


def _month_range(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise InvalidInputError("月份必须是 YYYY-MM。")
    year, month_number = (int(item) for item in month.split("-"))
    start = date(year, month_number, 1)
    if month_number == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month_number + 1, 1)
    return start.isoformat(), end.isoformat()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _invoice_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": row["company_id"],
        "invoice_id": row["invoice_id"],
        "issue_date": row["issue_date"],
        "seller_name": row["seller_name"],
        "project_name": row["project_name"],
        "amount": cents_to_text(row["amount_cents"]),
        "purpose": row["purpose"],
        "remarks": row["remarks"],
        "revision": row["revision"],
    }


class TaxService:
    """Services accept explicit identity; no model or implicit session is involved."""

    def __init__(self, database: TaxDatabase):
        self.database = database

    def _permission(self, user_id: str, company_id: str, *, modify: bool = False) -> dict:
        row = self.database.connection.execute(
            """SELECT user_id,company_id,can_view,can_modify
               FROM user_company_permissions WHERE user_id=? AND company_id=?""",
            (user_id, company_id),
        ).fetchone()
        if row is None or not bool(row["can_view"]):
            raise PermissionDeniedError(f"用户 {user_id} 无权访问企业 {company_id}。")
        if modify and not bool(row["can_modify"]):
            raise PermissionDeniedError(f"用户 {user_id} 对企业 {company_id} 仅有查看权限，无权修改发票。")
        return dict(row)

    def require_access(self, *, user_id: str, company_id: str, modify: bool = False) -> dict:
        """Public boundary for other deterministic modules using this service."""

        return self._permission(user_id, company_id, modify=modify)

    def query_invoice_summary(self, *, user_id: str, company_id: str, month: str) -> dict[str, Any]:
        self._permission(user_id, company_id)
        start, end = _month_range(month)
        rows = self.database.connection.execute(
            """SELECT * FROM invoices WHERE company_id=? AND issue_date>=? AND issue_date<?
               ORDER BY issue_date, invoice_id""",
            (company_id, start, end),
        ).fetchall()
        total = sum(int(row["amount_cents"]) for row in rows)
        return {
            "status": "ok",
            "data": {
                "company_id": company_id,
                "month": month,
                "invoice_count": len(rows),
                "invoice_total": cents_to_text(total),
                "no_records": not rows,
                "invoices": [_invoice_view(dict(row)) for row in rows],
                "rules": [applicable_rule(self.database.connection, kind="amount_statistics", company_id=company_id, month=month)],
            },
        }

    def query_bank_summary(self, *, user_id: str, company_id: str, month: str) -> dict[str, Any]:
        self._permission(user_id, company_id)
        start, end = _month_range(month)
        rows = self.database.connection.execute(
            """SELECT * FROM bank_records WHERE company_id=? AND transaction_date>=? AND transaction_date<?
               ORDER BY transaction_date, transaction_id""",
            (company_id, start, end),
        ).fetchall()
        income = sum(int(row["amount_cents"]) for row in rows if row["direction"] == "收入")
        expense = sum(int(row["amount_cents"]) for row in rows if row["direction"] == "支出")
        return {
            "status": "ok",
            "data": {
                "company_id": company_id,
                "month": month,
                "record_count": len(rows),
                "income": cents_to_text(income),
                "expense": cents_to_text(expense),
                "no_records": not rows,
                "rules": [applicable_rule(self.database.connection, kind="bank_requirements", company_id=company_id, month=month)],
            },
        }

    def read_invoice(self, *, user_id: str, company_id: str, invoice_id: str, month: str | None = None) -> dict[str, Any]:
        """Read one current invoice, including the trusted revision used for saving."""

        self._permission(user_id, company_id)
        if month is not None:
            summary = self.query_invoice_summary(user_id=user_id, company_id=company_id, month=month)
            for invoice in summary["data"]["invoices"]:
                if invoice["invoice_id"] == invoice_id:
                    return {"status": "ok", "data": invoice}
            return {"status": "not_found", "summary": "未找到该企业和月份下的发票。", "data": {"company_id": company_id, "invoice_id": invoice_id, "month": month}}
        row = self.database.connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, invoice_id)).fetchone()
        if row is None:
            return {"status": "not_found", "summary": "未找到该企业下的发票。", "data": {"company_id": company_id, "invoice_id": invoice_id}}
        return {"status": "ok", "data": _invoice_view(dict(row))}

    def query_all(self, *, user_id: str, company_id: str, month: str) -> dict[str, Any]:
        invoice = self.query_invoice_summary(user_id=user_id, company_id=company_id, month=month)
        bank = self.query_bank_summary(user_id=user_id, company_id=company_id, month=month)
        return {"status": "ok", "data": {"invoice": invoice["data"], "bank": bank["data"], "rules": applicable_rules(self.database.connection, company_id=company_id, month=month)}}

    def list_missing_purposes(self, *, user_id: str, company_id: str, month: str = "2026-09") -> dict[str, Any]:
        self._permission(user_id, company_id)
        if month != "2026-09":
            return {"status": "out_of_scope", "summary": "初赛用途完整性检查仅限 2026-09。", "data": {"month": month, "allowed_month": "2026-09"}}
        start, end = _month_range(month)
        try:
            rule = applicable_rule(self.database.connection, kind="invoice_purpose", company_id=company_id, month=month)
        except (RuleNotFoundError, RuleConflictError) as error:
            return {"status": error.code, "summary": str(error), "data": {"company_id": company_id, "month": month}}
        if rule.get("execution_status") not in {"supported"}:
            return {
                "status": "cannot_determine",
                "summary": rule.get("execution_summary", "当前规则不能自动判定。"),
                "data": {"company_id": company_id, "month": month, "rule": rule},
            }
        rows = self.database.connection.execute(
            """SELECT * FROM invoices WHERE company_id=? AND issue_date>=? AND issue_date<?
               ORDER BY issue_date, invoice_id""",
            (company_id, start, end),
        ).fetchall()
        missing = []
        for row in rows:
            try:
                evaluated = evaluate_invoice_purpose(rule, int(row["amount_cents"]), row["purpose"])
            except RuleExecutionError as error:
                return {"status": error.code, "summary": str(error), "data": {"company_id": company_id, "month": month, "rule": rule}}
            if evaluated["missing"]:
                missing.append(_invoice_view(dict(row)))
        return {"status": "ok", "data": {"company_id": company_id, "month": month, "rule": rule, "missing": missing, "missing_count": len(missing)}}

    def update_invoice(
        self,
        *,
        user_id: str,
        company_id: str,
        invoice_id: str,
        changes: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Update only purpose/remarks, with scoped idempotent transactional write."""

        self._permission(user_id, company_id, modify=True)
        if not isinstance(changes, dict) or not changes or set(changes) - {"purpose", "remarks"}:
            return {"status": "invalid_input", "summary": "只能修改用途或备注，且至少提供一项。"}
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            return {"status": "invalid_input", "summary": "expected_revision 必须是正整数。"}
        if not self._valid_idempotency_key(idempotency_key, user_id, company_id, invoice_id):
            return {"status": "invalid_idempotency_key", "summary": "幂等键必须绑定用户、企业和发票。"}
        clean_changes = {key: _clean_text(value) for key, value in changes.items()}
        payload = {"changes": clean_changes, "expected_revision": expected_revision}
        clean_payload = safe_case_json(payload)
        payload_hash = hashlib.sha256(json.dumps(clean_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            prior = connection.execute(
                """SELECT * FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice'
                   AND object_id=? AND idempotency_key=?""",
                (user_id, company_id, invoice_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["payload_hash"] != payload_hash:
                    result = {"status": "idempotency_conflict", "summary": "同一幂等键已用于不同 payload，拒绝重放。"}
                    connection.rollback()
                    return result
                original = json.loads(prior["result_json"])
                connection.rollback()
                return {"status": "duplicate", "replayed": True, "original_status": prior["status"], "result": original}
            current = connection.execute(
                "SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, invoice_id)
            ).fetchone()
            if current is None:
                result = {"status": "not_found", "summary": "未找到该企业下的发票。"}
                self._record_operation(connection, user_id, company_id, invoice_id, idempotency_key, payload_hash, result["status"], result, "update_rejected", payload)
                connection.commit()
                return result
            if current["revision"] != expected_revision:
                result = {"status": "revision_conflict", "summary": "发票版本已变化，拒绝覆盖最新数据。", "current_revision": current["revision"], "expected_revision": expected_revision}
                self._record_operation(connection, user_id, company_id, invoice_id, idempotency_key, payload_hash, result["status"], result, "update_rejected", payload)
                connection.commit()
                return result
            old = dict(current)
            purpose = clean_changes.get("purpose", old["purpose"])
            remarks = clean_changes.get("remarks", old["remarks"])
            if purpose == old["purpose"] and remarks == old["remarks"]:
                result = {"status": "no_change", "committed": False, "summary": "当前已经是这个值，本次无需再修改。", "current": _invoice_view(old)}
                self._record_operation(connection, user_id, company_id, invoice_id, idempotency_key, payload_hash, result["status"], result, "update_noop", payload)
                connection.commit()
                return result
            connection.execute(
                "UPDATE invoices SET purpose=?,remarks=?,revision=revision+1 WHERE company_id=? AND invoice_id=? AND revision=?",
                (purpose, remarks, company_id, invoice_id, expected_revision),
            )
            after_row = connection.execute(
                "SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, invoice_id)
            ).fetchone()
            if after_row is None or after_row["revision"] != expected_revision + 1:
                raise RuntimeError("保存后回查失败。")
            after = _invoice_view(dict(after_row))
            result = {"status": "committed", "committed": True, "replayed": False, "before": _invoice_view(old), "after": after, "rechecked": True}
            self._record_operation(connection, user_id, company_id, invoice_id, idempotency_key, payload_hash, result["status"], result, "update_invoice", payload)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _valid_idempotency_key(key: str, user_id: str, company_id: str, invoice_id: str) -> bool:
        pattern = re.compile(r"^([A-Za-z0-9_-]+):([A-Za-z0-9_-]+):invoice:([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)$")
        match = pattern.fullmatch(str(key))
        return bool(match and match.group(1) == user_id and match.group(2) == company_id and match.group(3) == invoice_id)

    @staticmethod
    def _record_operation(connection, user_id: str, company_id: str, invoice_id: str, key: str, payload_hash: str, status: str, result: dict, action: str, payload: dict | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps(safe_case_json(result), ensure_ascii=False, sort_keys=True)
        payload_json = json.dumps(safe_case_json(payload or {"status": status}), ensure_ascii=False, sort_keys=True)
        connection.execute(
            """INSERT INTO operations(operation_id,user_id,company_id,object_type,object_id,idempotency_key,payload_hash,status,result_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("op-" + uuid4().hex, user_id, company_id, "invoice", invoice_id, key, payload_hash, status, result_json, now),
        )
        connection.execute(
            """INSERT INTO audit_events(user_id,company_id,action,object_type,object_id,payload_json,result_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, company_id, action, "invoice", invoice_id, payload_json, result_json, now),
        )

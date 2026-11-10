"""Preview how a proposed invoice-purpose condition changes current work."""

from __future__ import annotations

from typing import Any

from .errors import InvalidInputError
from .money import amount_to_cents, cents_to_text
from .rules import applicable_rule, condition_execution, evaluate_invoice_purpose
from .services import TaxService, _month_range
from .storage import TaxDatabase
from .task_manager import TaskManager


def preview_purpose_rule(
    database: TaxDatabase, *, user_id: str, company_id: str, month: str,
    condition_type: str, threshold_yuan: str | None = None,
) -> dict[str, Any]:
    """Compare the active rule with a proposed condition without changing either."""

    service = TaxService(database)
    service.require_access(user_id=user_id, company_id=company_id)
    start, end = _month_range(month)
    if condition_type not in {"all", "none", "amount_gte"}:
        raise InvalidInputError("预览条件必须是 all、none 或 amount_gte。")
    condition: dict[str, Any] = {"type": condition_type}
    if condition_type == "amount_gte":
        if threshold_yuan is None:
            raise InvalidInputError("请提供金额门槛。")
        condition["amount_cents"] = amount_to_cents(threshold_yuan)
    current = applicable_rule(database.connection, kind="invoice_purpose", company_id=company_id, month=month)
    candidate = {**current, "condition": condition}
    candidate_summary = condition_execution(condition, kind="invoice_purpose")["summary"]
    rows = database.connection.execute(
        """SELECT invoice_id,amount_cents,purpose FROM invoices
           WHERE company_id=? AND issue_date>=? AND issue_date<? ORDER BY issue_date,invoice_id""",
        (company_id, start, end),
    ).fetchall()
    before_missing, after_missing = [], []
    newly_required_missing, newly_required_filled, no_longer_required = [], [], []
    for row in rows:
        old = evaluate_invoice_purpose(current, int(row["amount_cents"]), row["purpose"])
        new = evaluate_invoice_purpose(candidate, int(row["amount_cents"]), row["purpose"])
        invoice_id = row["invoice_id"]
        if old["missing"]:
            before_missing.append(invoice_id)
        if new["missing"]:
            after_missing.append(invoice_id)
        detail = {"invoice_id": invoice_id, "amount": cents_to_text(int(row["amount_cents"]))}
        if not old["required"] and new["required"]:
            (newly_required_missing if new["missing"] else newly_required_filled).append(detail)
        if old["required"] and not new["required"]:
            no_longer_required.append({**detail, "purpose_missing": not str(row["purpose"] or "").strip()})
    current_tasks = TaskManager(database, service).list_tasks(user_id=user_id, company_id=company_id)["tasks"]
    task_impacts = []
    for task in current_tasks:
        if task["month"] != month:
            continue
        # A selected task expresses an explicit user request, independent of the rule threshold.
        if task["scope"] == "selected":
            task_impacts.append({"task_id": task["task_id"], "scope": "selected", "added_pending": [],
                                 "removed_pending": [], "summary": "指定发票任务保留用户选择的范围。"})
        else:
            task_impacts.append({"task_id": task["task_id"], "scope": "all_missing",
                                 "added_pending": sorted(set(after_missing) - set(before_missing)),
                                 "removed_pending": sorted(set(before_missing) - set(after_missing))})
    return {
        "status": "ok", "company_id": company_id, "month": month, "preview_only": True,
        "current_rule": {key: current.get(key) for key in ("rule_id", "version", "name", "condition", "execution_summary")},
        "proposed_condition": condition, "proposed_summary": candidate_summary,
        "before_missing": before_missing, "after_missing": after_missing,
        "newly_required_missing": newly_required_missing, "newly_required_filled": newly_required_filled,
        "no_longer_required": no_longer_required, "task_impacts": task_impacts,
        "summary": "这是规则变化影响预览，尚未更改规则、发票或任务。",
    }

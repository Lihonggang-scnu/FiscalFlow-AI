"""Read the real task and invoice rows used by the three-page Chainlit workspace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .rules import applicable_rules
from .services import TaxService
from .storage import TaxDatabase
from .task_manager import TaskManager


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def read_workspace_page(
    *,
    database_path: str | Path,
    user_id: str,
    company_id: str,
    month: str | None,
    refresh_tasks: bool = False,
) -> dict[str, Any]:
    """Return user/company-scoped tasks, invoices and rules for an explicit month.

    An unselected period deliberately returns empty period data. When requested,
    active task progress is recomputed by TaskManager from the current invoice
    purposes before the rows are returned.
    """

    if month is not None and not _MONTH.fullmatch(month):
        raise ValueError("月份必须是 YYYY-MM。")

    with TaxDatabase(database_path) as database:
        service = TaxService(database)
        permission = service.require_access(user_id=user_id, company_id=company_id)
        empty = {
            "user_id": user_id,
            "company_id": company_id,
            "month": month,
            "can_modify": bool(permission["can_modify"]),
            "tasks": [],
            "invoices": [],
            "rules": [],
        }
        if month is None:
            return empty

        manager = TaskManager(database, service)
        if refresh_tasks:
            for task in manager.list_tasks(user_id=user_id, company_id=company_id, include_history=True)["tasks"]:
                if task["month"] == month:
                    manager.refresh_task(task_id=task["task_id"], user_id=user_id, company_id=company_id)

        tasks = [
            task
            for task in manager.list_tasks(user_id=user_id, company_id=company_id, include_history=True)["tasks"]
            if task["month"] == month
        ]
        invoices = service.query_invoice_summary(
            user_id=user_id,
            company_id=company_id,
            month=month,
        )["data"]["invoices"]
        purpose_check = service.list_missing_purposes(
            user_id=user_id,
            company_id=company_id,
            month=month,
        )
        missing_invoice_ids = {
            str(invoice["invoice_id"])
            for invoice in purpose_check.get("data", {}).get("missing", [])
        }
        rules = applicable_rules(database.connection, company_id=company_id, month=month)

    return {
        **empty,
        "tasks": [
            {
                "task_id": task["task_id"],
                "month": task["month"],
                "scope": task["scope"],
                "status": task["status"],
                "target_invoice_ids": task["target_invoice_ids"],
                "pending_invoice_ids": task["pending_invoice_ids"],
                "message": task["message"],
                "next_step": task["next_step"],
                "revision": task["revision"],
                "updated_at": task["updated_at"],
            }
            for task in tasks
        ],
        "invoices": [
            {
                "invoice_id": invoice["invoice_id"],
                "issue_date": invoice["issue_date"],
                "seller_name": invoice["seller_name"],
                "project_name": invoice["project_name"],
                "amount": invoice["amount"],
                "purpose": invoice["purpose"],
                "purpose_status": (
                    "filled"
                    if str(invoice["purpose"] or "").strip()
                    else "required"
                    if purpose_check["status"] == "ok" and invoice["invoice_id"] in missing_invoice_ids
                    else "optional"
                    if purpose_check["status"] == "ok"
                    else "undetermined"
                ),
                "remarks": invoice["remarks"],
                "revision": invoice["revision"],
            }
            for invoice in invoices
        ],
        "purpose_check_status": purpose_check["status"],
        "purpose_check_summary": purpose_check.get("summary", ""),
        "rules": [
            {
                "rule_id": rule.get("rule_id"),
                "version": rule.get("version"),
                "name": rule.get("name"),
                "kind": rule.get("kind"),
                "status": rule.get("status"),
                "effective_from": rule.get("effective_from"),
                "content": rule.get("content"),
                "execution_status": rule.get("execution_status", rule.get("status")),
                "execution_summary": rule.get("execution_summary", rule.get("summary")),
            }
            for rule in rules
        ],
    }

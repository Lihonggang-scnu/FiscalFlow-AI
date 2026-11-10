"""Read-only data for the Chainlit invoice and rule context panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .rules import applicable_rules
from .services import TaxService
from .storage import TaxDatabase
from .task_manager import TaskManager


def read_period_context(
    *,
    database_path: str | Path,
    user_id: str,
    company_id: str,
    month: str | None,
) -> dict[str, Any]:
    """Return authorized invoices, rules, and tasks for one explicitly chosen month."""

    with TaxDatabase(database_path) as database:
        service = TaxService(database)
        service.require_access(user_id=user_id, company_id=company_id)
        if month is None:
            return {"company_id": company_id, "month": None, "invoices": [], "rules": [], "tasks": []}

        invoices = service.query_invoice_summary(
            user_id=user_id,
            company_id=company_id,
            month=month,
        )["data"]["invoices"]
        rules = applicable_rules(database.connection, company_id=company_id, month=month)
        tasks = TaskManager(database, service).list_tasks(
            user_id=user_id,
            company_id=company_id,
            include_history=True,
        )["tasks"]

    return {
        "company_id": company_id,
        "month": month,
        "invoices": [
            {
                "invoice_id": invoice["invoice_id"],
                "issue_date": invoice["issue_date"],
                "seller_name": invoice["seller_name"],
                "project_name": invoice["project_name"],
                "amount": invoice["amount"],
                "purpose": invoice["purpose"],
                "remarks": invoice["remarks"],
            }
            for invoice in invoices
        ],
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
            }
            for task in tasks
            if task["month"] == month
        ],
    }

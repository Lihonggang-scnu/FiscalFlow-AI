"""Read the real task and invoice rows used by the three-page Chainlit workspace."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .rules import applicable_rules
from .services import TaxService, _clean_text
from .storage import TaxDatabase
from .task_ledger import TaskLedger
from .task_manager import TaskManager


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_FIELD_LABELS = {"purpose": "用途", "remarks": "备注"}


def workspace_shared_draft(page: dict[str, Any], invoice_id: str | None) -> dict[str, Any]:
    """Build the invoice form from the same drafts used by the conversation."""

    invoice = next((item for item in page.get("invoices", []) if item["invoice_id"] == invoice_id), None)
    drafts = (page.get("frontier") or {}).get("drafts", {}).get(invoice_id, {})
    if not invoice or not drafts:
        return {}
    return {
        "invoice_id": invoice_id,
        "expected_revision": invoice["revision"],
        "purpose": drafts.get("purpose", {}).get("value", invoice.get("purpose") or ""),
        "remarks": drafts.get("remarks", {}).get("value", invoice.get("remarks") or ""),
        "draft_event_ids": {field: item["event_id"] for field, item in drafts.items()},
        "modified_fields": [],
        "idempotency_key": "",
    }


def sync_workspace_draft(*, database_path: str | Path, user_id: str, company_id: str, invoice_id: str,
                         expected_revision: int, changes: dict[str, str], expected_draft_events: dict[str, str],
                         session_id: str, turn_id: str) -> dict[str, Any]:
    """Save edited form fields into the active task, without writing invoice data."""

    with TaxDatabase(database_path) as database:
        service = TaxService(database)
        service.require_access(user_id=user_id, company_id=company_id, modify=True)
        manager = TaskManager(database, service)
        connection = database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            tasks = manager.list_tasks(user_id=user_id, company_id=company_id, include_history=True)["tasks"]
            task = next((item for item in tasks if item["status"] in {"open", "waiting_input", "out_of_scope"}), None)
            if task is None and tasks and tasks[0]["status"] == "completed":
                task = tasks[0]
            if not task or invoice_id not in task["target_invoice_ids"]:
                connection.rollback()
                return {"status": "local_only", "summary": "该票不属于当前办件，草稿暂存于当前会话。"}
            current = service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=invoice_id)
            if current.get("status") != "ok":
                connection.rollback()
                return current
            invoice = current["data"]
            if invoice["revision"] != expected_revision:
                connection.rollback()
                return {"status": "revision_conflict", "summary": "发票资料已更新，请刷新后再编辑草稿。"}
            ledger = TaskLedger(database)
            memory = ledger.memory(user_id=user_id, company_id=company_id)
            for field in changes:
                current_draft = memory["drafts"].get(f"{invoice_id}:{field}")
                if (current_draft or {}).get("event_id") != expected_draft_events.get(field):
                    connection.rollback()
                    return {"status": "revision_conflict", "summary": f"{_FIELD_LABELS[field]}草稿已更新，请刷新后再编辑。"}
            for field, value in changes.items():
                current_draft = memory["drafts"].get(f"{invoice_id}:{field}")
                if _clean_text(value) == _clean_text(invoice.get(field)):
                    if current_draft:
                        ledger.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                                      task_id=task["task_id"], kind="draft_removed", payload={"invoice_id": invoice_id, "field": field})
                elif not current_draft or current_draft.get("value") != value:
                    ledger.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                                  task_id=task["task_id"], kind="draft_set", payload={"invoice_id": invoice_id, "field": field,
                                      "value": value, "save_requested": False, "base_revision": expected_revision, "source": "workspace"})
            connection.commit()
            return {"status": "ok", "summary": "草稿已暂存到当前办件，发票尚未修改。"}
        except Exception:
            connection.rollback()
            raise


def _undoable_fields(database: TaxDatabase, *, user_id: str, company_id: str,
                     invoice: dict[str, Any], can_modify: bool) -> list[str]:
    if not can_modify:
        return []
    rows = database.connection.execute(
        """SELECT result_json FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice'
           AND object_id=? AND status='committed' AND idempotency_key NOT LIKE '%:undo-%'
           ORDER BY created_at DESC,rowid DESC""",
        (user_id, company_id, invoice["invoice_id"]),
    ).fetchall()
    latest: dict[str, bool] = {}
    for row in rows:
        saved = json.loads(row["result_json"])
        before, after = saved.get("before", {}), saved.get("after", {})
        for field in ("purpose", "remarks"):
            if field not in latest and before.get(field) != after.get(field):
                latest[field] = invoice.get(field) == after.get(field)
        if len(latest) == 2:
            break
    return [field for field in ("purpose", "remarks") if latest.get(field)]


def _recent_task_changes(events: list[dict[str, Any]], *, task_ids: set[str], invoice_ids: set[str]) -> list[dict[str, str]]:
    summaries = []
    for event in reversed(events):
        kind = event["kind"]
        payload = event["payload"]
        invoice_id = str(payload.get("invoice_id") or "")
        if event.get("task_id") and event["task_id"] not in task_ids:
            continue
        if not event.get("task_id") and invoice_id not in invoice_ids:
            continue
        if kind == "invoice_saved":
            fields = "、".join(_FIELD_LABELS[field] for field in payload.get("fields", []) if field in _FIELD_LABELS)
            summary = f"{invoice_id} 已保存{fields}" if fields else f"{invoice_id} 已保存发票信息"
        elif kind == "deferred":
            reason = str(payload.get("reason") or "")
            summary = f"{invoice_id} 已暂缓" + (f"：{reason}" if reason else "")
        elif kind == "resumed":
            summary = f"{invoice_id} 已恢复办理"
        elif kind in {"draft_set", "draft_removed"}:
            field = _FIELD_LABELS.get(payload.get("field"), "")
            summary = f"{invoice_id} 的{field}草稿" + ("已更新" if kind == "draft_set" else "已清除")
        elif kind == "scope_changed":
            removed = payload.get("removed_invoice_ids", [])
            summary = f"办件范围已调整，移出 {len(removed)} 张发票"
        elif kind == "note_set":
            summary = "办件事项已更新"
        else:
            continue
        summaries.append({"kind": kind, "summary": summary, "occurred_at": event["occurred_at"]})
        if len(summaries) == 5:
            break
    return summaries


def read_workspace_page(
    *,
    database_path: str | Path,
    user_id: str,
    company_id: str,
    month: str | None,
    refresh_tasks: bool = False,
    session_id: str = "",
    turn_id: str = "",
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
            "frontier": None,
            "diff": {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}},
            "recent_changes": [],
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
        ledger = TaskLedger(database)
        ledger_state = ledger.frontier(
            user_id=user_id,
            company_id=company_id,
            session_id=session_id,
            turn_id=turn_id,
            observe=False,
        )
        frontier = ledger_state["frontier"]
        if frontier.get("month") != month:
            frontier = None
            diff = {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}}
        else:
            diff = ledger_state["diff"]
        period_task_ids = {task["task_id"] for task in tasks}
        if frontier and frontier.get("task_id"):
            period_task_ids.add(frontier["task_id"])
        period_invoice_ids = {str(invoice["invoice_id"]) for invoice in invoices}
        recent_changes = _recent_task_changes(
            ledger.events(user_id=user_id, company_id=company_id),
            task_ids=period_task_ids,
            invoice_ids=period_invoice_ids,
        )
        undoable_fields = {
            str(invoice["invoice_id"]): _undoable_fields(
                database,
                user_id=user_id,
                company_id=company_id,
                invoice=invoice,
                can_modify=bool(permission["can_modify"]),
            )
            for invoice in invoices
        }

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
                "undo_fields": undoable_fields[str(invoice["invoice_id"])],
            }
            for invoice in invoices
        ],
        "purpose_check_status": purpose_check["status"],
        "purpose_check_summary": purpose_check.get("summary", ""),
        "frontier": frontier,
        "diff": diff,
        "recent_changes": recent_changes,
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

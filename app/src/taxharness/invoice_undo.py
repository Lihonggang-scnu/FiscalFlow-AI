"""Undo an explicit invoice change using the existing business write service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .errors import InvalidInputError
from .services import TaxService
from .storage import TaxDatabase
from .task_ledger import TaskLedger
from .task_manager import TaskManager


def undo_invoice_change(
    database: TaxDatabase, *, user_id: str, company_id: str, invoice_id: str,
    field: str | None = None, session_id: str = "", turn_id: str = "",
) -> dict[str, Any]:
    """Restore fields changed by the user's latest saved operation on one invoice."""

    if field not in {None, "purpose", "remarks"}:
        raise InvalidInputError("只能撤销用途或备注的修改。")
    service = TaxService(database)
    service.require_access(user_id=user_id, company_id=company_id, modify=True)
    current_result = service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=invoice_id)
    if current_result.get("status") != "ok":
        return current_result
    rows = database.connection.execute(
        """SELECT * FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice'
           AND object_id=? AND status='committed' AND idempotency_key NOT LIKE '%:undo-%'
           ORDER BY created_at DESC,rowid DESC""",
        (user_id, company_id, invoice_id),
    ).fetchall()
    original = None
    for row in rows:
        saved = json.loads(row["result_json"])
        before, after = saved.get("before", {}), saved.get("after", {})
        changed = [key for key in ("purpose", "remarks") if before.get(key) != after.get(key)]
        selected = [field] if field and field in changed else (changed if field is None else [])
        if selected:
            original = (row, before, after, selected)
            break
    if original is None:
        return {"status": "not_found", "summary": "没有找到可撤销的对应保存操作。"}
    row, before, after, selected = original
    digest = hashlib.sha256((row["operation_id"] + ":" + ",".join(sorted(selected))).encode()).hexdigest()[:20]
    key = f"{user_id}:{company_id}:invoice:{invoice_id}:undo-{digest}"
    previous_undo = database.connection.execute(
        "SELECT status,result_json FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice' AND object_id=? AND idempotency_key=?",
        (user_id, company_id, invoice_id, key),
    ).fetchone()
    if previous_undo and previous_undo["status"] == "committed":
        return {"status": "already_undone", "summary": "这次修改已经撤销过，没有再次改动。", "result": json.loads(previous_undo["result_json"])}
    current = current_result["data"]
    conflicts = [key for key in selected if current.get(key) != after.get(key)]
    if conflicts:
        return {"status": "revision_conflict", "summary": "要撤销的字段后来又被修改，请先确认当前内容。",
                "fields": conflicts, "current": current}
    changes = {key: before.get(key) for key in selected}
    result = TaskManager(database, service).save_invoice(
        user_id=user_id, company_id=company_id, invoice_id=invoice_id,
        month=current["issue_date"][:7], changes=changes, expected_revision=current["revision"], idempotency_key=key,
    )
    if result.get("status") in {"committed", "no_change"}:
        try:
            ledger = TaskLedger(database)
            if result.get("task_refresh_status") == "no_active_task":
                source_events = [event for event in ledger.events(user_id=user_id, company_id=company_id)
                                 if event["kind"] == "invoice_saved" and event["task_id"]
                                 and event["payload"].get("source_operation_id") == row["operation_id"]]
                if source_events:
                    task_result = TaskManager(database, service).reopen_after_undo(
                        task_id=source_events[-1]["task_id"], user_id=user_id, company_id=company_id)
                    result["task_reopen_status"] = task_result["status"]
                    if task_result.get("task"):
                        result["task"] = task_result["task"]
            ledger.invoice_saved(user_id=user_id, company_id=company_id, session_id=session_id,
                                turn_id=turn_id, invoice_id=invoice_id, fields=selected,
                                task_id=(result.get("task") or {}).get("task_id"),
                                saved_values={key: (result.get("after") or result.get("current") or {}).get(key) for key in selected})
        except sqlite3.Error:
            result["task_memory_warning"] = "发票已还原，但任务记忆未刷新。"
    return {**result, "undone_fields": selected, "source_operation_id": row["operation_id"]}

"""Complete, user/company-scoped invoice-purpose task management.

This module stores progress only. Current invoice values and applicable rules
always come from TaxService when a task is created, resumed, or refreshed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .errors import TaxHarnessError
from .services import TaxService, _clean_text
from .storage import TaxDatabase, record_task_event


ACTIVE_STATUSES = ("waiting_input", "open", "out_of_scope")
TERMINAL_STATUSES = ("completed", "cancelled", "superseded", "deleted")
SCOPES = {"all_missing", "selected"}
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _unique_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result = []
    for value in values or []:
        value = str(value).strip()
        if value and value not in result:
            result.append(value)
    return result


class TaskManager:
    """Task state bound to the existing database and TaxService."""

    def __init__(self, database: TaxDatabase, service: TaxService | None = None):
        self.database = database
        self.service = service or TaxService(database)

    @staticmethod
    def _validate_month(month: str | None) -> str | None:
        if month is None:
            return None
        if not _MONTH_RE.fullmatch(str(month)):
            raise ValueError("月份必须是 YYYY-MM。")
        return str(month)

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["target_invoice_ids"] = json.loads(value.pop("target_invoice_ids_json"))
        value["pending_invoice_ids"] = json.loads(value.pop("pending_invoice_ids_json"))
        return value

    def _find(self, task_id: str, user_id: str, company_id: str) -> sqlite3.Row | None:
        return self.database.connection.execute(
            "SELECT * FROM tasks WHERE task_id=? AND user_id=? AND company_id=?",
            (task_id, user_id, company_id),
        ).fetchone()

    def _active(self, user_id: str, company_id: str) -> sqlite3.Row | None:
        return self.database.connection.execute(
            """SELECT * FROM tasks WHERE user_id=? AND company_id=?
               AND status IN ('waiting_input','open','out_of_scope')
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, company_id),
        ).fetchone()

    def _invoice_rows(self, user_id: str, company_id: str, month: str) -> dict[str, dict[str, Any]]:
        result = self.service.query_invoice_summary(user_id=user_id, company_id=company_id, month=month)
        return {row["invoice_id"]: row for row in result["data"]["invoices"]}

    def _goal(self, *, user_id: str, company_id: str, month: str | None, scope: str, target_ids: list[str]) -> dict[str, Any]:
        if month is None:
            return {
                "status": "waiting_input",
                "target": target_ids,
                "pending": target_ids,
                "message": "还缺少办理月份，请补充月份。",
                "next_step": "补充月份",
            }
        if month != "2026-09":
            if scope == "selected":
                rows = self._invoice_rows(user_id, company_id, month)
                invalid = [invoice_id for invoice_id in target_ids if invoice_id not in rows]
                if invalid:
                    return {"error": {"status": "invalid_target", "summary": "指定发票不属于当前企业和月份。", "invoice_ids": invalid}}
                pending = [invoice_id for invoice_id in target_ids if not str(rows[invoice_id].get("purpose") or "").strip()]
            else:
                pending = []
            return {
                "status": "out_of_scope",
                "target": target_ids,
                "pending": pending,
                "message": "当前初赛用途检查只覆盖 2026-09；请更正月份，或改做金额查询。",
                "next_step": "更正月份",
            }
        if scope == "all_missing":
            result = self.service.list_missing_purposes(user_id=user_id, company_id=company_id, month=month)
            if result.get("status") != "ok":
                return {"error": {"status": result.get("status", "cannot_determine"), "summary": result.get("summary", "当前规则不能自动判定。"), "data": result.get("data", {})}}
            target = [row["invoice_id"] for row in result["data"]["missing"]]
            pending = list(target)
            rule = result["data"].get("rule", {})
            rule_note = f"依据 {rule.get('name', rule.get('rule_id', '当前规则'))} {rule.get('version', '')}。".strip()
        else:
            rows = self._invoice_rows(user_id, company_id, month)
            invalid = [invoice_id for invoice_id in target_ids if invoice_id not in rows]
            if invalid:
                return {"error": {"status": "invalid_target", "summary": "指定发票不属于当前企业和月份。", "invoice_ids": invalid}}
            target = list(target_ids)
            pending = [invoice_id for invoice_id in target if not str(rows[invoice_id].get("purpose") or "").strip()]
            rule_note = ""
        if pending:
            return {"status": "open", "target": target, "pending": pending, "message": f"任务进行中。{rule_note}", "next_step": "补充待办发票用途"}
        return {"status": "completed", "target": target, "pending": [], "message": f"任务目标已满足。{rule_note}", "next_step": "无需继续办理"}

    def _insert(self, *, user_id: str, company_id: str, month: str | None, scope: str, goal: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        task_id = "task-" + uuid4().hex
        closed_at = now if goal["status"] == "completed" else None
        self.database.connection.execute(
            """INSERT INTO tasks(task_id,user_id,company_id,month,scope,target_invoice_ids_json,pending_invoice_ids_json,
               status,message,next_step,revision,created_at,updated_at,closed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, user_id, company_id, month, scope, json.dumps(goal["target"], ensure_ascii=False),
             json.dumps(goal["pending"], ensure_ascii=False), goal["status"], goal["message"], goal["next_step"],
             1, now, now, closed_at),
        )
        row = self._find(task_id, user_id, company_id)
        task = self._decode(row)
        record_task_event(self.database.connection, user_id=user_id, company_id=company_id,
                          task_id=task_id, kind="task_created", payload={"after": task})
        return task

    def create_task(
        self,
        *,
        user_id: str,
        company_id: str,
        month: str | None = None,
        scope: str = "all_missing",
        invoice_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        if scope not in SCOPES:
            return {"status": "invalid_input", "summary": "scope 必须是 all_missing 或 selected。"}
        target_ids = _unique_ids(invoice_ids)
        if scope == "selected" and not target_ids:
            return {"status": "invalid_input", "summary": "selected 任务至少需要一个发票编号。"}
        if scope == "all_missing" and target_ids:
            return {"status": "invalid_input", "summary": "all_missing 不能同时指定发票编号。"}
        month = self._validate_month(month)
        if month is not None and month != "2026-09":
            return {"status": "out_of_scope", "summary": "用途补全任务只覆盖 2026-09；任务未创建。", "requested_month": month}
        existing = self._active(user_id, company_id)
        if existing is not None:
            return {"status": "existing", "summary": "该用户在该企业已有未完成任务。", "task": self._decode(existing)}
        goal = self._goal(user_id=user_id, company_id=company_id, month=month, scope=scope, target_ids=target_ids)
        if "error" in goal:
            return goal["error"]
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._active(user_id, company_id)
            if existing is not None:
                connection.rollback()
                return {"status": "existing", "summary": "该用户在该企业已有未完成任务。", "task": self._decode(existing)}
            task = self._insert(user_id=user_id, company_id=company_id, month=month, scope=scope, goal=goal)
            connection.commit()
            return {"status": "created", "task": task}
        except Exception:
            connection.rollback()
            raise

    def list_tasks(self, *, user_id: str, company_id: str, include_history: bool = False) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        if include_history:
            rows = self.database.connection.execute(
                """SELECT * FROM tasks WHERE user_id=? AND company_id=? AND status<>'deleted'
                   ORDER BY updated_at DESC""", (user_id, company_id)
            ).fetchall()
        else:
            rows = self.database.connection.execute(
                """SELECT * FROM tasks WHERE user_id=? AND company_id=?
                   AND status IN ('waiting_input','open','out_of_scope') ORDER BY updated_at DESC""",
                (user_id, company_id),
            ).fetchall()
        return {"status": "ok", "tasks": [self._decode(row) for row in rows]}

    def _persist_state(self, task: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._find(task["task_id"], task["user_id"], task["company_id"])
            if current is None:
                connection.rollback()
                return {"status": "not_found", "summary": "任务不存在。"}
            if expected_revision is not None and current["revision"] != expected_revision:
                connection.rollback()
                return {"status": "revision_conflict", "summary": "任务版本已变化，请重新读取后更正。", "task": self._decode(current)}
            now = _now()
            closed_at = task.get("closed_at")
            connection.execute(
                """UPDATE tasks SET month=?,scope=?,target_invoice_ids_json=?,pending_invoice_ids_json=?,status=?,
                   message=?,next_step=?,revision=revision+1,updated_at=?,closed_at=? WHERE task_id=?""",
                (task.get("month"), task["scope"], json.dumps(task["target_invoice_ids"], ensure_ascii=False),
                 json.dumps(task["pending_invoice_ids"], ensure_ascii=False), task["status"], task["message"],
                 task["next_step"], now, closed_at, task["task_id"]),
            )
            updated = self._find(task["task_id"], task["user_id"], task["company_id"])
            record_task_event(connection, user_id=task["user_id"], company_id=task["company_id"], task_id=task["task_id"],
                              kind="task_changed", payload={"before": self._decode(current), "after": self._decode(updated)})
            connection.commit()
            return {"status": "ok", "task": self._decode(updated)}
        except Exception:
            connection.rollback()
            raise

    def refresh_task(self, *, task_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        row = self._find(task_id, user_id, company_id)
        if row is None or row["status"] == "deleted":
            return {"status": "not_found", "summary": "任务不存在。"}
        current = self._decode(row)
        if current["status"] not in ACTIVE_STATUSES:
            return {"status": "ok", "task": current, "refreshed": False}
        goal = self._goal(user_id=user_id, company_id=company_id, month=current["month"], scope=current["scope"], target_ids=current["target_invoice_ids"])
        if "error" in goal:
            return goal["error"]
        changed = any(current[key] != goal[value] for key, value in (("status", "status"), ("target_invoice_ids", "target"), ("pending_invoice_ids", "pending"), ("message", "message"), ("next_step", "next_step")))
        if not changed:
            return {"status": "ok", "task": current, "refreshed": False}
        updated = {**current, "status": goal["status"], "target_invoice_ids": goal["target"], "pending_invoice_ids": goal["pending"], "message": goal["message"], "next_step": goal["next_step"]}
        if goal["status"] == "completed":
            updated["closed_at"] = updated.get("closed_at") or _now()
        else:
            updated["closed_at"] = None
        result = self._persist_state(updated, expected_revision=current["revision"])
        if result["status"] != "ok":
            return result
        return {"status": "ok", "task": result["task"], "refreshed": True}

    def get_task(self, *, task_id: str, user_id: str, company_id: str, refresh: bool = False) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        row = self._find(task_id, user_id, company_id)
        if row is None or row["status"] == "deleted":
            return {"status": "not_found", "summary": "任务不存在。"}
        if refresh and row["status"] in ACTIVE_STATUSES:
            return self.refresh_task(task_id=task_id, user_id=user_id, company_id=company_id)
        return {"status": "ok", "task": self._decode(row)}

    def resume_task(self, *, task_id: str | None, user_id: str, company_id: str) -> dict[str, Any]:
        if task_id is None:
            row = self._active(user_id, company_id)
            if row is None:
                self.service.require_access(user_id=user_id, company_id=company_id)
                return {"status": "no_task", "summary": "当前用户和企业没有未完成任务。"}
            task_id = row["task_id"]
        current = self.get_task(task_id=task_id, user_id=user_id, company_id=company_id)
        if current["status"] != "ok":
            return current
        if current["task"]["status"] not in ACTIVE_STATUSES:
            return {"status": "not_resumable", "summary": "已完成或已取消的任务不会自动恢复。", "task": current["task"]}
        result = self.refresh_task(task_id=task_id, user_id=user_id, company_id=company_id)
        if result["status"] == "ok":
            result["status"] = "resumed"
        return result

    def correct_task(self, *, task_id: str, user_id: str, company_id: str, changes: dict[str, Any], expected_revision: int | None = None) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        row = self._find(task_id, user_id, company_id)
        if row is None or row["status"] == "deleted":
            return {"status": "not_found", "summary": "任务不存在。"}
        current = self._decode(row)
        if current["status"] not in ACTIVE_STATUSES:
            return {"status": "not_editable", "summary": "已完成或已取消的任务不能更正。", "task": current}
        if not isinstance(changes, dict) or not changes or set(changes) - {"month", "scope", "invoice_ids"}:
            return {"status": "invalid_input", "summary": "任务只能更正月份、范围或指定发票。"}
        month = self._validate_month(changes["month"]) if "month" in changes else current["month"]
        if "month" in changes and month is not None and month != "2026-09":
            return {"status": "out_of_scope", "summary": "用途补全任务只覆盖 2026-09；原任务未改变。", "requested_month": month, "task": current}
        scope = changes.get("scope", current["scope"])
        if scope not in SCOPES:
            return {"status": "invalid_input", "summary": "scope 必须是 all_missing 或 selected。"}
        if scope == "all_missing" and "invoice_ids" in changes:
            return {"status": "invalid_input", "summary": "all_missing 不能指定发票编号。"}
        target = _unique_ids(changes.get("invoice_ids")) if "invoice_ids" in changes else list(current["target_invoice_ids"])
        if scope == "selected" and not target:
            return {"status": "invalid_input", "summary": "selected 任务至少需要一个发票编号。"}
        goal = self._goal(user_id=user_id, company_id=company_id, month=month, scope=scope, target_ids=target)
        if "error" in goal:
            return goal["error"]
        updated = {**current, "month": month, "scope": scope, "status": goal["status"], "target_invoice_ids": goal["target"], "pending_invoice_ids": goal["pending"], "message": goal["message"], "next_step": goal["next_step"], "closed_at": _now() if goal["status"] == "completed" else None}
        result = self._persist_state(updated, expected_revision=expected_revision)
        if result["status"] != "ok":
            return result
        return {"status": "updated", "task": result["task"]}

    def complete_task(self, *, task_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        current = self.get_task(task_id=task_id, user_id=user_id, company_id=company_id)
        if current["status"] != "ok":
            return current
        if current["task"]["status"] == "completed":
            return {"status": "completed", "already_completed": True, "task": current["task"]}
        if current["task"]["status"] not in ACTIVE_STATUSES:
            return {"status": "not_resumable", "summary": "已取消或已删除的任务不能完成。", "task": current["task"]}
        result = self.resume_task(task_id=task_id, user_id=user_id, company_id=company_id)
        if result["status"] not in {"resumed", "ok"}:
            return result
        task = result["task"]
        if task["status"] == "completed":
            result["status"] = "completed"
            return result
        result["status"] = "incomplete"
        result["summary"] = "当前目标仍有待办，任务未完成。"
        return result

    def reopen_after_undo(self, *, task_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        """Reopen the linked completed task when an explicit undo makes work pending."""

        current = self.get_task(task_id=task_id, user_id=user_id, company_id=company_id)
        task = current.get("task")
        if not task or task["status"] != "completed":
            return {"status": "not_reopened", "summary": "原任务不处于已完成状态。"}
        if self._active(user_id, company_id) is not None:
            return {"status": "not_reopened", "summary": "当前已有未完成任务，原任务保持完成。"}
        goal = self._goal(user_id=user_id, company_id=company_id, month=task["month"], scope=task["scope"],
                          target_ids=task["target_invoice_ids"])
        if "error" in goal:
            return goal["error"]
        if goal["status"] == "completed":
            return {"status": "not_reopened", "summary": "撤销后任务目标仍已满足。", "task": task}
        updated = {**task, "status": goal["status"], "target_invoice_ids": goal["target"],
                   "pending_invoice_ids": goal["pending"], "message": goal["message"],
                   "next_step": goal["next_step"], "closed_at": None}
        result = self._persist_state(updated, expected_revision=task["revision"])
        return {"status": "reopened", "task": result["task"]} if result["status"] == "ok" else result

    def cancel_task(self, *, task_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        row = self._find(task_id, user_id, company_id)
        if row is None or row["status"] == "deleted":
            return {"status": "not_found", "summary": "任务不存在。"}
        current = self._decode(row)
        if current["status"] not in ACTIVE_STATUSES:
            return {"status": "already_closed", "task": current}
        current.update(status="cancelled", message="任务已取消。", next_step="不再自动接续", closed_at=_now())
        result = self._persist_state(current, expected_revision=current["revision"])
        return {"status": "cancelled", "task": result.get("task")} if result["status"] == "ok" else result

    def delete_task(self, *, task_id: str, user_id: str, company_id: str) -> dict[str, Any]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        row = self._find(task_id, user_id, company_id)
        if row is None or row["status"] == "deleted":
            return {"status": "not_found", "summary": "任务不存在。"}
        current = self._decode(row)
        current.update(status="deleted", message="任务已删除。", next_step="不再恢复", closed_at=_now())
        result = self._persist_state(current, expected_revision=current["revision"])
        return {"status": "deleted", "task": result.get("task")} if result["status"] == "ok" else result

    def save_invoice(self, *, user_id: str, company_id: str, invoice_id: str, month: str, changes: dict[str, Any], expected_revision: int, idempotency_key: str) -> dict[str, Any]:
        """Save any visible invoice and refresh the user's active task, if any."""

        try:
            current_result = self.service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=invoice_id, month=month)
            if current_result["status"] != "ok":
                return current_result
            current = current_result["data"]
            if not isinstance(changes, dict) or not changes or set(changes) - {"purpose", "remarks"}:
                return {"status": "invalid_input", "summary": "只能修改用途或备注。"}
            prior = self.database.connection.execute(
                """SELECT status FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice'
                   AND object_id=? AND idempotency_key=?""",
                (user_id, company_id, invoice_id, idempotency_key),
            ).fetchone()
            unchanged = all(_clean_text(changes[key]) == current.get(key) for key in changes)
            if unchanged and (prior is None or prior["status"] != "committed"):
                return {"status": "no_change", "committed": False, "summary": "当前已经是这个值，本次无需再修改。", "current": current, "task_progress_updated": False}
            saved = self.service.update_invoice(user_id=user_id, company_id=company_id, invoice_id=invoice_id, changes=changes, expected_revision=expected_revision, idempotency_key=idempotency_key)
        except TaxHarnessError as error:
            return {"status": error.code, "summary": str(error), "task_progress_updated": False}
        committed = saved.get("status") == "committed" or (saved.get("status") == "duplicate" and saved.get("original_status") == "committed")
        if not committed:
            return {**saved, "task_progress_updated": False}
        active = self._active(user_id, company_id)
        if active is None:
            return {**saved, "task_progress_updated": False, "task_refresh_status": "no_active_task"}
        try:
            refreshed = self.refresh_task(task_id=active["task_id"], user_id=user_id, company_id=company_id)
            return {**saved, "task": refreshed.get("task"), "task_progress_updated": refreshed.get("status") == "ok", "task_refresh_status": refreshed.get("status")}
        except Exception as error:  # The invoice commit remains authoritative.
            return {**saved, "task_progress_updated": False, "task_refresh_status": "failed", "task_refresh_error": type(error).__name__}

    def save_invoice_for_task(self, *, task_id: str, user_id: str, company_id: str, invoice_id: str, changes: dict[str, Any], expected_revision: int, idempotency_key: str) -> dict[str, Any]:
        current = self.get_task(task_id=task_id, user_id=user_id, company_id=company_id, refresh=True)
        if current["status"] != "ok":
            return current
        task = current["task"]
        prior = self.database.connection.execute(
            """SELECT status FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice'
               AND object_id=? AND idempotency_key=?""",
            (user_id, company_id, invoice_id, idempotency_key),
        ).fetchone()
        committed_replay = prior is not None and prior["status"] == "committed"
        if not committed_replay and (task["status"] != "open" or invoice_id not in task["target_invoice_ids"]):
            return {"status": "invalid_target", "summary": "该发票不是当前可办理任务目标。", "task": task}
        try:
            saved = self.service.update_invoice(
                user_id=user_id, company_id=company_id, invoice_id=invoice_id,
                changes=changes, expected_revision=expected_revision, idempotency_key=idempotency_key,
            )
        except TaxHarnessError as error:
            return {"status": error.code, "summary": str(error), "task": task, "task_progress_updated": False}
        committed = saved.get("status") == "committed" or (
            saved.get("status") == "duplicate" and saved.get("original_status") == "committed"
        )
        if not committed:
            return {**saved, "task": task, "task_progress_updated": False}
        try:
            refreshed = self.refresh_task(task_id=task_id, user_id=user_id, company_id=company_id)
            if refreshed["status"] != "ok":
                return {**saved, "task": task, "task_progress_updated": False, "task_refresh_status": "failed", "task_refresh_result": refreshed}
            return {**saved, "task": refreshed["task"], "task_progress_updated": True, "task_refresh_status": "ok"}
        except Exception as error:  # Save already committed; expose actual state without inviting a retry.
            return {**saved, "task": task, "task_progress_updated": False, "task_refresh_status": "failed", "task_refresh_error": type(error).__name__}

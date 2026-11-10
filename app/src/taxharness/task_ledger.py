"""Task memories and a live view of work that can be continued.

Invoice values, rules and formal task status remain in their existing services.
The ledger keeps user decisions and drafts that those services do not store.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .errors import InvalidInputError, RuleConflictError, RuleNotFoundError
from .rules import applicable_rule
from .services import TaxService, _clean_text
from .storage import TaxDatabase, record_task_event
from .task_manager import TaskManager


def _draft_key(invoice_id: str, field: str) -> str:
    return f"{invoice_id}:{field}"


def _moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise InvalidInputError("时间应为 ISO 日期或日期时间。") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(UTC)


def replay_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the current memory without calling a model or writing business data."""

    task_order: list[str] = []
    task_states: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["kind"] in {"task_created", "task_changed", "task_imported"}:
            task_id = event["task_id"]
            task_states[task_id] = event["payload"]["after"]
            if task_id in task_order:
                task_order.remove(task_id)
            task_order.append(task_id)
    current_task_id = next((task_id for task_id in reversed(task_order)
                            if task_states[task_id]["status"] in {"open", "waiting_input", "out_of_scope"}), None)
    if current_task_id is None and task_order and task_states[task_order[-1]]["status"] == "completed":
        current_task_id = task_order[-1]
    bindings = {(event["session_id"], event["turn_id"]): set(event["payload"]["target_invoice_ids"])
                for event in events if event["kind"] == "task_bound" and event["task_id"] == current_task_id}
    task_memory_kinds = {"draft_set", "draft_removed", "deferred", "resumed", "note_set", "note_removed",
                         "question_asked", "scope_changed", "invoice_saved", "frontier_observed"}
    state: dict[str, Any] = {"drafts": {}, "deferred": {}, "deferred_sources": {}, "notes": {}, "asked": [],
                             "strategies": {}, "cases": [], "tasks": {}, "invoice_events": {},
                             "rule_events": {}, "last_frontier": None}
    for event in events:
        kind = event["kind"]
        if kind in task_memory_kinds and event.get("task_id") != current_task_id:
            targets = bindings.get((event["session_id"], event["turn_id"]))
            invoice_id = event["payload"].get("invoice_id")
            if targets is None or (invoice_id and invoice_id not in targets):
                continue
        payload = event["payload"]
        if kind == "draft_set":
            state["drafts"][_draft_key(payload["invoice_id"], payload["field"])] = {
                **payload,
                "event_id": event["event_id"],
                "source_turn_id": event["turn_id"],
                "source_session_id": event["session_id"],
            }
        elif kind == "draft_removed":
            state["drafts"].pop(_draft_key(payload["invoice_id"], payload["field"]), None)
        elif kind == "deferred":
            state["deferred"][payload["invoice_id"]] = payload.get("reason", "等待用户补充")
            state["deferred_sources"][payload["invoice_id"]] = event["turn_id"]
        elif kind == "resumed":
            state["deferred"].pop(payload["invoice_id"], None)
            state["deferred_sources"].pop(payload["invoice_id"], None)
        elif kind == "note_set":
            state["notes"][payload["key"]] = {**payload, "source_turn_id": event["turn_id"]}
        elif kind == "note_removed":
            state["notes"].pop(payload["key"], None)
        elif kind == "question_asked":
            state["asked"].append(payload)
            state["asked"] = state["asked"][-20:]
        elif kind == "scope_changed":
            removed = set(payload.get("removed_invoice_ids", []))
            state["drafts"] = {key: value for key, value in state["drafts"].items() if value["invoice_id"] not in removed}
            state["deferred"] = {key: value for key, value in state["deferred"].items() if key not in removed}
            state["deferred_sources"] = {key: value for key, value in state["deferred_sources"].items() if key not in removed}
        elif kind == "invoice_saved":
            saved_values = payload.get("saved_values")
            for field in payload.get("fields", []):
                key = _draft_key(payload["invoice_id"], field)
                draft = state["drafts"].get(key)
                same_value = bool(draft and saved_values is not None and field in saved_values
                                  and _clean_text(draft.get("value")) == _clean_text(saved_values[field]))
                same_turn = bool(draft and saved_values is None and draft.get("source_turn_id") == event["turn_id"])
                if same_value or same_turn:
                    state["drafts"].pop(key, None)
            if "purpose" in payload.get("fields", []) and (saved_values is None or _clean_text(saved_values.get("purpose"))):
                state["deferred"].pop(payload["invoice_id"], None)
                state["deferred_sources"].pop(payload["invoice_id"], None)
        elif kind == "case_observed":
            state["cases"].append({"case_id": event["event_id"], **payload})
            state["cases"] = state["cases"][-50:]
        elif kind == "strategy_proposed":
            state["strategies"][payload["strategy_id"]] = {**payload, "status": "candidate"}
        elif kind == "strategy_activated" and payload["strategy_id"] in state["strategies"]:
            state["strategies"][payload["strategy_id"]]["status"] = "active"
        elif kind == "strategy_retired" and payload["strategy_id"] in state["strategies"]:
            state["strategies"][payload["strategy_id"]]["status"] = "retired"
        elif kind in {"task_created", "task_changed", "task_imported"}:
            state["tasks"][event["task_id"]] = payload["after"]
        elif kind == "invoice_changed":
            state["invoice_events"][payload["invoice_id"]] = payload
        elif kind == "rule_changed":
            state["rule_events"][payload["rule_id"]] = payload
        elif kind == "frontier_observed":
            state["last_frontier"] = payload
    return state


def compare_frontiers(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """Describe how task scope and the available work changed."""

    if before is None:
        return {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}}
    previous_targets = set(before.get("target_invoice_ids", []))
    current_targets = set(after.get("target_invoice_ids", []))
    changes = {}
    for key in ("needs_input", "ready_to_save", "awaiting_confirmation", "waiting", "completed"):
        old = set(before.get(key, []))
        new = set(after.get(key, []))
        if old != new:
            changes[key] = {"added": sorted(new - old), "removed": sorted(old - new)}
    old_drafts, new_drafts = before.get("drafts", {}), after.get("drafts", {})
    updated_drafts = sorted(key for key in set(old_drafts) | set(new_drafts) if old_drafts.get(key) != new_drafts.get(key))
    if updated_drafts:
        changes["drafts"] = {"updated": updated_drafts}
    if before.get("waiting_reasons", {}) != after.get("waiting_reasons", {}):
        changes["waiting_reasons"] = after.get("waiting_reasons", {})
    if before.get("notes", {}) != after.get("notes", {}):
        changes["notes"] = {"updated": True}
    if before.get("rule") != after.get("rule"):
        changes["rule"] = {"before": before.get("rule"), "after": after.get("rule")}
    old_revisions, new_revisions = before.get("invoice_revisions", {}), after.get("invoice_revisions", {})
    revisions = {key: {"before": old_revisions[key], "after": new_revisions[key]}
                 for key in old_revisions.keys() & new_revisions.keys() if old_revisions[key] != new_revisions[key]}
    if revisions:
        changes["invoice_revisions"] = revisions
    affected = (previous_targets ^ current_targets) | set(updated_drafts) | set(revisions)
    for key in ("needs_input", "ready_to_save", "awaiting_confirmation", "waiting", "completed"):
        affected.update(set(before.get(key, [])) ^ set(after.get(key, [])))
    old_waiting, new_waiting = before.get("waiting_reasons", {}), after.get("waiting_reasons", {})
    affected.update(invoice_id for invoice_id in old_waiting.keys() | new_waiting.keys()
                    if old_waiting.get(invoice_id) != new_waiting.get(invoice_id))
    return {
        "initial": False,
        "added_targets": sorted(current_targets - previous_targets),
        "removed_targets": sorted(previous_targets - current_targets),
        "changes": changes,
        "affected_targets": sorted(affected),
        "unchanged_targets": sorted((previous_targets & current_targets) - affected),
        "basis_changed": before.get("rule") != after.get("rule"),
    }


class TaskLedger:
    def __init__(self, database: TaxDatabase):
        self.database = database
        self.service = TaxService(database)
        self.tasks = TaskManager(database, self.service)

    def events(self, *, user_id: str, company_id: str, as_of_recorded: str | None = None,
               valid_at: str | None = None) -> list[dict[str, Any]]:
        self.service.require_access(user_id=user_id, company_id=company_id)
        rows = self.database.connection.execute(
            """SELECT * FROM task_ledger_events WHERE (user_id=? AND company_id=?)
               OR (user_id='__system__' AND company_id IN (?, '*')) ORDER BY sequence""",
            (user_id, company_id, company_id),
        ).fetchall()
        result = []
        recorded_cutoff = _moment(as_of_recorded) if as_of_recorded else None
        valid_cutoff = _moment(valid_at) if valid_at else None
        for row in rows:
            event = dict(row)
            if recorded_cutoff and _moment(event["recorded_at"]) > recorded_cutoff:
                continue
            if valid_cutoff and _moment(event["occurred_at"]) > valid_cutoff:
                continue
            event["payload"] = json.loads(event.pop("payload_json"))
            result.append(event)
        return result

    def memory(self, *, user_id: str, company_id: str, as_of_recorded: str | None = None,
               valid_at: str | None = None) -> dict[str, Any]:
        valid_at = valid_at or datetime.now(UTC).isoformat()
        return replay_events(self.events(user_id=user_id, company_id=company_id,
                                         as_of_recorded=as_of_recorded, valid_at=valid_at))

    def append(
        self,
        *,
        user_id: str,
        company_id: str,
        session_id: str,
        turn_id: str,
        kind: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        self.service.require_access(user_id=user_id, company_id=company_id)
        return record_task_event(self.database.connection, user_id=user_id, company_id=company_id,
            session_id=session_id, turn_id=turn_id, task_id=task_id, kind=kind, payload=payload, occurred_at=occurred_at)

    def apply(
        self,
        *,
        user_id: str,
        company_id: str,
        session_id: str,
        turn_id: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record model-interpreted decisions and drafts from the user's request."""

        self.service.require_access(user_id=user_id, company_id=company_id)
        kinds = {"draft": "draft_set", "remove_draft": "draft_removed", "defer": "deferred",
                 "resume": "resumed", "note": "note_set", "remove_note": "note_removed",
                 "question_asked": "question_asked"}
        if not isinstance(operations, list) or not operations:
            raise InvalidInputError("请提供需要记录的任务信息。")
        parsed = []
        for op in operations:
            action = op.get("action")
            if action not in kinds:
                raise InvalidInputError("不支持的任务记忆操作。")
            payload = {key: value for key, value in op.items() if key != "action"}
            if action in {"draft", "remove_draft", "defer", "resume"} and not str(payload.get("invoice_id", "")).strip():
                raise InvalidInputError("请提供发票编号。")
            if action in {"draft", "remove_draft"} and payload.get("field") not in {"purpose", "remarks"}:
                raise InvalidInputError("草稿字段只能是用途或备注。")
            if action == "draft" and not isinstance(payload.get("value"), str):
                raise InvalidInputError("草稿内容必须是用户提供的文字。")
            if action in {"note", "remove_note"} and not str(payload.get("key", "")).strip():
                raise InvalidInputError("请提供任务事项名称。")
            effective_at = payload.pop("effective_at", None)
            occurred_at = _moment(effective_at).isoformat() if effective_at else None
            parsed.append((kinds[action], payload, occurred_at))
        task_result = self.tasks.resume_task(task_id=None, user_id=user_id, company_id=company_id)
        task_id = task_result.get("task", {}).get("task_id")
        if not task_id:
            history = self.tasks.list_tasks(user_id=user_id, company_id=company_id, include_history=True)["tasks"]
            invoice_ids = {payload["invoice_id"] for _, payload, _ in parsed if payload.get("invoice_id")}
            if history and history[0]["status"] == "completed" and invoice_ids and invoice_ids.issubset(history[0]["target_invoice_ids"]):
                task_id = history[0]["task_id"]
        ids = [self.append(user_id=user_id, company_id=company_id, session_id=session_id,
                           turn_id=turn_id, kind=kind, payload=payload, task_id=task_id, occurred_at=occurred_at)
               for kind, payload, occurred_at in parsed]
        return {"status": "ok", "event_ids": ids, "memory": self.memory(user_id=user_id, company_id=company_id)}

    def frontier(
        self, *, user_id: str, company_id: str, session_id: str = "", turn_id: str = "", observe: bool = True
    ) -> dict[str, Any]:
        """Combine current task data and user decisions into the available work."""

        memory = self.memory(user_id=user_id, company_id=company_id)
        task_result = self.tasks.resume_task(task_id=None, user_id=user_id, company_id=company_id)
        task = task_result.get("task")
        warning = None if task_result.get("status") in {"resumed", "ok", "no_task", "not_resumable"} else {
            "status": task_result.get("status"), "summary": task_result.get("summary", "任务暂时无法刷新。")
        }
        previous = memory["last_frontier"]
        if task is None and warning:
            current = self.tasks.list_tasks(user_id=user_id, company_id=company_id).get("tasks", [])
            task = current[0] if current else None
        if task is None and previous and previous.get("task_id"):
            last = self.tasks.get_task(task_id=previous["task_id"], user_id=user_id, company_id=company_id)
            if last.get("task", {}).get("status") == "completed":
                task = last["task"]
        if task is None and not warning:
            history = self.tasks.list_tasks(user_id=user_id, company_id=company_id, include_history=True)["tasks"]
            if history and history[0]["status"] == "completed":
                task = history[0]
        if observe and task and task["task_id"] not in memory["tasks"]:
            self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                        task_id=task["task_id"], kind="task_imported", payload={"after": task})
            memory = self.memory(user_id=user_id, company_id=company_id)
        targets = list(task.get("target_invoice_ids", [])) if task else []
        scope_was_corrected = bool(task and previous and (previous.get("scope") != task.get("scope") or task.get("scope") == "selected"))
        if observe and previous and task and previous.get("task_id") == task["task_id"] and scope_was_corrected:
            removed = set(previous.get("target_invoice_ids", [])) - set(targets)
            has_memory = any(item["invoice_id"] in removed for item in memory["drafts"].values()) or bool(removed & set(memory["deferred"]))
            if removed and has_memory:
                self.scope_changed(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                                   old_targets=previous["target_invoice_ids"], new_targets=targets, task_id=task["task_id"])
                memory = self.memory(user_id=user_id, company_id=company_id)
        if observe:
            relevant = set(memory["deferred"]) | {item["invoice_id"] for item in memory["drafts"].values()}
            reconciled = False
            for invoice_id in relevant & set(targets):
                current = self.service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=invoice_id)
                if current.get("status") != "ok":
                    continue
                invoice = current["data"]
                for item in memory["drafts"].values():
                    if item["invoice_id"] == invoice_id and invoice.get(item["field"]) == _clean_text(item["value"]):
                        self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                                    task_id=task["task_id"], kind="draft_removed",
                                    payload={"invoice_id": invoice_id, "field": item["field"]})
                        reconciled = True
                unresolved_draft = any(item["invoice_id"] == invoice_id and invoice.get(item["field"]) != _clean_text(item["value"])
                                       for item in memory["drafts"].values())
                if invoice_id in memory["deferred"] and str(invoice.get("purpose") or "").strip() and not unresolved_draft:
                    self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                                task_id=task["task_id"], kind="resumed", payload={"invoice_id": invoice_id})
                    reconciled = True
            if reconciled:
                memory = self.memory(user_id=user_id, company_id=company_id)
        pending = set(task.get("pending_invoice_ids", [])) if task else set()
        work: dict[str, Any] = {
            "company_id": company_id,
            "task_id": task.get("task_id") if task else None,
            "month": task.get("month") if task else None,
            "scope": task.get("scope") if task else None,
            "task_revision": task.get("revision") if task else None,
            "task_status": task.get("status") if task else None,
            "target_invoice_ids": targets,
            "needs_input": [], "ready_to_save": [], "awaiting_confirmation": [], "waiting": [], "completed": [],
            "drafts": {}, "waiting_reasons": {}, "notes": memory["notes"],
            "rule": None, "invoice_revisions": {}, "dependencies": {},
        }
        if task and task.get("month"):
            try:
                rule = applicable_rule(self.database.connection, kind="invoice_purpose", company_id=company_id, month=task["month"])
                work["rule"] = {key: rule.get(key) for key in ("rule_id", "version", "name", "execution_summary")}
            except (RuleConflictError, RuleNotFoundError) as error:
                work["rule"] = {"status": error.code, "summary": str(error)}
            rows = self.service.query_invoice_summary(user_id=user_id, company_id=company_id, month=task["month"])["data"]["invoices"]
            work["invoice_revisions"] = {row["invoice_id"]: row["revision"] for row in rows if row["invoice_id"] in targets}
        if warning:
            work["warning"] = warning
        for invoice_id in targets:
            invoice_drafts = {
                item["field"]: {**item, "save_requested": bool(item.get("save_requested") is True and turn_id
                    and item.get("source_turn_id") == turn_id and item.get("source_session_id") == session_id)}
                for item in memory["drafts"].values() if item["invoice_id"] == invoice_id
            }
            if invoice_drafts:
                work["drafts"][invoice_id] = invoice_drafts
                if invoice_id not in memory["deferred"] and any(item.get("save_requested") for item in invoice_drafts.values()):
                    work["ready_to_save"].append(invoice_id)
                if invoice_id not in memory["deferred"] and any(not item.get("save_requested") for item in invoice_drafts.values()):
                    work["awaiting_confirmation"].append(invoice_id)
            work["dependencies"][invoice_id] = {
                "target_basis": "user_selection" if task.get("scope") == "selected" else "current_rule_and_invoice",
                "task_id": task["task_id"], "task_revision": task["revision"],
                "invoice_revision": work["invoice_revisions"].get(invoice_id),
                "rule": work["rule"],
                "draft_event_ids": {field: item["event_id"] for field, item in invoice_drafts.items()},
            }
            if invoice_id in memory["deferred"] and (invoice_id in pending or invoice_drafts):
                work["waiting"].append(invoice_id)
                work["waiting_reasons"][invoice_id] = memory["deferred"][invoice_id]
            elif invoice_id not in pending:
                if not invoice_drafts:
                    work["completed"].append(invoice_id)
            elif "purpose" not in invoice_drafts:
                work["needs_input"].append(invoice_id)
        actions = []
        for category, action in (("ready_to_save", "save"), ("awaiting_confirmation", "confirm"),
                                 ("needs_input", "ask"), ("waiting", "wait")):
            if work[category]:
                actions.append({"action": action, "invoice_ids": work[category]})
        if task and task.get("status") == "completed" and not work["drafts"]:
            actions.append({"action": "finish", "invoice_ids": work["completed"]})
        if warning:
            actions.append({"action": "inspect_rule", "summary": warning["summary"]})
        work["next_actions"] = actions
        delta = compare_frontiers(previous, work)
        if observe and previous != work:
            self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                        kind="frontier_observed", payload=work, task_id=work["task_id"])
        return {"status": "ok", "frontier": work, "diff": delta, "recent_questions": memory["asked"][-5:]}

    def scope_changed(self, *, user_id: str, company_id: str, session_id: str, turn_id: str,
                      old_targets: list[str], new_targets: list[str], task_id: str | None) -> None:
        removed = sorted(set(old_targets) - set(new_targets))
        if removed:
            self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                        task_id=task_id, kind="scope_changed", payload={"removed_invoice_ids": removed})

    def invoice_saved(self, *, user_id: str, company_id: str, session_id: str, turn_id: str,
                      invoice_id: str, fields: list[str], task_id: str | None = None,
                      source_operation_id: str | None = None, saved_values: dict[str, Any] | None = None) -> None:
        if saved_values is None and source_operation_id:
            row = self.database.connection.execute("SELECT result_json FROM operations WHERE operation_id=?", (source_operation_id,)).fetchone()
            if row:
                result = json.loads(row["result_json"])
                saved = result.get("after") or result.get("current") or {}
                saved_values = {field: saved[field] for field in fields if field in saved}
        self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                    task_id=task_id, kind="invoice_saved", payload={"invoice_id": invoice_id, "fields": fields,
                        "source_operation_id": source_operation_id, "saved_values": saved_values})

    def bind_task_turn(self, *, user_id: str, company_id: str, session_id: str, turn_id: str, task: dict[str, Any]) -> None:
        """Associate same-turn task decisions even if tools finish in a different order."""

        self.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                    task_id=task["task_id"], kind="task_bound", payload={"target_invoice_ids": task["target_invoice_ids"]})

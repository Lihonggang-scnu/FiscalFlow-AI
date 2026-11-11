"""Bound field decisions and transactional execution for one invoice."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .services import TaxService, _clean_text
from .storage import TaxDatabase, record_task_event


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _condition_valid(condition: dict | None) -> bool:
    if condition is None:
        return True
    if not isinstance(condition, dict):
        return False
    operator = condition.get("operator")
    if operator in {"all", "any"}:
        parts = condition.get("conditions")
        return isinstance(parts, list) and bool(parts) and all(_condition_valid(part) for part in parts)
    if operator == "version_matches":
        return condition.get("field") == "revision" and type(condition.get("value")) is int
    if condition.get("field") not in {"purpose", "remarks"}:
        return False
    if operator in {"empty", "not_empty"}:
        return set(condition) <= {"field", "operator"}
    return operator in {"equals", "not_equals"} and "value" in condition and (
        condition["value"] is None or isinstance(condition["value"], str))


def _condition_met(condition: dict | None, invoice: dict) -> bool:
    if condition is None:
        return True
    operator = condition["operator"]
    if operator in {"all", "any"}:
        results = [_condition_met(part, invoice) for part in condition["conditions"]]
        return all(results) if operator == "all" else any(results)
    if operator == "version_matches":
        return invoice["revision"] == condition["value"]
    current = _clean_text(invoice[condition["field"]])
    if operator == "empty":
        return current is None
    if operator == "not_empty":
        return current is not None
    matches = current == _clean_text(condition["value"])
    return matches if operator == "equals" else not matches


class InvoicePlans:
    """Plans bind a real actor, quoted decision, fields, conditions, and observed revision."""

    def __init__(self, database: TaxDatabase):
        self.database = database
        self.service = TaxService(database)

    def prepare(self, *, user_id: str, company_id: str, invoice_id: str, session_id: str,
                turn_id: str, user_message: str, intent_quote: str, expected_revision: int,
                actions: list[dict]) -> dict:
        self.service.require_access(user_id=user_id, company_id=company_id, modify=True)
        if not intent_quote or intent_quote not in user_message:
            raise ValueError("操作决定必须引用当前用户实际说过的话。")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("请先读取当前票据版本。")
        if not isinstance(actions, list) or not actions:
            raise ValueError("请提供需要处理的字段。")
        if any(item.get("mode") in {"draft", "defer"} for item in actions):
            task = self.database.connection.execute("""SELECT target_invoice_ids_json FROM tasks WHERE user_id=? AND company_id=?
                AND status IN ('waiting_input','open','out_of_scope') ORDER BY updated_at DESC LIMIT 1""", (user_id, company_id)).fetchone()
            if task is None or invoice_id not in json.loads(task["target_invoice_ids_json"]):
                raise ValueError("暂存或暂缓前，请先建立包含该发票的办理任务。")
        normalized = []
        seen = set()
        for index, source in enumerate(actions, 1):
            field, mode = source.get("field"), source.get("mode")
            if field not in {"purpose", "remarks"} or field in seen:
                raise ValueError("每个用途或备注字段只能有一个决定。")
            if mode not in {"preserve", "draft", "commit", "clear", "defer"}:
                raise ValueError("不支持这个字段处理方式。")
            value = source.get("value")
            if mode in {"draft", "commit"} and (not isinstance(value, str) or not value.strip()):
                raise ValueError("用途或备注内容不能为空；明确清空应单独选择清空。")
            if mode == "clear":
                value = None
            if mode in {"preserve", "defer"}:
                value = None
            condition = source.get("condition")
            if not _condition_valid(condition):
                raise ValueError("当前条件无法准确处理，请进一步说明。")
            normalized.append({"action_id": f"a{index}", "field": field, "mode": mode, "value": value,
                               "value_source": source.get("value_source"), "condition": condition,
                               "condition_quote": source.get("condition_quote"), "reason": str(source.get("reason") or "")[:1000]})
            seen.add(field)
        payload = {"actions": normalized, "intent_quote": intent_quote,
                   "user_message_sha256": hashlib.sha256(user_message.encode("utf-8")).hexdigest(),
                   "source_turn_id": turn_id, "initial_revision": expected_revision}
        digest = _digest(payload)
        connection = self.database.connection
        plan_id = "plan-" + uuid4().hex
        connection.execute("""INSERT OR IGNORE INTO invoice_action_plans
            (plan_id,user_id,company_id,invoice_id,session_id,turn_id,expected_revision,payload_json,payload_hash,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,'accepted',?)""", (plan_id, user_id, company_id, invoice_id, session_id, turn_id,
            expected_revision, _json(payload), digest, datetime.now(UTC).isoformat()))
        row = connection.execute("""SELECT * FROM invoice_action_plans WHERE user_id=? AND company_id=? AND invoice_id=?
            AND session_id=? AND turn_id=? AND payload_hash=?""", (user_id, company_id, invoice_id, session_id, turn_id, digest)).fetchone()
        return {"plan_id": row["plan_id"], "status": row["status"], "actions": normalized}

    def revoke(self, *, user_id: str, company_id: str, plan_id: str) -> dict:
        self.service.require_access(user_id=user_id, company_id=company_id, modify=True)
        cursor = self.database.connection.execute("""UPDATE invoice_action_plans SET status='revoked'
            WHERE plan_id=? AND user_id=? AND company_id=? AND status='accepted'""", (plan_id, user_id, company_id))
        return {"status": "revoked" if cursor.rowcount else "not_active"}

    def commit(self, *, user_id: str, company_id: str, session_id: str, turn_id: str,
               plan_id: str, action_ids: list[str], request_key: str) -> dict:
        if not isinstance(action_ids, list) or not action_ids or len(set(action_ids)) != len(action_ids):
            raise ValueError("请选择不重复的计划动作。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self.service.require_access(user_id=user_id, company_id=company_id)
            row = connection.execute("""SELECT * FROM invoice_action_plans WHERE plan_id=? AND user_id=? AND company_id=?""",
                                     (plan_id, user_id, company_id)).fetchone()
            if row is None:
                raise ValueError("没有找到当前用户可访问的计划。")
            if not self.service._valid_idempotency_key(request_key, user_id, company_id, row["invoice_id"]):
                raise ValueError("操作标识与用户、企业或票据不一致。")
            request_hash = _digest({"plan_id": plan_id, "action_ids": sorted(action_ids)})
            prior_request = connection.execute("SELECT * FROM invoice_action_requests WHERE user_id=? AND company_id=? AND request_key=?",
                (user_id, company_id, request_key)).fetchone()
            if prior_request:
                if prior_request["request_hash"] != request_hash:
                    connection.rollback()
                    return {"status": "idempotency_conflict", "summary": "该操作标识已用于其他动作。"}
                original = json.loads(prior_request["result_json"])
                current = self.service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=row["invoice_id"])
                connection.rollback()
                return {"status": "duplicate", "original_status": original["status"], "result": original,
                        "current_state": current.get("data")}
            payload = json.loads(row["payload_json"])
            if _digest(payload) != row["payload_hash"]:
                raise ValueError("办理计划已变化，请重新准备。")
            actions_by_id = {action["action_id"]: action for action in payload["actions"]}
            if any(action_id not in actions_by_id for action_id in action_ids):
                raise ValueError("动作不属于这个计划。")
            prior_actions = {item["action_id"]: json.loads(item["result_json"]) for item in connection.execute(
                "SELECT action_id,result_json FROM invoice_action_receipts WHERE plan_id=?", (plan_id,))}
            remaining = [actions_by_id[action_id] for action_id in action_ids if action_id not in prior_actions]
            if not remaining:
                original = prior_actions[action_ids[0]]
                current = self.service.read_invoice(user_id=user_id, company_id=company_id, invoice_id=row["invoice_id"])
                connection.rollback()
                return {"status": "duplicate", "original_status": original["status"], "result": original,
                        "current_state": current.get("data")}
            self.service.require_access(user_id=user_id, company_id=company_id, modify=True)
            if row["status"] != "accepted" or row["session_id"] != session_id or row["turn_id"] != turn_id:
                connection.rollback()
                return {"status": "plan_not_active", "summary": "计划已撤回、完成或不属于当前决定。"}
            current = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (company_id, row["invoice_id"])).fetchone()
            if current is None:
                connection.rollback()
                return {"status": "not_found", "summary": "当前票据已不存在。"}
            snapshot = dict(current)
            failed = [action["action_id"] for action in remaining if not _condition_met(action["condition"], snapshot)]
            if failed:
                connection.rollback()
                return {"status": "precondition_not_met", "action_ids": failed, "summary": "当前内容不满足原定条件，未执行这些动作。"}
            changes = {action["field"]: action["value"] for action in remaining if action["mode"] in {"commit", "clear"}}
            if changes:
                result = self.service._update_invoice_in_transaction(user_id=user_id, company_id=company_id,
                    invoice_id=row["invoice_id"], changes=changes, expected_revision=int(row["expected_revision"]), idempotency_key=request_key)
                if result["status"] not in {"committed", "no_change"}:
                    connection.commit()
                    return result
            else:
                result = {"status": "no_change", "committed": False, "current": self.service.read_invoice(
                    user_id=user_id, company_id=company_id, invoice_id=row["invoice_id"])["data"]}
            task = connection.execute("""SELECT task_id FROM tasks WHERE user_id=? AND company_id=?
                AND status IN ('waiting_input','open','out_of_scope') ORDER BY updated_at DESC LIMIT 1""", (user_id, company_id)).fetchone()
            if changes:
                operation = connection.execute("""SELECT operation_id FROM operations WHERE user_id=? AND company_id=?
                    AND object_type='invoice' AND object_id=? AND idempotency_key=?""", (user_id, company_id, row["invoice_id"], request_key)).fetchone()
                saved = result.get("after") or result.get("current") or {}
                record_task_event(connection, user_id=user_id, company_id=company_id, session_id=session_id,
                    turn_id=turn_id, task_id=task["task_id"] if task else None, kind="invoice_saved",
                    payload={"invoice_id": row["invoice_id"], "fields": list(changes),
                             "source_operation_id": operation["operation_id"] if operation else None,
                             "saved_values": {field: saved[field] for field in changes if field in saved}})
            for action in remaining:
                if action["mode"] in {"draft", "defer"}:
                    kind = "draft_set" if action["mode"] == "draft" else "deferred"
                    event = {"invoice_id": row["invoice_id"], "field": action["field"]}
                    event.update({"value": action["value"], "save_requested": False} if action["mode"] == "draft" else {"reason": action["reason"]})
                    if action["mode"] == "draft":
                        event["value_source"] = action.get("value_source")
                    record_task_event(connection, user_id=user_id, company_id=company_id, session_id=session_id,
                        turn_id=turn_id, task_id=task["task_id"] if task else None, kind=kind, payload=event)
            next_revision = (result.get("after") or result.get("current") or snapshot)["revision"]
            consumed = len(prior_actions) + len(remaining)
            connection.execute("UPDATE invoice_action_plans SET expected_revision=?,status=? WHERE plan_id=?",
                (next_revision, "completed" if consumed == len(actions_by_id) else "accepted", plan_id))
            result = {**result, "plan_id": plan_id, "action_ids": action_ids,
                      "written_fields": list(changes),
                      "preserved_fields": [action["field"] for action in remaining if action["mode"] == "preserve"],
                      "drafted_fields": [action["field"] for action in remaining if action["mode"] == "draft"],
                      "deferred_fields": [action["field"] for action in remaining if action["mode"] == "defer"],
                      "previously_completed": [key for key in action_ids if key in prior_actions]}
            connection.executemany("INSERT INTO invoice_action_receipts(plan_id,action_id,result_json) VALUES(?,?,?)",
                                   ((plan_id, action["action_id"], _json(result)) for action in remaining))
            connection.execute("INSERT INTO invoice_action_requests VALUES(?,?,?,?,?)",
                               (user_id, company_id, request_key, request_hash, _json(result)))
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

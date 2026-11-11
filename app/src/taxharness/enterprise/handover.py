"""Compact, read-only handover summaries for enterprise work items."""

from __future__ import annotations

import json
from typing import Any

from .service import EnterpriseService
from .storage import EnterpriseDatabase
from .work_projection import project_enterprise_work


_COUNTER_CATEGORIES = ("needs_input", "awaiting_confirmation", "waiting", "awaiting_review", "completed", "excluded")
_ACTION_TEXT = {
    "request_information": "会计向经办人补充询问未完成事项。",
    "revise_returned_items": "会计按复核意见修订已退回事项。",
    "confirm_drafts": "会计确认待处理草稿；确认后交复核人办理。",
    "clarify_drafts": "会计先澄清草稿，再确认保存。",
    "resolve_draft_conflicts": "会计核对票据当前内容后处理冲突草稿。",
    "review_unavailable": "复核人决定无法补充的信息如何处置。",
    "wait_for_response": "等待经办人回复。",
    "confirm_ready_fields": "会计确认无冲突字段，再处理冲突项。",
    "review_invoices": "复核人审核已提交的票据。",
    "inspect_void_invoices": "会计检查作废票据后关闭工作项。",
    "close_work_item": "票据已完成，会计可以关闭工作项。",
}


def _has_table(database: EnterpriseDatabase, table: str) -> bool:
    return database.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _waiting_people(database: EnterpriseDatabase, task_id: str) -> list[dict[str, Any]]:
    rows = database.connection.execute(
        """SELECT r.request_id,r.respondent_user_id,r.message,r.target_invoice_ids_json,r.created_at,u.display_name,
                  (SELECT COUNT(*) FROM enterprise_responses p WHERE p.request_id=r.request_id) AS response_count
           FROM enterprise_requests r LEFT JOIN enterprise_users u ON u.user_id=r.respondent_user_id
           WHERE r.task_id=? AND r.status='open'
           ORDER BY r.created_at,r.request_id""",
        (task_id,),
    ).fetchall()
    people = {}
    for row in rows:
        person = people.setdefault(row["respondent_user_id"], {"name": row["display_name"] or "经办人", "open_request_count": 0, "requests": []})
        person["open_request_count"] += 1
        invoice_ids = json.loads(row["target_invoice_ids_json"])
        person["requests"].append({"request_id": row["request_id"], "message": row["message"],
                                   "invoice_count": len(invoice_ids), "invoice_ids": invoice_ids,
                                   "response_count": int(row["response_count"])})
    return list(people.values())


def _recent_changes(database: EnterpriseDatabase, task_id: str) -> list[dict[str, str]]:
    connection = database.connection
    changes: list[dict[str, str]] = []

    for row in connection.execute(
        """SELECT r.request_id,r.status,r.created_at,r.cancelled_at,u.display_name
           FROM enterprise_requests r LEFT JOIN enterprise_users u ON u.user_id=r.respondent_user_id
           WHERE r.task_id=? ORDER BY COALESCE(r.cancelled_at,r.created_at) DESC LIMIT 5""", (task_id,),
    ):
        if row["status"] == "open":
            changes.append({"at": row["created_at"], "request_id": row["request_id"], "summary": f"已向{row['display_name'] or '经办人'}发起补充说明请求。"})
        elif row["status"] == "cancelled" and row["cancelled_at"]:
            changes.append({"at": row["cancelled_at"], "request_id": row["request_id"], "summary": "一项补充说明请求已取消。"})

    for row in connection.execute(
        """SELECT p.request_id,p.created_at,p.extraction_status,u.display_name
           FROM enterprise_responses p JOIN enterprise_requests r USING(request_id)
           LEFT JOIN enterprise_users u ON u.user_id=p.submitted_by WHERE r.task_id=? ORDER BY p.created_at DESC LIMIT 5""", (task_id,),
    ):
        name = row["display_name"] or "经办人"
        summary = "已提交补充说明，待会计处理。" if row["extraction_status"] != "complete" else "已提交补充说明，回复已整理。"
        changes.append({"at": row["created_at"], "request_id": row["request_id"], "summary": f"{name}{summary}"})

    for row in connection.execute(
        """SELECT r.created_at,u.display_name FROM enterprise_confirmation_receipts r
           JOIN enterprise_drafts d USING(draft_id)
           LEFT JOIN enterprise_users u ON u.user_id=r.confirmed_by WHERE d.task_id=? ORDER BY r.created_at DESC LIMIT 5""", (task_id,),
    ):
        changes.append({"at": row["created_at"], "summary": f"{row['display_name'] or '会计'}已确认一项字段修改。"})

    action_text = {"approve": "已通过一项票据复核", "return": "已退回一项票据", "waive": "已批准一项豁免"}
    for row in connection.execute(
        """SELECT r.created_at,r.action,u.display_name FROM enterprise_reviews r
           LEFT JOIN enterprise_users u ON u.user_id=r.reviewer_user_id WHERE r.task_id=? ORDER BY r.created_at DESC LIMIT 5""", (task_id,),
    ):
        changes.append({"at": row["created_at"], "summary": f"{row['display_name'] or '复核人'}{action_text[row['action']]}。"})

    if _has_table(database, "enterprise_extraction_runs"):
        state_text = {
            "queued": "回复处理已排队。",
            "running": "回复正在处理。",
            "completed": "一条回复处理完成。",
            "partially_applied": "一条回复已部分整理，剩余内容待核对。",
            "needs_attention": "一条回复需要会计查看。",
            "failed": "一条回复处理失败，待会计查看。",
            "cancelled": "一条回复处理已取消。",
            "stale": "一条回复处理结果已过期。",
        }
        for row in connection.execute(
            "SELECT updated_at,state FROM enterprise_extraction_runs WHERE task_id=? ORDER BY updated_at DESC LIMIT 5",
            (task_id,),
        ):
            if row["state"] in state_text:
                changes.append({"at": row["updated_at"], "summary": state_text[row["state"]]})

    return sorted(changes, key=lambda item: (item["at"], item["summary"]), reverse=True)[:5]


def _deliveries(database: EnterpriseDatabase, task_id: str) -> list[dict[str, Any]]:
    if not _has_table(database, "enterprise_deliveries"):
        return []
    rows = database.connection.execute(
        """SELECT delivery_id,version,status,created_at FROM enterprise_deliveries
           WHERE task_id=? ORDER BY version DESC LIMIT 5""",
        (task_id,),
    ).fetchall()
    return [
        {"delivery_id": row["delivery_id"], "version": int(row["version"]), "status": row["status"], "created_at": row["created_at"]}
        for row in rows
    ]


def _next_step(waiting_for: list[dict[str, Any]], projection: dict[str, Any], task_status: str) -> str:
    if task_status == "closed":
        return "本期间工作项已关闭。"
    actions = projection["next_actions"]
    priorities = ("confirm_ready_fields", "confirm_drafts", "review_invoices", "resolve_draft_conflicts",
                  "clarify_drafts", "review_unavailable", "revise_returned_items", "request_information",
                  "inspect_void_invoices", "close_work_item", "wait_for_response")
    choice = next((item for name in priorities for item in actions if item["action"] == name), None)
    if choice and choice["action"] != "wait_for_response":
        action = choice["action"]
        waiting_ids = {invoice_id for person in waiting_for for request in person["requests"] for invoice_id in request["invoice_ids"]}
        action_ids = set(choice.get("invoice_ids") or ([choice["invoice_id"]] if choice.get("invoice_id") else []))
        if action in {"confirm_ready_fields", "confirm_drafts", "review_invoices"} and action_ids & waiting_ids:
            unaffected = action_ids - waiting_ids
            if unaffected:
                return f"可先办理 {len(unaffected)} 张没有待补请求的票；其余票据请核对补充事项是否为确认前必需。"
            return "当前票据已有待处理内容，同时还有补充请求未回复；请核对补充事项是否为确认前必需。"
        if action == "confirm_drafts":
            result = f"会计确认 {len(choice.get('draft_ids', []))} 条待处理草稿；确认后交复核人办理。"
        elif action == "review_invoices":
            result = f"复核人审核 {len(choice.get('invoice_ids', []))} 张已提交票据。"
        else:
            result = _ACTION_TEXT[action]
        if waiting_for:
            result += " 另有补充请求等待经办人回复。"
        return result
    if waiting_for:
        names = "、".join(dict.fromkeys(person["name"] for person in waiting_for))
        return f"等待{names}回复；收到后由会计核对说明并处理草稿。"
    return "当前没有待推进事项。"


def build_handover(
    database: EnterpriseDatabase,
    *,
    user: dict,
    task_id: str,
    include_projection: bool = False,
    detail_page: int | None = None,
) -> dict[str, Any]:
    """Build a small task-scoped handover view using the caller's current role."""

    service = EnterpriseService(database)
    if detail_page is not None and (type(detail_page) is not int or detail_page < 1):
        raise ValueError("详情页码必须为正整数。")
    owns_transaction = not database.connection.in_transaction
    if owns_transaction:
        database.connection.execute("BEGIN")
    try:
        projection = project_enterprise_work(database, user=user, task_id=task_id, include_review_basis=False)
        task = projection["task"]
        category_counts = {category: len(projection[category]) for category in _COUNTER_CATEGORIES}
        completed = category_counts["completed"]
        excluded = category_counts["excluded"]
        waiting_for = _waiting_people(database, task_id)
        result = {
            "status": "ok", "task_id": task_id, "company": task["company_name"], "period": task["period"],
            "task_status": task["status"], "task_revision": int(task["revision"]),
            "counts": {"total": int(projection["target_invoice_count"]), "completed": completed,
                "unfinished": int(projection["target_invoice_count"]) - completed - excluded,
                "awaiting_confirmation": category_counts["awaiting_confirmation"],
                "awaiting_review": category_counts["awaiting_review"], "waiting": category_counts["waiting"],
                "needs_input": category_counts["needs_input"], "excluded": excluded},
            "waiting_for": waiting_for, "recent_changes": _recent_changes(database, task_id),
            "deliveries": _deliveries(database, task_id), "next_step": _next_step(waiting_for, projection, task["status"]),
        }
        if include_projection:
            result["projection"] = projection
        if detail_page is not None:
            result["detail"] = service.work_item(user=user, task_id=task_id, page=detail_page, page_size=50)
        if owns_transaction:
            database.connection.commit()
        return result
    except Exception:
        if owns_transaction:
            database.connection.rollback()
        raise

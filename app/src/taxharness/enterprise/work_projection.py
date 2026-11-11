"""Read-only work view shared by enterprise pages and conversation clients."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from .field_evaluation import evaluate_draft_fields
from .service import EnterpriseService
from .storage import EnterpriseDatabase


_ACTIVE_DRAFT_STATUSES = {"proposed", "clarification", "unavailable", "conflict"}
_CATEGORY_NAMES = ("needs_input", "awaiting_confirmation", "waiting", "awaiting_review", "completed", "excluded")


def _draft_field_source(draft: dict, field: str) -> dict | None:
    value = draft.get("proposed_" + field)
    if value is None:
        return None
    context = (draft.get("field_context") or {}).get(field, {})
    evidence = context.get("evidence_source") or {}
    if context.get("source_confirmation"):
        return {"kind": "accountant_confirmed_suggestion", "label": "会计确认过的建议"}
    if evidence.get("type") == "source_document":
        return {"kind": "imported_source", "label": "导入资料", "document_id": evidence.get("document_id"),
                "locator": evidence.get("locator")}
    if evidence.get("type") == "reply_attachment":
        return {"kind": "requester_attachment", "label": "经办人附件", "attachment_id": evidence.get("attachment_id"),
                "page": evidence.get("page"), "verification": evidence.get("verification")}
    if evidence.get("type") == "requester_reply":
        exact = value in str(context.get("evidence_quote") or draft.get("evidence_quote") or "")
        return {"kind": "requester_reply" if exact else "reply_interpretation",
                "label": "经办人回复原话" if exact else "经办人回复整理出的建议，需核对"}
    if context.get("source_recorded") or draft.get("origin") == "chat":
        if value and value not in str(context.get("evidence_quote") or draft.get("evidence_quote") or ""):
            return {"kind": "unverified_suggestion", "label": "待核对的整理建议"}
        return {"kind": "accountant_message", "label": "会计的说明或更正"}
    if draft.get("response_id"):
        exact = value in str(context.get("evidence_quote") or draft.get("evidence_quote") or "")
        return {"kind": "requester_reply" if exact else "reply_interpretation",
                "label": "经办人回复原话" if exact else "经办人回复整理出的建议，需核对"}
    return {"kind": "unknown", "label": "来源待核对"}


def project_enterprise_work(database: EnterpriseDatabase, *, user: dict, task_id: str, include_review_basis: bool = True) -> dict[str, Any]:
    """Project the stored work item, live invoice rows, and drafts without mutating them."""

    service = EnterpriseService(database)
    first = service.work_item(user=user, task_id=task_id, page=1, page_size=100, include_review_basis=include_review_basis)
    task = first["task"]
    invoice_page = first["invoices"]
    invoices = list(invoice_page["items"])
    for page in range(2, invoice_page["pages"] + 1):
        invoices.extend(service.work_item(user=user, task_id=task_id, page=page, page_size=100, include_review_basis=include_review_basis)["invoices"]["items"])

    invoice_by_id = {invoice["invoice_id"]: invoice for invoice in invoices}
    record_kind = "task_snapshot" if task["status"] == "closed" else "current_database"
    drafts = []
    drafts_by_invoice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in first["drafts"]:
        target_ids = list(dict.fromkeys(source["candidate_invoice_ids"] + ([source["invoice_id"]] if source["invoice_id"] else [])))
        current_versions = {
            invoice_id: invoice_by_id[invoice_id]["revision"]
            for invoice_id in target_ids if invoice_id in invoice_by_id
        }
        exact_invoice = invoice_by_id.get(source["invoice_id"])
        source_revision = source.get("source_revision")
        evaluation = evaluate_draft_fields(source, exact_invoice)
        version_state = evaluation["source_version_state"]
        draft = {
            "draft_id": source["draft_id"],
            "task_id": source["task_id"],
            "company_id": source["company_id"],
            "invoice_id": source["invoice_id"],
            "candidate_invoice_ids": target_ids,
            "status": source["status"],
            "proposed_fields": {
                "purpose": source["proposed_purpose"],
                "remarks": source["proposed_remarks"],
            },
            "evidence_quote": source["evidence_quote"],
            "field_sources": {field: _draft_field_source(source, field) for field in ("purpose", "remarks")
                              if source["proposed_" + field] is not None},
            "clarification_question": source["clarification_question"],
            "needs_clarification": source["needs_clarification"],
            "unavailable": source["unavailable"],
            "draft_revision": int(source["draft_revision"]),
            "source_invoice_revision": source_revision,
            "current_invoice_revisions": current_versions,
            "source_version_state": version_state,
            "invoice_revision_state": evaluation["invoice_revision_state"],
            "field_states": evaluation["field_states"],
            "field_conflicts": evaluation["field_conflicts"],
            "conflict_reason": (source.get("field_context") or {}).get("_conflict_reason", {}).get("kind"),
            "superseded_by": source.get("superseded_by"),
            "created_by": source["created_by"],
            "created_at": source["created_at"],
            "invoice_refs": source["invoice_refs"],
        }
        drafts.append(draft)
        for invoice_id in target_ids:
            drafts_by_invoice[invoice_id].append(draft)

    tickets = []
    categories: dict[str, list[str]] = {name: [] for name in _CATEGORY_NAMES}
    waiting_reasons: dict[str, str] = {}
    for invoice in invoices:
        invoice_id = invoice["invoice_id"]
        related = drafts_by_invoice.get(invoice_id, [])
        active = [draft for draft in related if draft["status"] in _ACTIVE_DRAFT_STATUSES and not draft["superseded_by"]]
        task_status = invoice["task_status"]
        has_purpose = bool(str(invoice.get("purpose") or "").strip())
        stale = any(draft["source_version_state"] == "stale" for draft in active)
        unavailable = any(draft["unavailable"] for draft in active)
        ambiguous = any(draft["needs_clarification"] or not draft["invoice_id"] for draft in active)
        conflict = any(draft["status"] == "conflict" for draft in active)
        proposed = [draft for draft in active if draft["status"] == "proposed" and draft["source_version_state"] != "stale"]

        if invoice.get("source_status") == "void":
            state, reason = "excluded", "invoice_void"
        elif stale or conflict:
            state, reason = "waiting", "invoice_revision_conflict"
        elif unavailable:
            state, reason = "waiting", "unavailable_needs_review_disposition"
        elif ambiguous:
            state, reason = "waiting", "needs_clarification"
        elif proposed:
            state, reason = "awaiting_confirmation", "draft_requires_accountant_confirmation"
        elif task_status in {"approved", "waived"}:
            state, reason = "completed", task_status
        elif task_status == "awaiting_info":
            state, reason = "waiting", "waiting_for_response"
        elif task_status == "returned":
            state, reason = "needs_input", "reviewer_returned"
        elif task_status == "awaiting_review" and not has_purpose:
            state, reason = "needs_input", "purpose_missing_before_review"
        elif task_status == "awaiting_review" or has_purpose:
            state, reason = "awaiting_review", "saved_pending_reviewer"
        else:
            state, reason = "needs_input", "purpose_missing"

        categories[state].append(invoice_id)
        if state == "waiting":
            waiting_reasons[invoice_id] = reason
        tickets.append({
            "invoice_id": invoice_id,
            "state": state,
            "reason": reason,
            "task_status": task_status,
            "invoice_record_kind": record_kind,
            "actual_invoice": dict(invoice),
            "draft_ids": [draft["draft_id"] for draft in related],
        })

    next_actions = []
    if categories["needs_input"]:
        returned = [ticket["invoice_id"] for ticket in tickets if ticket["state"] == "needs_input" and ticket["reason"] == "reviewer_returned"]
        missing = [invoice_id for invoice_id in categories["needs_input"] if invoice_id not in returned]
        if missing:
            next_actions.append({"action": "request_information", "invoice_ids": missing})
        if returned:
            next_actions.append({"action": "revise_returned_items", "invoice_ids": returned})
    if categories["awaiting_confirmation"]:
        invoice_ids = set(categories["awaiting_confirmation"])
        draft_ids = [draft["draft_id"] for draft in drafts if draft["status"] == "proposed" and draft["invoice_id"] in invoice_ids]
        next_actions.append({"action": "confirm_drafts", "draft_ids": draft_ids, "invoice_ids": categories["awaiting_confirmation"]})
    if categories["waiting"]:
        for reason, action in (
            ("needs_clarification", "clarify_drafts"),
            ("invoice_revision_conflict", "resolve_draft_conflicts"),
            ("unavailable_needs_review_disposition", "review_unavailable"),
            ("waiting_for_response", "wait_for_response"),
        ):
            invoice_ids = [invoice_id for invoice_id, value in waiting_reasons.items() if value == reason]
            if invoice_ids:
                next_actions.append({"action": action, "invoice_ids": invoice_ids})
        for draft in drafts:
            if (draft["invoice_id"] in categories["waiting"] and draft["field_conflicts"] and
                    (draft["status"] == "proposed" or draft["conflict_reason"] == "field_conflict") and not draft["superseded_by"] and
                    not draft["needs_clarification"] and not draft["unavailable"]):
                ready = [field for field, value in draft["field_states"].items() if value["state"] == "ready"]
                if ready:
                    next_actions.append({"action": "confirm_ready_fields", "invoice_id": draft["invoice_id"],
                                         "draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"],
                                         "fields": ready, "conflicting_fields": list(draft["field_conflicts"])})
    if categories["awaiting_review"]:
        next_actions.append({"action": "review_invoices", "invoice_ids": categories["awaiting_review"]})
    if categories["excluded"]:
        next_actions.append({"action": "inspect_void_invoices", "invoice_ids": categories["excluded"]})
    if categories["completed"] and len(categories["completed"]) == len(invoices) and task["status"] != "closed":
        next_actions.append({"action": "close_work_item", "task_id": task_id})

    response_counts = {row["request_id"]: int(row["n"]) for row in database.connection.execute(
        """SELECT r.request_id,COUNT(p.response_id) AS n FROM enterprise_requests r
           LEFT JOIN enterprise_responses p USING(request_id) WHERE r.task_id=? GROUP BY r.request_id""", (task_id,))}
    requests = [{"request_id": row["request_id"], "status": row["status"], "message": row["message"],
                 "respondent_user_id": row["respondent_user_id"], "target_invoice_ids": json.loads(row["target_invoice_ids_json"]),
                 "response_count": response_counts[row["request_id"]]} for row in first["requests"]]

    return {
        "status": "ok",
        "task": task,
        "target_invoice_count": len(invoices),
        "invoice_record_kind": record_kind,
        "tickets": tickets,
        **categories,
        "waiting_reasons": waiting_reasons,
        "drafts": drafts,
        "requests": requests,
        "next_actions": next_actions,
    }

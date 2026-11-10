"""Read-only work view shared by enterprise pages and conversation clients."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .service import EnterpriseService
from .storage import EnterpriseDatabase


_ACTIVE_DRAFT_STATUSES = {"proposed", "clarification", "unavailable", "conflict"}
_CATEGORY_NAMES = ("needs_input", "awaiting_confirmation", "waiting", "awaiting_review", "completed", "excluded")


def project_enterprise_work(database: EnterpriseDatabase, *, user: dict, task_id: str) -> dict[str, Any]:
    """Project the stored work item, live invoice rows, and drafts without mutating them."""

    service = EnterpriseService(database)
    first = service.work_item(user=user, task_id=task_id, page=1, page_size=100)
    task = first["task"]
    invoice_page = first["invoices"]
    invoices = list(invoice_page["items"])
    for page in range(2, invoice_page["pages"] + 1):
        invoices.extend(service.work_item(user=user, task_id=task_id, page=page, page_size=100)["invoices"]["items"])

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
        if exact_invoice is None or source_revision is None:
            version_state = "not_comparable"
        else:
            version_state = "current" if int(source_revision) == int(exact_invoice["revision"]) else "stale"
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
            "clarification_question": source["clarification_question"],
            "needs_clarification": source["needs_clarification"],
            "unavailable": source["unavailable"],
            "draft_revision": int(source["draft_revision"]),
            "source_invoice_revision": source_revision,
            "current_invoice_revisions": current_versions,
            "source_version_state": version_state,
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
    if categories["awaiting_review"]:
        next_actions.append({"action": "review_invoices", "invoice_ids": categories["awaiting_review"]})
    if categories["excluded"]:
        next_actions.append({"action": "inspect_void_invoices", "invoice_ids": categories["excluded"]})
    if categories["completed"] and len(categories["completed"]) == len(invoices) and task["status"] != "closed":
        next_actions.append({"action": "close_work_item", "task_id": task_id})

    return {
        "status": "ok",
        "task": task,
        "target_invoice_count": len(invoices),
        "invoice_record_kind": record_kind,
        "tickets": tickets,
        **categories,
        "waiting_reasons": waiting_reasons,
        "drafts": drafts,
        "next_actions": next_actions,
    }

"""Versioned professional review methods and acknowledged, source-backed facts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import RuleConflictError, RuleNotFoundError
from ..money import cents_to_text
from ..rule_impact import preview_purpose_rule
from ..rules import applicable_rule
from ..services import TaxService
from .auth import require_role
from .imports import _document_identity_key, _weak_identity_tokens
from .storage import utc_now


DEFAULT_METHOD = {
    "name": "企业默认专业复核方法",
    "criteria": {
        "purpose_support_required": True,
        "identity_clear_required": True,
        "reviewable_required": True,
        "alternative_support": True,
    },
}
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CRITERIA = ("purpose_support_required", "identity_clear_required", "reviewable_required")


class ProfessionalConflict(RuntimeError):
    """A fact or method changed after the user read its previous version."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _period(value: str) -> str:
    if not isinstance(value, str) or not _PERIOD.fullmatch(value):
        raise ValueError("期间必须为 YYYY-MM。")
    return value


def _method_row(database, *, company_id: str, period: str):
    return database.connection.execute(
        "SELECT * FROM enterprise_professional_methods WHERE company_id=? AND period=? ORDER BY version DESC LIMIT 1",
        (company_id, period),
    ).fetchone()


def _method_view(row, *, company_id: str, period: str) -> dict[str, Any]:
    if row is None:
        return {"company_id": company_id, "period": period, "version": 0,
                "name": DEFAULT_METHOD["name"], "criteria": dict(DEFAULT_METHOD["criteria"]), "source": "built_in"}
    return {"company_id": row["company_id"], "period": row["period"], "version": int(row["version"]),
            "name": row["method_name"], "criteria": json.loads(row["criteria_json"]),
            "previous_version": int(row["previous_version"]), "created_by": row["created_by"],
            "created_at": row["created_at"], "source": "institution"}


def _validated_method(name: str, criteria: dict[str, Any]) -> tuple[str, dict[str, bool]]:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ValueError("复核方法名称须为 1 到 120 个字符。")
    accepted_keys = set(_CRITERIA) | {"alternative_support"}
    if not isinstance(criteria, dict) or frozenset(criteria) not in {frozenset(_CRITERIA), frozenset(accepted_keys)}:
        raise ValueError("方法须明确填写用途有支持、身份明确、可复核三个要求。")
    if any(not isinstance(criteria[key], bool) for key in _CRITERIA):
        raise ValueError("方法要求必须使用 true/false。")
    if criteria.get("alternative_support", True) is not True:
        raise ValueError("专业方法至少保留一组有效支持即可通过的替代来源规则。")
    return name.strip(), {**{key: criteria[key] for key in _CRITERIA}, "alternative_support": True}


def _invoice_rows(database, *, company_id: str, period: str, invoice_ids: list[str] | None):
    if invoice_ids is not None and (not isinstance(invoice_ids, list) or any(not isinstance(item, str) or not item for item in invoice_ids)):
        raise ValueError("invoice_ids 必须是票据编号数组。")
    selected = sorted(set(invoice_ids)) if invoice_ids is not None else None
    query = """SELECT i.company_id,i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.amount_cents,
                  i.purpose,i.remarks,i.revision,x.amount_text,x.currency,x.source_status,
                  l.source_system,l.source_invoice_id
           FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
           LEFT JOIN enterprise_invoice_links l USING(company_id,invoice_id)
           WHERE i.company_id=? AND substr(i.issue_date,1,7)=?"""
    if selected is None:
        rows = database.connection.execute(query + " ORDER BY i.invoice_id", (company_id, period)).fetchall()
    else:
        rows = []
        for offset in range(0, len(selected), 500):
            batch = selected[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(database.connection.execute(query + f" AND i.invoice_id IN ({placeholders})", (company_id, period, *batch)).fetchall())
    by_id = {row["invoice_id"]: dict(row) for row in rows}
    if selected is None:
        return [by_id[key] for key in sorted(by_id)]
    missing = [invoice_id for invoice_id in selected if invoice_id not in by_id]
    if missing:
        raise ValueError("所选票据不属于当前企业期间。")
    return [by_id[invoice_id] for invoice_id in selected]


def _identity_candidates(database, *, company_id: str, invoice_id: str) -> list[dict[str, str]]:
    rows = database.connection.execute(
        """SELECT r.*,d.filename,d.original_available,d.file_hash AS stored_file_hash
           FROM enterprise_identity_reviews r JOIN enterprise_source_documents d USING(document_id)
           WHERE r.company_id=? AND r.status IN ('open','deferred') ORDER BY r.identity_review_id""",
        (company_id,),
    ).fetchall()
    if not rows:
        return []
    links = database.connection.execute(
        "SELECT source_data_json FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (company_id, invoice_id),
    ).fetchall()
    aliases = database.connection.execute(
        "SELECT source_json FROM enterprise_source_aliases WHERE company_id=? AND invoice_id=?", (company_id, invoice_id),
    ).fetchall()
    identities = database.connection.execute(
        "SELECT invoice_code,invoice_number FROM enterprise_document_identities WHERE company_id=? AND invoice_id=?", (company_id, invoice_id),
    ).fetchall()
    known_tokens = set()
    for row in links:
        known_tokens.update(_weak_identity_tokens(json.loads(row["source_data_json"])))
    for row in aliases:
        known_tokens.update(_weak_identity_tokens(json.loads(row["source_json"])))
    known_identities = {(row["invoice_code"], row["invoice_number"]) for row in identities}
    blocked = []
    for review in rows:
        source = json.loads(review["source_json"])
        tokens = _weak_identity_tokens(source)
        key = _document_identity_key(source)
        if tokens & known_tokens or key and key in known_identities:
            blocked.append({"identity_review_id": review["identity_review_id"],
                            "source_system": review["source_system"],
                            "source_invoice_id": review["source_invoice_id"], "status": review["status"]})
    return blocked


def _stored_attachment_match(database, *, company_id: str, invoice_id: str, attachment_id: str,
                             quote: str, version: str | None = None, page: int | None = None) -> tuple[bool, dict, str]:
    row = database.connection.execute("""SELECT a.*,q.target_invoice_ids_json FROM enterprise_original_attachments a
        LEFT JOIN enterprise_requests q ON q.request_id=a.request_id WHERE a.attachment_id=? AND a.company_id=?""",
        (attachment_id, company_id)).fetchone()
    if row is None:
        return False, {}, ""
    if row["source_kind"] == "reply":
        allowed = set(json.loads(row["target_invoice_ids_json"] or "[]"))
    else:
        allowed = {item[0] for item in database.connection.execute("SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=? AND company_id=?", (row["task_id"], company_id))}
    source_version = f"{row['sha256']}:{row['source_revision']}:{row['result_revision']}"
    if invoice_id not in allowed or (version is not None and version != source_version):
        return False, {}, source_version
    prepared = json.loads(row["prepared_json"] or "{}")
    pages = [item for item in prepared.get("pages", []) if quote in str(item.get("text") or "") and
             (page is None or page == item.get("page"))]
    recognized = json.loads(row["recognized_json"] or "{}")
    documents = [item for item in recognized.get("documents", []) if page is None or item.get("page") == page]
    candidate_text = json.dumps(documents, ensure_ascii=False)
    matches_candidate = quote in candidate_text
    quote_start = str(pages[0].get("text") or "").find(quote) if pages else candidate_text.find(quote)
    locator = {"attachment_id": attachment_id, "task_id": row["task_id"], "request_id": row["request_id"],
               "pages": [item["page"] for item in pages] if pages else sorted({item["page"] for item in documents if item.get("page")}), "quote_start": quote_start,
               "quote_end": quote_start + len(quote) if quote_start >= 0 else None,
               "match_type": "text_layer" if pages else "recognized_candidate"}
    return bool(quote and (pages or matches_candidate)), locator, source_version


def _source_text_match(database, *, company_id: str, invoice_id: str, source_type: str,
                       source_id: str, source_version: str, source_quote: str) -> tuple[bool, dict[str, Any]]:
    if source_type == "requester_reply":
        row = database.connection.execute(
            """SELECT r.reply_text,r.created_at,q.request_id,q.target_invoice_ids_json
               FROM enterprise_responses r JOIN enterprise_requests q USING(request_id)
               WHERE r.response_id=? AND q.company_id=?""", (source_id, company_id),
        ).fetchone()
        if row is None:
            return False, {}
        quote_at = row["reply_text"].find(source_quote)
        version = hashlib.sha256(row["reply_text"].encode("utf-8")).hexdigest()
        targets = json.loads(row["target_invoice_ids_json"])
        return version == source_version and quote_at >= 0 and invoice_id in targets, {
            "request_id": row["request_id"], "response_id": source_id,
            "quote_start": quote_at, "quote_end": quote_at + len(source_quote),
        }
    if source_type == "source_document":
        document = database.connection.execute(
            """SELECT file_hash,file_content,original_available FROM enterprise_source_documents
               WHERE document_id=? AND company_id=?""", (source_id, company_id),
        ).fetchone()
        aliases = database.connection.execute(
            """SELECT alias_id,locator_json,source_json FROM enterprise_source_aliases
               WHERE document_id=? AND company_id=? AND invoice_id=? AND status='linked'
               ORDER BY row_number,alias_id""", (source_id, company_id, invoice_id),
        ).fetchall()
        if document is None or not document["original_available"] or document["file_content"] is None or not aliases:
            return False, {}
        if document["file_hash"] != source_version:
            return False, {}
        content = bytes(document["file_content"])
        searchable_rows = "\n".join(alias["source_json"] for alias in aliases)
        quote_at = searchable_rows.find(source_quote)
        all_aliases_for_document = database.connection.execute(
            "SELECT COUNT(*) FROM enterprise_source_aliases WHERE document_id=? AND company_id=? AND status='linked'",
            (source_id, company_id),
        ).fetchone()[0]
        if quote_at < 0 and all_aliases_for_document == len(aliases) == 1:
            quote_at = content.decode("utf-8-sig", "replace").find(source_quote)
        return quote_at >= 0, {"document_id": source_id, "quote_start": quote_at,
                               "aliases": [{"alias_id": row["alias_id"], "locator": json.loads(row["locator_json"])} for row in aliases]}
    if source_type == "assistant_attachment":
        stored = database.connection.execute("SELECT 1 FROM enterprise_original_attachments WHERE attachment_id=? AND company_id=?", (source_id, company_id)).fetchone()
        if stored:
            matches, locator, _ = _stored_attachment_match(database, company_id=company_id, invoice_id=invoice_id,
                                                            attachment_id=source_id, quote=source_quote, version=source_version)
            return matches, locator
        row = database.connection.execute(
            """SELECT a.sha256,a.extracted_json,a.turn_id,a.task_id
               FROM enterprise_assistant_attachments a JOIN enterprise_work_items w USING(task_id)
               WHERE a.attachment_id=? AND a.company_id=? AND w.company_id=?""", (source_id, company_id, company_id),
        ).fetchone()
        if row is None or row["sha256"] != source_version:
            return False, {}
        extracted = json.loads(row["extracted_json"])
        searchable = str(extracted.get("pdf_text", "")) + "\n" + json.dumps(extracted.get("documents", []), ensure_ascii=False)
        quote_at = searchable.find(source_quote)
        if quote_at < 0:
            return False, {}
        page_texts = str(extracted.get("pdf_text", "")).split("\f")
        pages = [index + 1 for index, page in enumerate(page_texts) if source_quote in page]
        return True, {"attachment_id": source_id, "task_id": row["task_id"], "turn_id": row["turn_id"],
                      "pages": pages, "quote_start": quote_at}
    raise ValueError("来源类型不受支持。")


def _purpose_rule_baseline(database, *, user_id: str, company_id: str, period: str) -> dict[str, Any]:
    try:
        rule = applicable_rule(database.connection, kind="invoice_purpose", company_id=company_id, month=period)
    except RuleNotFoundError as error:
        return {"status": "not_found", "summary": str(error)}
    except RuleConflictError as error:
        return {"status": "conflict", "summary": str(error)}
    condition = rule["condition"]
    condition_type = condition["type"]
    threshold = cents_to_text(int(condition["amount_cents"])) if condition_type == "amount_gte" else None
    preview = preview_purpose_rule(database, user_id=user_id, company_id=company_id, month=period,
                                  condition_type=condition_type, threshold_yuan=threshold)
    return {"status": "ok", "current_rule": {key: rule.get(key) for key in ("rule_id", "version", "name", "effective_from", "condition")},
            "preview_only": True, "impact": preview}


def review_basis(database, *, user: dict, company_id: str, period: str,
                 invoice_ids: list[str] | None = None, _method_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the current per-invoice review basis without opening transactions or writing state."""
    period = _period(period)
    require_role(database, user, company_id, {"accountant", "reviewer"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id)
    stored_method = _method_row(database, company_id=company_id, period=period)
    method = _method_override or _method_view(stored_method, company_id=company_id, period=period)
    invoices = _invoice_rows(database, company_id=company_id, period=period, invoice_ids=invoice_ids)
    result_rows = []
    for invoice in invoices:
        invoice_id = invoice["invoice_id"]
        fact_rows = database.connection.execute(
            """SELECT f.fact_id,f.fact_type,f.source_type,f.source_id,f.source_version,f.source_quote,f.source_quote_start,
                      f.fact_text,t.current_revision,t.status AS target_status,t.text_override,t.source_locator_json
               FROM enterprise_professional_facts f JOIN enterprise_professional_fact_targets t USING(fact_id,company_id)
               WHERE f.company_id=? AND f.period=? AND t.invoice_id=? ORDER BY f.fact_id""",
            (company_id, period, invoice_id),
        ).fetchall()
        groups: list[dict[str, Any]] = []
        withdrawn = []
        for row in fact_rows:
            group = {"support_group_id": "fact:" + row["fact_id"], "kind": "acknowledged_fact",
                     "fact_id": row["fact_id"], "fact_type": row["fact_type"],
                     "target_revision": int(row["current_revision"]),
                     "fact_text": row["text_override"] if row["text_override"] is not None else row["fact_text"],
                     "source_type": row["source_type"], "source_id": row["source_id"],
                     "source_version": row["source_version"], "source_quote": row["source_quote"],
                     "source_quote_start": int(row["source_quote_start"]),
                     "source_locator": json.loads(row["source_locator_json"]),
                     "target_status": row["target_status"]}
            group["reviewable"], group["source_status"] = _source_text_match(
                database, company_id=company_id, invoice_id=invoice_id, source_type=row["source_type"],
                source_id=row["source_id"], source_version=row["source_version"], source_quote=row["source_quote"],
            )
            if row["target_status"] == "active":
                groups.append(group)
            else:
                withdrawn.append({"fact_id": row["fact_id"], "fact_type": row["fact_type"],
                                  "target_revision": int(row["current_revision"]), "status": "withdrawn",
                                  "source_type": row["source_type"], "source_id": row["source_id"],
                                  "source_version": row["source_version"], "source_quote": row["source_quote"]})

        confirmed = database.connection.execute(
            """SELECT d.draft_id,d.task_id,d.draft_revision,d.proposed_purpose,d.evidence_quote,d.field_context_json,
                      d.source_revision,d.response_id,d.source_turn_id,r.reply_text
               FROM enterprise_drafts d JOIN enterprise_confirmation_receipts receipt USING(draft_id)
               LEFT JOIN enterprise_responses r USING(response_id)
               WHERE d.company_id=? AND d.task_id IN (SELECT task_id FROM enterprise_work_items WHERE company_id=? AND period=?)
                 AND d.invoice_id=? AND d.status='confirmed' AND d.superseded_by IS NULL AND d.proposed_purpose IS NOT NULL
               ORDER BY receipt.created_at DESC,receipt.rowid DESC LIMIT 1""", (company_id, company_id, period, invoice_id),
        ).fetchall()
        for draft in confirmed:
            if not str(draft["proposed_purpose"] or "").strip():
                continue
            context = json.loads(draft["field_context_json"] or "{}")
            purpose_context = context.get("purpose", {})
            quote = purpose_context.get("evidence_quote") or draft["evidence_quote"]
            evidence = purpose_context.get("evidence_source") or {}
            if evidence.get("type") == "source_document":
                source_id, version = evidence.get("document_id", ""), evidence.get("file_hash", "")
                matched, locator = _source_text_match(database, company_id=company_id, invoice_id=invoice_id,
                    source_type="source_document", source_id=source_id, source_version=version, source_quote=quote)
                aliases = locator.get("aliases", [])
                matched = matched and any(item.get("alias_id") == evidence.get("alias_id") for item in aliases)
                groups.append({"support_group_id": "confirmed-draft:" + draft["draft_id"], "kind": "confirmed_import_draft",
                    "draft_id": draft["draft_id"], "draft_revision": int(draft["draft_revision"]),
                    "source_revision": draft["source_revision"], "fact_type": "purpose", "fact_text": draft["proposed_purpose"],
                    "source_type": "source_document", "source_id": source_id, "source_version": version,
                    "source_quote": quote, "source_quote_start": locator.get("quote_start", -1), "source_locator": locator,
                    "reviewable": matched, "source_status": "available" if matched else "source_changed", "target_status": "active"})
                continue
            if evidence.get("type") == "reply_attachment":
                version = f"{evidence.get('sha256')}:{evidence.get('source_revision')}:{evidence.get('result_revision')}"
                matched, locator, _ = _stored_attachment_match(database, company_id=company_id, invoice_id=invoice_id,
                    attachment_id=evidence.get("attachment_id", ""), quote=quote, version=version, page=evidence.get("page"))
                groups.append({"support_group_id": "confirmed-draft:" + draft["draft_id"], "kind": "confirmed_attachment_draft",
                    "draft_id": draft["draft_id"], "draft_revision": int(draft["draft_revision"]),
                    "source_revision": draft["source_revision"], "fact_type": "purpose", "fact_text": draft["proposed_purpose"],
                    "source_type": "assistant_attachment", "source_id": evidence.get("attachment_id"),
                    "source_version": version, "source_quote": quote, "source_quote_start": 0,
                    "source_locator": locator, "reviewable": matched, "source_status": "available" if matched else "source_changed",
                    "target_status": "active"})
                continue
            confirmation = purpose_context.get("source_confirmation") or {}
            if confirmation:
                confirmed_message = database.connection.execute("""SELECT content,user_id FROM enterprise_actor_messages
                    WHERE turn_id=? AND company_id=? AND task_id=? LIMIT 1""",
                    (confirmation.get("confirmed_turn_id"), company_id, draft["task_id"])).fetchone()
                prompt = str(confirmation.get("prompt") or "")
                matched = bool(confirmed_message and confirmed_message["content"] == confirmation.get("confirmation") and
                               draft["proposed_purpose"] in prompt and confirmation.get("displayed_turn_id"))
                source_text = prompt + "\n用户确认：" + str(confirmation.get("confirmation") or "")
                groups.append({"support_group_id": "confirmed-draft:" + draft["draft_id"], "kind": "confirmed_dialogue_proposal",
                    "draft_id": draft["draft_id"], "draft_revision": int(draft["draft_revision"]),
                    "source_revision": draft["source_revision"], "fact_type": "purpose", "fact_text": draft["proposed_purpose"],
                    "source_type": "user_confirmation", "source_id": confirmation.get("confirmed_turn_id"),
                    "source_version": hashlib.sha256(source_text.encode("utf-8")).hexdigest(), "source_quote": source_text,
                    "source_quote_start": 0, "source_locator": {"displayed_turn_id": confirmation.get("displayed_turn_id"),
                        "confirmed_turn_id": confirmation.get("confirmed_turn_id")}, "reviewable": matched,
                    "source_status": "available" if matched else "source_changed", "target_status": "active"})
                continue
            turn_id = purpose_context.get("source_turn_id") or draft["source_turn_id"]
            actor_source = database.connection.execute("""SELECT turn_id,content,content_hash FROM enterprise_actor_messages
                WHERE turn_id=? AND company_id=? AND task_id=? ORDER BY created_at DESC LIMIT 1""", (turn_id, company_id, draft["task_id"])).fetchone() if turn_id else None
            if actor_source and quote and quote in actor_source["content"]:
                source_text, source_id, source_version = actor_source["content"], turn_id, actor_source["content_hash"]
                source_type, kind = "actor_message", "confirmed_actor_draft"
            else:
                source_id = evidence.get("response_id") if evidence.get("type") == "requester_reply" else draft["response_id"]
                reply_source = database.connection.execute("""SELECT r.reply_text,q.target_invoice_ids_json FROM enterprise_responses r
                    JOIN enterprise_requests q USING(request_id) WHERE r.response_id=? AND q.company_id=? AND q.task_id=?""",
                    (source_id, company_id, draft["task_id"])).fetchone() if source_id else None
                source_text = reply_source["reply_text"] if reply_source and invoice_id in json.loads(reply_source["target_invoice_ids_json"]) else ""
                source_version = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text else ""
                source_type, kind = "requester_reply", "confirmed_reply_draft"
            quote_at = source_text.find(quote) if quote else -1
            groups.append({"support_group_id": "confirmed-draft:" + draft["draft_id"], "kind": kind,
                           "draft_id": draft["draft_id"], "draft_revision": int(draft["draft_revision"]),
                           "source_revision": draft["source_revision"], "fact_type": "purpose",
                           "fact_text": draft["proposed_purpose"], "source_type": source_type,
                           "source_id": source_id, "source_version": source_version,
                           "source_quote": quote, "source_quote_start": quote_at,
                           "source_locator": {"source_id": source_id, "quote_start": quote_at,
                                              "quote_end": quote_at + len(quote) if quote_at >= 0 else None},
                           "reviewable": quote_at >= 0,
                           "source_status": "available" if quote_at >= 0 else "quote_not_found",
                           "target_status": "active"})

        linked = database.connection.execute(
            """SELECT l.source_system,l.source_invoice_id,l.source_hash FROM enterprise_invoice_links l
               WHERE l.company_id=? AND l.invoice_id=? ORDER BY l.source_system,l.source_invoice_id""",
            (company_id, invoice_id),
        ).fetchall()
        linked_aliases = database.connection.execute(
            """SELECT source_system,source_invoice_id,content_hash FROM enterprise_source_aliases
               WHERE company_id=? AND invoice_id=? AND status='linked' ORDER BY source_system,source_invoice_id,content_hash""",
            (company_id, invoice_id),
        ).fetchall()
        pending_identity = _identity_candidates(database, company_id=company_id, invoice_id=invoice_id)
        identity_clear = bool(linked or linked_aliases) and not pending_identity
        purpose_groups = [group for group in groups if group["fact_type"] == "purpose" and group["reviewable"]]
        raw_purpose_groups = [group for group in groups if group["fact_type"] == "purpose"]
        purpose_values = sorted({str(group["fact_text"]).strip() for group in purpose_groups if str(group["fact_text"]).strip()})
        current_purpose = str(invoice["purpose"] or "").strip()
        conflict_reasons = []
        if len(purpose_values) > 1:
            conflict_reasons.append({"kind": "accepted_sources_disagree", "values": purpose_values,
                                     "support_group_ids": [group["support_group_id"] for group in purpose_groups]})
        if current_purpose and purpose_values and current_purpose not in purpose_values:
            conflict_reasons.append({"kind": "saved_purpose_differs", "current_value": current_purpose,
                                     "accepted_values": purpose_values})
        invalid_sources = [{"support_group_id": group["support_group_id"], "reason": group["source_status"]}
                           for group in raw_purpose_groups if not group["reviewable"]]
        purpose_supported = bool(purpose_groups) and not conflict_reasons
        reviewable = any(group["reviewable"] for group in groups)
        criteria = method["criteria"]
        requirements = [
            {"id": "purpose_supported", "label": "用途有支持", "required": bool(criteria["purpose_support_required"]),
             "satisfied": purpose_supported, "support_group_ids": [group["support_group_id"] for group in purpose_groups],
             "summary": "存在来源可回读的认可事实或经办回复草稿。" if purpose_supported else "尚无有效的用途支持来源。"},
            {"id": "identity_clear", "label": "身份明确", "required": bool(criteria["identity_clear_required"]),
             "satisfied": identity_clear, "support_group_ids": [],
             "summary": "已关联正式来源且无待判定候选。" if identity_clear else "来源身份尚未关联，或存在待判定候选。"},
            {"id": "reviewable", "label": "可复核", "required": bool(criteria["reviewable_required"]),
             "satisfied": reviewable, "support_group_ids": [group["support_group_id"] for group in groups if group["reviewable"]],
             "summary": "至少一项支持可通过来源版本和原话定位复核。" if reviewable else "目前没有可回读的来源支持。"},
        ]
        missing = [item["id"] for item in requirements if item["required"] and not item["satisfied"]]
        if conflict_reasons:
            status = "conflicting"
        elif pending_identity or invalid_sources:
            status = "awaiting_judgment"
        elif missing:
            status = "open"
        else:
            status = "supported"
        groups.sort(key=lambda group: (group["kind"], group["support_group_id"]))
        withdrawn.sort(key=lambda group: group["fact_id"])
        invoice_result = {
            "invoice_id": invoice_id, "issue_date": invoice["issue_date"], "revision": int(invoice["revision"]),
            "seller_name": invoice["seller_name"], "project_name": invoice["project_name"],
            "amount_text": invoice["amount_text"], "currency": invoice["currency"],
            "source_system": invoice["source_system"], "source_invoice_id": invoice["source_invoice_id"],
            "purpose": invoice["purpose"], "source_status": invoice["source_status"],
            "status": status, "requirements": requirements, "missing_requirements": missing,
            "conflicts": conflict_reasons, "awaiting_judgment": {"identity_reviews": pending_identity,
                                                                    "invalid_sources": invalid_sources},
            "support_groups": groups, "withdrawn_sources": withdrawn,
            "identity_sources": [{"source_system": row["source_system"], "source_invoice_id": row["source_invoice_id"],
                                   "source_version": row["source_hash"]} for row in linked]
                                  + [{"source_system": row["source_system"], "source_invoice_id": row["source_invoice_id"],
                                      "source_version": row["content_hash"]} for row in linked_aliases],
        }
        invoice_result["basis_revision"] = _digest({"company_id": company_id, "period": period, "method_version": method["version"],
                                                     "criteria": method["criteria"], "invoice_id": invoice_id,
                                                     "invoice_revision": invoice_result["revision"], "status": status,
                                                     "requirements": requirements, "conflicts": conflict_reasons,
                                                     "support_groups": groups, "withdrawn_sources": withdrawn,
                                                     "identity_sources": invoice_result["identity_sources"]})
        result_rows.append(invoice_result)
    counts = {status: sum(row["status"] == status for row in result_rows)
              for status in ("supported", "open", "conflicting", "awaiting_judgment")}
    result = {"status": "ok", "company_id": company_id, "period": period,
              "method": method, "invoices": result_rows, "summary": {"invoice_count": len(result_rows), **counts}}
    result["basis_revision"] = _digest({"company_id": company_id, "period": period,
                                        "method_version": method["version"], "criteria": method["criteria"],
                                        "invoices": [{"invoice_id": row["invoice_id"], "basis_revision": row["basis_revision"]}
                                                     for row in result_rows]})
    return result


def _approved_invoices(database, *, company_id: str, period: str) -> set[str]:
    rows = database.connection.execute(
        """SELECT r.task_id,r.invoice_id,r.action FROM enterprise_reviews r
           JOIN enterprise_work_items w USING(task_id)
           WHERE r.company_id=? AND w.company_id=? AND w.period=?
           ORDER BY r.created_at DESC,r.review_id DESC""", (company_id, company_id, period),
    )
    seen = set()
    approved = set()
    for row in rows:
        key = (row["task_id"], row["invoice_id"])
        if key in seen:
            continue
        seen.add(key)
        if row["action"] in {"approve", "waive"}:
            approved.add(row["invoice_id"])
    return approved


def _impact(before: dict[str, Any], after: dict[str, Any], database, *, company_id: str, period: str) -> dict[str, Any]:
    old = {row["invoice_id"]: row for row in before["invoices"]}
    new = {row["invoice_id"]: row for row in after["invoices"]}
    approved = _approved_invoices(database, company_id=company_id, period=period)
    changed = []
    unchanged = []
    for invoice_id in sorted(set(old) | set(new)):
        a, b = old.get(invoice_id), new.get(invoice_id)
        if a and b and a["basis_revision"] == b["basis_revision"]:
            unchanged.append(invoice_id)
            continue
        changed.append({"invoice_id": invoice_id, "before_status": a["status"] if a else None,
                        "after_status": b["status"] if b else None,
                        "before_missing": a["missing_requirements"] if a else [],
                        "after_missing": b["missing_requirements"] if b else [],
                        "previously_approved": invoice_id in approved,
                        "requires_re_review": invoice_id in approved})
    return {"affected_invoices": changed, "unchanged_invoice_ids": unchanged,
            "requires_re_review": [row["invoice_id"] for row in changed if row["requires_re_review"]]}


def _apply_review_impact(database, *, user: dict, company_id: str, period: str, event_id: str, impact: dict) -> dict:
    """Reopen only affected approvals; closed deliveries get linked follow-up work."""
    from .service import EnterpriseService
    connection = database.connection
    reopened, followups = set(), set()
    for invoice_id in impact.get("requires_re_review", []):
        tasks = connection.execute("""SELECT w.task_id,w.status FROM enterprise_work_items w
            JOIN enterprise_task_invoices t USING(task_id) WHERE w.company_id=? AND w.period=? AND t.invoice_id=?
            AND t.status IN ('approved','waived') ORDER BY w.created_at""", (company_id, period, invoice_id)).fetchall()
        for task in tasks:
            if task["status"] == "closed":
                followup = EnterpriseService(database)._ensure_task(company_id=company_id, period=period,
                    user_id=user["user_id"], invoice_ids=[invoice_id], origin="source_change")
                connection.execute("""INSERT OR IGNORE INTO enterprise_correction_links
                    (original_task_id,followup_task_id,invoice_id,event_id,created_by,created_at) VALUES(?,?,?,?,?,?)""",
                    (task["task_id"], followup["task_id"], invoice_id, event_id, user["user_id"], utc_now()))
                followups.add(followup["task_id"])
            else:
                connection.execute("""UPDATE enterprise_task_invoices SET status='pending',disposition=NULL,updated_at=?
                    WHERE task_id=? AND invoice_id=?""", (utc_now(), task["task_id"], invoice_id))
                reopened.add(task["task_id"])
        if tasks:
            connection.execute("""UPDATE enterprise_invoice_extra SET purpose_review_status='stale',disposition=NULL,updated_at=?
                WHERE company_id=? AND invoice_id=?""", (utc_now(), company_id, invoice_id))
    for task_id in reopened | followups:
        connection.execute("""UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=?
            WHERE task_id=? AND status<>'closed'""", (utc_now(), task_id))
    return {"reopened_task_ids": sorted(reopened), "followup_task_ids": sorted(followups)}


def preview_method(database, *, user: dict, company_id: str, period: str, method_name: str,
                   criteria: dict[str, Any]) -> dict[str, Any]:
    period = _period(period)
    require_role(database, user, company_id, {"accountant"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id)
    name, normalized = _validated_method(method_name, criteria)
    current = _method_view(_method_row(database, company_id=company_id, period=period), company_id=company_id, period=period)
    before = review_basis(database, user=user, company_id=company_id, period=period)
    candidate = {"company_id": company_id, "period": period, "version": current["version"] + 1,
                 "name": name, "criteria": normalized, "source": "preview"}
    after = review_basis(database, user=user, company_id=company_id, period=period, _method_override=candidate)
    rule_baseline = _purpose_rule_baseline(database, user_id=user["user_id"], company_id=company_id, period=period)
    token_payload = {"company_id": company_id, "period": period, "expected_version": current["version"],
                     "method_name": name, "criteria": normalized, "before_basis_revision": before["basis_revision"],
                     "rule_baseline": rule_baseline}
    preview_token = _digest(token_payload)
    return {"status": "ok", "preview_only": True, "company_id": company_id, "period": period,
            "current_method": current, "proposed_method": candidate, "expected_version": current["version"],
            "preview_token": preview_token, "impact": _impact(before, after, database, company_id=company_id, period=period),
            "before_summary": before["summary"], "after_summary": after["summary"],
            "purpose_rule_baseline": rule_baseline,
            "summary": "此预览只读取当前票据、来源和规则，不更改机构方法、票据或复核记录。"}


def _command_replay(database, *, command_id: str, company_id: str, operation: str, request_json: str) -> dict[str, Any] | None:
    row = database.connection.execute("SELECT * FROM enterprise_professional_commands WHERE command_id=?", (command_id,)).fetchone()
    if row is None:
        return None
    if row["company_id"] != company_id or row["operation"] != operation or row["request_json"] != request_json:
        raise ValueError("command_id 已用于不同的专业复核操作。")
    return json.loads(row["result_json"])


def publish_method(database, *, user: dict, company_id: str, period: str, method_name: str,
                   criteria: dict[str, Any], expected_version: int, preview_token: str,
                   command_id: str) -> dict[str, Any]:
    period = _period(period)
    require_role(database, user, company_id, {"accountant"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id, modify=True)
    name, normalized = _validated_method(method_name, criteria)
    if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
        raise ValueError("expected_version 必须是非负整数。")
    if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
        raise ValueError("command_id 必须为 1 到 200 个字符。")
    if not isinstance(preview_token, str) or not preview_token:
        raise ValueError("发布前请先读取只读预览。")
    request_json = json.dumps({"period": period, "method_name": name, "criteria": normalized,
                               "expected_version": expected_version, "preview_token": preview_token}, ensure_ascii=False, sort_keys=True)
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        replay = _command_replay(database, command_id=command_id, company_id=company_id,
                                 operation="publish_method", request_json=request_json)
        if replay:
            connection.commit()
            return replay
        current = _method_view(_method_row(database, company_id=company_id, period=period), company_id=company_id, period=period)
        if current["version"] != expected_version:
            raise ProfessionalConflict("机构方法已被其他用户更新，请重新预览。")
        fresh = preview_method(database, user=user, company_id=company_id, period=period, method_name=name, criteria=normalized)
        if fresh["preview_token"] != preview_token:
            raise ProfessionalConflict("相关事实、规则或票据已发生变化，请重新预览。")
        new_version = expected_version + 1
        now = utc_now()
        connection.execute(
            """INSERT INTO enterprise_professional_methods
               (company_id,period,version,method_name,criteria_json,previous_version,operation_id,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (company_id, period, new_version, name, json.dumps(normalized, ensure_ascii=False, sort_keys=True),
             expected_version, command_id, user["user_id"], now),
        )
        published = _method_view(_method_row(database, company_id=company_id, period=period), company_id=company_id, period=period)
        after = review_basis(database, user=user, company_id=company_id, period=period)
        continuation = _apply_review_impact(database, user=user, company_id=company_id, period=period,
                                            event_id=command_id, impact=fresh["impact"])
        result = {"status": "published", "method": published, "impact": fresh["impact"],
                  "continuation": continuation,
                  "basis_revision": after["basis_revision"],
                  "summary": "新方法已发布；仅适用于所选企业期间，历史复核记录保留。"}
        connection.execute(
            """INSERT INTO enterprise_professional_commands
               (command_id,company_id,operation,actor_user_id,request_json,result_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (command_id, company_id, "publish_method", user["user_id"], request_json,
             json.dumps(result, ensure_ascii=False, sort_keys=True), now),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def _source_payload(database, *, user: dict, company_id: str, period: str, source_type: str,
                    source_id: str, invoice_ids: list[str], source_quote: str,
                    attachment_root: str | Path | None = None) -> tuple[str, int, dict[str, Any]]:
    if not isinstance(source_type, str) or source_type not in {"requester_reply", "source_document", "assistant_attachment"}:
        raise ValueError("来源必须是经办人回复、企业已存原件或本人上传的附件。")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("请先选择可访问的来源记录。")
    if not isinstance(source_quote, str) or not source_quote.strip():
        raise ValueError("请提供来源原话或原件中的可定位内容。")
    quote = source_quote.strip()
    source_version = ""
    locator_by_invoice: dict[str, Any] = {}
    if source_type == "requester_reply":
        row = database.connection.execute(
            """SELECT r.reply_text,r.request_id,q.company_id,q.target_invoice_ids_json,w.period
               FROM enterprise_responses r JOIN enterprise_requests q USING(request_id)
               JOIN enterprise_work_items w USING(task_id) WHERE r.response_id=?""", (source_id,),
        ).fetchone()
        if row is None or row["company_id"] != company_id or row["period"] != period:
            raise PermissionError("该回复不属于当前企业和期间。")
        allowed = set(json.loads(row["target_invoice_ids_json"]))
        if not set(invoice_ids) <= allowed:
            raise PermissionError("只能把回复关联到原请求明确列出的票据。")
        quote_start = row["reply_text"].find(quote)
        if quote_start < 0:
            raise ValueError("引用内容必须是经办人实际回复中的原文。")
        source_version = hashlib.sha256(row["reply_text"].encode("utf-8")).hexdigest()
        for invoice_id in invoice_ids:
            locator_by_invoice[invoice_id] = {"request_id": row["request_id"], "response_id": source_id,
                                              "quote_start": quote_start, "quote_end": quote_start + len(quote)}
    elif source_type == "source_document":
        row = database.connection.execute(
            """SELECT file_hash,file_content,original_available FROM enterprise_source_documents
               WHERE document_id=? AND company_id=?""", (source_id, company_id),
        ).fetchone()
        if row is None or not row["original_available"] or row["file_content"] is None:
            raise PermissionError("原件不存在、不可用或不属于当前企业。")
        aliases = database.connection.execute(
            """SELECT alias_id,invoice_id,source_json,locator_json FROM enterprise_source_aliases
               WHERE document_id=? AND company_id=? AND status='linked' ORDER BY invoice_id,row_number,alias_id""",
            (source_id, company_id),
        ).fetchall()
        by_invoice: dict[str, list[dict[str, Any]]] = {}
        for alias in aliases:
            by_invoice.setdefault(alias["invoice_id"], []).append(alias)
        if any(invoice_id not in by_invoice for invoice_id in invoice_ids):
            raise PermissionError("所选票据没有关联到该原件。")
        content = bytes(row["file_content"])
        selected_aliases = [alias for alias in aliases if alias["invoice_id"] in invoice_ids]
        per_invoice_rows = {invoice_id: "\n".join(alias["source_json"] for alias in by_invoice[invoice_id])
                            for invoice_id in invoice_ids}
        quote_positions = {invoice_id: text.find(quote) for invoice_id, text in per_invoice_rows.items()}
        raw_quote_start = content.decode("utf-8-sig", "replace").find(quote)
        raw_is_ticket_specific = len(aliases) == len(selected_aliases) == 1 and len(invoice_ids) == 1
        if any(position < 0 for position in quote_positions.values()) and not (raw_is_ticket_specific and raw_quote_start >= 0):
            raise ValueError("引用内容必须能在原件或其已解析来源记录中定位。")
        quote_start = next(iter(quote_positions.values())) if all(position >= 0 for position in quote_positions.values()) else raw_quote_start
        source_version = row["file_hash"]
        for invoice_id in invoice_ids:
            locator_by_invoice[invoice_id] = {"document_id": source_id, "quote_start": quote_start,
                "aliases": [{"alias_id": alias["alias_id"], "locator": json.loads(alias["locator_json"])}
                            for alias in by_invoice[invoice_id]]}
    else:
        stored = database.connection.execute("""SELECT a.*,w.period FROM enterprise_original_attachments a
            JOIN enterprise_work_items w ON w.task_id=a.task_id WHERE a.attachment_id=? AND a.company_id=?""", (source_id, company_id)).fetchone()
        if stored:
            from .reply_attachments import get_attachment
            get_attachment(database, user=user, attachment_id=source_id)
            if stored["period"] != period:
                raise PermissionError("该附件不属于当前期间。")
            source_version = f"{stored['sha256']}:{stored['source_revision']}:{stored['result_revision']}"
            positions = []
            for invoice_id in invoice_ids:
                matches, locator, _ = _stored_attachment_match(database, company_id=company_id, invoice_id=invoice_id,
                    attachment_id=source_id, quote=quote, version=source_version)
                if not matches:
                    raise ValueError("引用内容必须能在附件的分页文字或已核对的识别候选中定位。")
                locator_by_invoice[invoice_id] = locator
                positions.append(locator["quote_start"])
            return source_version, positions[0], locator_by_invoice
        row = database.connection.execute(
            """SELECT a.sha256,a.extracted_json,a.task_id,a.turn_id,a.stored_name,w.company_id,w.period
               FROM enterprise_assistant_attachments a JOIN enterprise_work_items w USING(task_id)
               WHERE a.attachment_id=? AND a.company_id=? AND a.user_id=?""",
            (source_id, company_id, user["user_id"]),
        ).fetchone()
        if row is None or row["period"] != period:
            raise PermissionError("该附件不是当前用户在此企业期间上传的原件。")
        if attachment_root is not None:
            root = Path(attachment_root).resolve()
            stored_name = row["stored_name"]
            if Path(stored_name).name != stored_name or (root / stored_name).resolve().parent != root or not (root / stored_name).is_file():
                raise PermissionError("附件原件当前不可用。")
        task_invoice_ids = {item[0] for item in database.connection.execute(
            "SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=? AND company_id=?",
            (row["task_id"], company_id),
        )}
        if not set(invoice_ids) <= task_invoice_ids:
            raise PermissionError("只能把附件关联到同一工作项中明确选择的票据。")
        extracted = json.loads(row["extracted_json"])
        pdf_text = str(extracted.get("pdf_text", ""))
        searchable = pdf_text + "\n" + json.dumps(extracted.get("documents", []), ensure_ascii=False)
        quote_start = searchable.find(quote)
        if quote_start < 0:
            raise ValueError("引用内容必须能在附件已保存的提取结果中定位。")
        source_version = row["sha256"]
        page_texts = pdf_text.split("\f")
        pages = [index + 1 for index, page in enumerate(page_texts) if quote in page]
        for invoice_id in invoice_ids:
            locator_by_invoice[invoice_id] = {"attachment_id": source_id, "task_id": row["task_id"],
                "turn_id": row["turn_id"], "pages": pages, "quote_start": quote_start}
    return source_version, quote_start, locator_by_invoice


def available_sources(database, *, user: dict, company_id: str, period: str,
                      attachment_root: str | Path | None = None) -> dict[str, Any]:
    period = _period(period)
    require_role(database, user, company_id, {"accountant"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id)
    sources = []
    replies = database.connection.execute(
        """SELECT r.response_id,r.reply_text,r.created_at,q.request_id,q.target_invoice_ids_json
           FROM enterprise_responses r JOIN enterprise_requests q USING(request_id)
           JOIN enterprise_work_items w USING(task_id)
           WHERE q.company_id=? AND w.period=? ORDER BY r.created_at,r.response_id""", (company_id, period),
    ).fetchall()
    for row in replies:
        targets = json.loads(row["target_invoice_ids_json"])
        if targets:
            sources.append({"source_type": "requester_reply", "source_id": row["response_id"],
                            "source_version": hashlib.sha256(row["reply_text"].encode("utf-8")).hexdigest(),
                            "label": f"经办人回复 · {row['created_at']}", "quote_preview": row["reply_text"][:240],
                            "invoice_ids": targets})
    documents = database.connection.execute(
        """SELECT d.document_id,d.filename,d.file_hash,d.original_available,d.file_content,a.invoice_id,a.locator_json
           FROM enterprise_source_documents d JOIN enterprise_source_aliases a USING(document_id)
           JOIN invoices i ON i.company_id=a.company_id AND i.invoice_id=a.invoice_id
           WHERE d.company_id=? AND substr(i.issue_date,1,7)=? AND a.status='linked' AND a.invoice_id IS NOT NULL
           ORDER BY d.created_at,d.document_id,a.invoice_id,a.row_number""", (company_id, period),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in documents:
        item = grouped.setdefault(row["document_id"], {"source_type": "source_document", "source_id": row["document_id"],
            "source_version": row["file_hash"], "label": row["filename"], "invoice_ids": [], "locators": [],
            "original_available": bool(row["original_available"] and row["file_content"] is not None)})
        item["invoice_ids"].append(row["invoice_id"])
        item["locators"].append({"invoice_id": row["invoice_id"], **json.loads(row["locator_json"])})
    sources.extend({**item, "invoice_ids": sorted(set(item["invoice_ids"]))} for item in grouped.values() if item["original_available"])
    attachments = database.connection.execute(
        """SELECT a.attachment_id,a.filename,a.sha256,a.extracted_json,a.task_id,a.turn_id,a.stored_name
           FROM enterprise_assistant_attachments a JOIN enterprise_work_items w USING(task_id)
           WHERE a.company_id=? AND a.user_id=? AND w.period=? ORDER BY a.created_at,a.attachment_id""",
        (company_id, user["user_id"], period),
    ).fetchall()
    root = Path(attachment_root).resolve() if attachment_root is not None else None
    for row in attachments:
        if root is not None and (Path(row["stored_name"]).name != row["stored_name"] or
                                 (root / row["stored_name"]).resolve().parent != root or not (root / row["stored_name"]).is_file()):
            continue
        extracted = json.loads(row["extracted_json"])
        targets = [item[0] for item in database.connection.execute(
            "SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=? AND company_id=? ORDER BY invoice_id",
            (row["task_id"], company_id),
        )]
        sources.append({"source_type": "assistant_attachment", "source_id": row["attachment_id"],
                        "source_version": row["sha256"], "label": row["filename"],
                        "quote_preview": str(extracted.get("pdf_text", ""))[:240],
                        "invoice_ids": targets, "task_id": row["task_id"], "turn_id": row["turn_id"]})
    originals = database.connection.execute("""SELECT a.attachment_id,a.filename,a.sha256,a.source_revision,a.result_revision,
        a.prepared_json,a.processing_status,a.source_kind,a.task_id,a.user_id,a.request_id,q.target_invoice_ids_json
        FROM enterprise_original_attachments a JOIN enterprise_work_items w ON w.task_id=a.task_id
        LEFT JOIN enterprise_requests q ON q.request_id=a.request_id WHERE a.company_id=? AND w.period=?
        AND (a.source_kind='reply' OR a.user_id=?) ORDER BY a.created_at,a.attachment_id""",
        (company_id, period, user["user_id"])).fetchall()
    for row in originals:
        if row["source_kind"] == "reply":
            targets = json.loads(row["target_invoice_ids_json"] or "[]")
        else:
            targets = [item[0] for item in database.connection.execute("SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=? AND company_id=? ORDER BY invoice_id", (row["task_id"], company_id))]
        prepared = json.loads(row["prepared_json"] or "{}")
        sources.append({"source_type": "assistant_attachment", "source_id": row["attachment_id"],
            "source_version": f"{row['sha256']}:{row['source_revision']}:{row['result_revision']}", "label": row["filename"],
            "quote_preview": str(prepared.get("text") or "")[:240], "invoice_ids": targets,
            "task_id": row["task_id"], "processing_status": row["processing_status"]})
    sources.sort(key=lambda item: (item["source_type"], item["label"], item["source_id"]))
    return {"status": "ok", "company_id": company_id, "period": period, "items": sources}


def list_facts(database, *, user: dict, company_id: str, period: str,
               invoice_id: str | None = None) -> dict[str, Any]:
    period = _period(period)
    require_role(database, user, company_id, {"accountant", "reviewer"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id)
    sql = """SELECT f.fact_id,f.period,f.fact_type,f.source_type,f.source_id,f.source_version,f.source_quote,
                    f.fact_text,f.created_by,f.created_at,t.invoice_id,t.current_revision,t.status,t.text_override,
                    t.updated_at,t.source_locator_json
             FROM enterprise_professional_facts f JOIN enterprise_professional_fact_targets t USING(fact_id,company_id)
             WHERE f.company_id=? AND f.period=?"""
    params: list[Any] = [company_id, period]
    if invoice_id:
        sql += " AND t.invoice_id=?"
        params.append(invoice_id)
    sql += " ORDER BY t.invoice_id,f.created_at,f.fact_id"
    items = []
    for row in database.connection.execute(sql, params):
        item = dict(row)
        item["effective_text"] = item.pop("text_override") or item["fact_text"]
        item["source_locator"] = json.loads(item.pop("source_locator_json"))
        item["history"] = [dict(history) for history in database.connection.execute(
            """SELECT revision,status,text_override,change_reason,changed_by,created_at
               FROM enterprise_professional_fact_target_history WHERE fact_id=? AND invoice_id=? ORDER BY revision""",
            (row["fact_id"], row["invoice_id"]),
        )]
        items.append(item)
    return {"status": "ok", "company_id": company_id, "period": period, "items": items}


def record_fact(database, *, user: dict, company_id: str, period: str, fact_type: str, fact_text: str,
                source_type: str, source_id: str, source_quote: str, invoice_ids: list[str],
                command_id: str, attachment_root: str | Path | None = None) -> dict[str, Any]:
    period = _period(period)
    require_role(database, user, company_id, {"accountant"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=company_id, modify=True)
    if not isinstance(fact_type, str) or fact_type not in {"purpose", "identity", "other"}:
        raise ValueError("认可事实类型必须是用途、身份或其他。")
    if not isinstance(fact_text, str) or not fact_text.strip() or len(fact_text.strip()) > 2000:
        raise ValueError("认可事实内容须为 1 到 2000 个字符。")
    if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
        raise ValueError("command_id 必须为 1 到 200 个字符。")
    if not isinstance(source_quote, str) or not source_quote.strip():
        raise ValueError("请填写来源中的原话或原件内容。")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("请先选择来源记录。")
    invoices = _invoice_rows(database, company_id=company_id, period=period, invoice_ids=invoice_ids)
    selected_ids = [item["invoice_id"] for item in invoices]
    if not selected_ids:
        raise ValueError("至少要明确选择一张票据。")
    request_json = json.dumps({"period": period, "fact_type": fact_type, "fact_text": fact_text.strip(),
                               "source_type": source_type, "source_id": source_id, "source_quote": source_quote.strip(),
                               "invoice_ids": selected_ids}, ensure_ascii=False, sort_keys=True)
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        replay = _command_replay(database, command_id=command_id, company_id=company_id,
                                 operation="record_fact", request_json=request_json)
        if replay:
            connection.commit()
            return replay
        before = review_basis(database, user=user, company_id=company_id, period=period, invoice_ids=selected_ids)
        source_version, source_quote_start, locators = _source_payload(database, user=user, company_id=company_id, period=period,
            source_type=source_type, source_id=source_id, invoice_ids=selected_ids,
            source_quote=source_quote, attachment_root=attachment_root)
        fact_id = "fact-" + uuid4().hex
        quote = source_quote.strip()
        now = utc_now()
        connection.execute(
            """INSERT INTO enterprise_professional_facts
               (fact_id,company_id,period,fact_type,source_type,source_id,source_version,source_quote,source_quote_start,
                fact_text,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fact_id, company_id, period, fact_type, source_type, source_id, source_version, quote,
             source_quote_start, fact_text.strip(), user["user_id"], now),
        )
        for invoice_id in selected_ids:
            locator_json = json.dumps(locators[invoice_id], ensure_ascii=False, sort_keys=True)
            connection.execute(
                """INSERT INTO enterprise_professional_fact_targets
                   (fact_id,company_id,invoice_id,current_revision,status,text_override,source_locator_json,updated_at)
                   VALUES(?,?,?,1,'active',NULL,?,?)""",
                (fact_id, company_id, invoice_id, locator_json, now),
            )
            connection.execute(
                """INSERT INTO enterprise_professional_fact_target_history
                   (fact_id,company_id,invoice_id,revision,status,text_override,change_reason,changed_by,created_at)
                   VALUES(?,?,?,1,'active',NULL,'首次登记',?,?)""",
                (fact_id, company_id, invoice_id, user["user_id"], now),
            )
        after = review_basis(database, user=user, company_id=company_id, period=period, invoice_ids=selected_ids)
        impact = _fact_impact(before, after, database, company_id=company_id, period=period)
        continuation = _apply_review_impact(database, user=user, company_id=company_id, period=period,
                                            event_id=command_id, impact=impact)
        result = {"status": "created", "fact_id": fact_id, "version": 1, "source_version": source_version,
                  "invoice_ids": selected_ids, "impact": impact, "continuation": continuation}
        connection.execute(
            """INSERT INTO enterprise_professional_commands
               (command_id,company_id,operation,actor_user_id,request_json,result_json,created_at) VALUES(?,?,?,?,?,?,?)""",
            (command_id, company_id, "record_fact", user["user_id"], request_json,
             json.dumps(result, ensure_ascii=False, sort_keys=True), now),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def _fact_impact(before: dict[str, Any], after: dict[str, Any], database, *, company_id: str, period: str) -> dict[str, Any]:
    old = {item["invoice_id"]: item for item in before["invoices"]}
    new = {item["invoice_id"]: item for item in after["invoices"]}
    changed, unchanged = [], []
    approved = _approved_invoices(database, company_id=company_id, period=period)
    for invoice_id in sorted(set(old) | set(new)):
        a, b = old.get(invoice_id), new.get(invoice_id)
        if a and b and a["basis_revision"] == b["basis_revision"]:
            unchanged.append(invoice_id)
        else:
            changed.append({"invoice_id": invoice_id, "before_status": a["status"] if a else None,
                            "after_status": b["status"] if b else None,
                            "before_missing": a["missing_requirements"] if a else [],
                            "after_missing": b["missing_requirements"] if b else [],
                            "previously_approved": invoice_id in approved,
                            "requires_re_review": invoice_id in approved})
    return {"affected_invoices": changed, "unchanged_invoice_ids": unchanged,
            "requires_re_review": [item["invoice_id"] for item in changed if item["requires_re_review"]]}


def _fact_change(database, *, user: dict, fact_id: str, invoice_id: str, expected_revision: int,
                 command_id: str, operation: str, text_override: str | None, reason: str) -> dict[str, Any]:
    if not isinstance(fact_id, str) or not fact_id or not isinstance(invoice_id, str) or not invoice_id:
        raise ValueError("fact_id 和 invoice_id 必须是文本编号。")
    if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
        raise ValueError("command_id 必须为 1 到 200 个字符。")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise ValueError("expected_revision 必须是正整数。")
    if operation == "correct" and (not isinstance(text_override, str) or not text_override.strip() or len(text_override.strip()) > 2000):
        raise ValueError("更正后的事实须为 1 到 2000 个字符。")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        raise ValueError("请填写本次更正或撤回的原因。")
    connection = database.connection
    fact = connection.execute(
        """SELECT f.company_id,f.period,f.fact_type,t.current_revision,t.status
           FROM enterprise_professional_facts f JOIN enterprise_professional_fact_targets t USING(fact_id,company_id)
           WHERE f.fact_id=? AND t.invoice_id=?""", (fact_id, invoice_id),
    ).fetchone()
    if fact is None:
        raise ValueError("认可事实不存在或未关联到所选票据。")
    require_role(database, user, fact["company_id"], {"accountant"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=fact["company_id"], modify=True)
    request_json = json.dumps({"fact_id": fact_id, "invoice_id": invoice_id, "expected_revision": expected_revision,
                               "text_override": text_override.strip() if isinstance(text_override, str) else None,
                               "reason": reason.strip()}, ensure_ascii=False, sort_keys=True)
    connection.execute("BEGIN IMMEDIATE")
    try:
        replay = _command_replay(database, command_id=command_id, company_id=fact["company_id"],
                                 operation=operation + "_fact", request_json=request_json)
        if replay:
            connection.commit()
            return replay
        current = connection.execute(
            "SELECT current_revision,status FROM enterprise_professional_fact_targets WHERE fact_id=? AND invoice_id=?",
            (fact_id, invoice_id),
        ).fetchone()
        if current["current_revision"] != expected_revision:
            raise ProfessionalConflict("该票的认可事实已更新，请刷新后重试。")
        if current["status"] != "active":
            raise ProfessionalConflict("该票的事实来源已经撤回，不能直接修改。")
        before = review_basis(database, user=user, company_id=fact["company_id"], period=fact["period"], invoice_ids=[invoice_id])
        revision = expected_revision + 1
        status = "active" if operation == "correct" else "withdrawn"
        value = text_override.strip() if operation == "correct" else None
        now = utc_now()
        connection.execute(
            """UPDATE enterprise_professional_fact_targets SET current_revision=?,status=?,text_override=?,updated_at=?
               WHERE fact_id=? AND invoice_id=? AND current_revision=?""",
            (revision, status, value, now, fact_id, invoice_id, expected_revision),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ProfessionalConflict("该票的认可事实已更新，请刷新后重试。")
        connection.execute(
            """INSERT INTO enterprise_professional_fact_target_history
               (fact_id,company_id,invoice_id,revision,status,text_override,change_reason,changed_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (fact_id, fact["company_id"], invoice_id, revision, status, value, reason.strip(), user["user_id"], now),
        )
        after = review_basis(database, user=user, company_id=fact["company_id"], period=fact["period"], invoice_ids=[invoice_id])
        impact = _fact_impact(before, after, database, company_id=fact["company_id"], period=fact["period"])
        continuation = _apply_review_impact(database, user=user, company_id=fact["company_id"], period=fact["period"],
                                            event_id=command_id, impact=impact)
        result = {"status": "corrected" if operation == "correct" else "withdrawn", "fact_id": fact_id,
                  "invoice_id": invoice_id, "revision": revision, "fact_status": status, "impact": impact,
                  "continuation": continuation}
        connection.execute(
            """INSERT INTO enterprise_professional_commands
               (command_id,company_id,operation,actor_user_id,request_json,result_json,created_at) VALUES(?,?,?,?,?,?,?)""",
            (command_id, fact["company_id"], operation + "_fact", user["user_id"], request_json,
             json.dumps(result, ensure_ascii=False, sort_keys=True), now),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def correct_fact(database, *, user: dict, fact_id: str, invoice_id: str, expected_revision: int,
                 command_id: str, fact_text: str, reason: str) -> dict[str, Any]:
    return _fact_change(database, user=user, fact_id=fact_id, invoice_id=invoice_id,
                        expected_revision=expected_revision, command_id=command_id, operation="correct",
                        text_override=fact_text, reason=reason)


def withdraw_fact(database, *, user: dict, fact_id: str, invoice_id: str, expected_revision: int,
                  command_id: str, reason: str) -> dict[str, Any]:
    return _fact_change(database, user=user, fact_id=fact_id, invoice_id=invoice_id,
                        expected_revision=expected_revision, command_id=command_id, operation="withdraw",
                        text_override=None, reason=reason)

"""Shared, side-effect-free draft field comparison for work views and commits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_FIELDS = ("purpose", "remarks")


def evaluate_draft_fields(draft: Mapping[str, Any], current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare proposed fields with their own bases, falling back to ticket revision for legacy drafts."""

    source_revision = draft.get("source_revision")
    current_revision = current["revision"] if current is not None else None
    if source_revision is None or current_revision is None:
        invoice_revision_state = "not_comparable"
    elif int(source_revision) == int(current_revision):
        invoice_revision_state = "current"
    else:
        invoice_revision_state = "changed"

    contexts = draft.get("field_context") or {}
    proposed_fields = draft.get("proposed_fields")
    states: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, dict[str, Any]] = {}
    proposed_count = 0

    for field in _FIELDS:
        proposed = proposed_fields.get(field) if isinstance(proposed_fields, Mapping) else draft.get(f"proposed_{field}")
        current_value = current[field] if current is not None else None
        basis = contexts.get(field) or {}
        base_known = "base_value" in basis and bool(basis.get("base_known", True))
        before = basis.get("base_value") if base_known else None

        if proposed is None:
            states[field] = {
                "state": "not_proposed",
                "reason_code": "field_not_proposed",
                "explanation": "草稿没有提议修改此字段。",
                "before": before,
                "base_known": base_known,
                "current": current_value,
                "proposed": None,
                "current_revision": current_revision,
            }
            continue

        proposed_count += 1
        if current is None:
            state = {
                "state": "not_comparable",
                "reason_code": "invoice_missing",
                "explanation": "当前票据不可用，暂时无法比较字段。",
                "before": before,
                "base_known": base_known,
                "current": None,
                "proposed": proposed,
                "current_revision": None,
            }
            states[field] = state
            continue

        if base_known:
            conflict = current_value != before
            reason_code = "field_value_changed" if conflict else "field_matches_base"
            explanation = "当前字段已不同于草稿基线，需要重新核对。" if conflict else "当前字段仍与草稿基线一致，可以确认提议。"
        else:
            revision_matches = (
                source_revision is not None
                and current_revision is not None
                and int(source_revision) == int(current_revision)
            )
            conflict = not revision_matches
            reason_code = "source_revision_changed" if conflict else "source_revision_matches"
            explanation = (
                "旧草稿没有已知字段基线，且票据版本已变化，需要重新核对。"
                if conflict
                else "旧草稿没有字段基线；票据版本未变化，可以确认提议。"
            )

        state = {
            "state": "conflict" if conflict else "ready",
            "reason_code": reason_code,
            "explanation": explanation,
            "before": before,
            "base_known": base_known,
            "current": current_value,
            "proposed": proposed,
            "current_revision": current_revision,
        }
        states[field] = state
        if conflict:
            conflicts[field] = {
                "before": before,
                "base_known": base_known,
                "current": current_value,
                "proposed": proposed,
                "current_revision": current_revision,
            }

    if current is None or proposed_count == 0:
        source_version_state = "not_comparable"
    elif conflicts:
        source_version_state = "stale"
    else:
        source_version_state = "current"

    return {
        "field_states": states,
        "field_conflicts": conflicts,
        "source_version_state": source_version_state,
        "invoice_revision_state": invoice_revision_state,
        "source_invoice_revision": source_revision,
        "current_invoice_revision": current_revision,
    }

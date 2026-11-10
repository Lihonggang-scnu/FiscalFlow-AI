"""Rank structured follow-up questions for a batch of task objects.

The planner never interprets question text or user intent. It only compares
the explicit targets and information slots supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


SlotKey = tuple[str, str]


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串。")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} 必须是字符串列表。")
    result = []
    for item in value:
        item = _text(item, field)
        if item not in result:
            result.append(item)
    return result


def _slot_pairs(values: Iterable[SlotKey]) -> set[SlotKey]:
    result = set()
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("known_slots 中的每项必须是 (对象ID, 槽位名)。")
        result.add((_text(pair[0], "对象ID"), _text(pair[1], "槽位名")))
    return result


def _candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("每个候选问题必须是结构化对象。")

    candidate_id = _text(value.get("id"), "id")
    question_text = _text(value.get("text"), "text")
    targets = _string_list(value.get("targets"), "targets")
    raw_required = value.get("required_slots")
    if not isinstance(raw_required, Mapping):
        raise ValueError("required_slots 必须按对象ID映射槽位列表。")
    required_slots: dict[str, list[str]] = {}
    for target, slots in raw_required.items():
        target = _text(target, "required_slots 对象ID")
        required_slots[target] = _string_list(slots, f"{target} 的槽位")
    if set(targets) != set(required_slots):
        raise ValueError("targets 必须与 required_slots 中的对象ID一致。")

    burden = value.get("burden")
    priority = value.get("priority")
    if isinstance(burden, bool) or not isinstance(burden, (int, float)) or not 1 <= burden <= 5:
        raise ValueError("burden 必须是 1 到 5。")
    if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not 1 <= priority <= 5:
        raise ValueError("priority 必须是 1 到 5。")

    return {
        **dict(value),
        "id": candidate_id,
        "text": question_text,
        "targets": targets,
        "required_slots": required_slots,
        "burden": float(burden),
        "priority": float(priority),
    }


def rank_questions(
    candidates: Iterable[Mapping[str, Any]],
    *,
    known_slots: Iterable[SlotKey] = (),
    deferred_targets: Iterable[str] = (),
    recently_asked: Iterable[str] = (),
    lookahead: bool = True,
    simulate_responses: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank generated questions using only their structured slot coverage.

    Candidate fields are ``id``, natural-language ``text``, ``targets``,
    ``required_slots`` (target ID to slot-name lists), ``burden`` (1..5), and
    ``priority`` (1..5). ``known_slots`` is an iterable of explicit
    ``(target_id, slot_name)`` pairs. The returned list retains the original
    question text and includes score components, actionable slots, and rank.
    """

    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit 必须是非负整数或 None。")
    known = _slot_pairs(known_slots)
    deferred = {_text(target, "deferred_targets") for target in deferred_targets}
    asked = {_text(candidate_id, "recently_asked") for candidate_id in recently_asked}

    normalized = [_candidate(value) for value in candidates]
    ids = [item["id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("候选问题 id 必须唯一。")

    prepared = []
    for candidate in normalized:
        all_slots = {
            (target, slot)
            for target in candidate["targets"]
            for slot in candidate["required_slots"][target]
        }
        applicable = {pair for pair in all_slots if pair[0] not in deferred}
        redundant = applicable & known
        actionable = applicable - known
        waiting = all_slots - applicable
        if not actionable:
            continue
        redundancy = len(redundant) / len(applicable) if applicable else 1.0
        waiting_score = len(applicable) / len(all_slots) if all_slots else 1.0
        burden_score = 1.0 - ((candidate["burden"] - 1.0) / 4.0)
        priority_score = (candidate["priority"] - 1.0) / 4.0
        prepared.append({
            **candidate,
            "_all_slots": all_slots,
            "_applicable": applicable,
            "_actionable": actionable,
            "_redundant": redundant,
            "_waiting": waiting,
            "_redundancy_score": 1.0 - redundancy,
            "_waiting_score": waiting_score,
            "_burden_score": burden_score,
            "_priority_score": priority_score,
        })

    if not prepared:
        return []

    max_coverage = max(len(item["_actionable"]) for item in prepared)
    for item in prepared:
        item["_coverage_score"] = len(item["_actionable"]) / max_coverage
        item["_cooldown_score"] = 0.0 if item["id"] in asked else 1.0

    max_future = 0.0
    for item in prepared:
        item["_next_id"] = None
        item["_lookahead_slots"] = 0
        item["_forecast"] = []
        if not lookahead:
            continue
        future = [
            other for other in prepared
            if other["id"] != item["id"] and other["id"] not in asked
        ]
        if future:
            next_item = max(
                future,
                key=lambda other: (
                    len(other["_actionable"] - item["_actionable"]),
                    other["priority"],
                    -other["burden"],
                    other["id"],
                ),
            )
            item["_lookahead_slots"] = len(next_item["_actionable"] - item["_actionable"])
            item["_next_id"] = next_item["id"] if item["_lookahead_slots"] else None
        if simulate_responses:
            slots = sorted(item["_actionable"])
            branches = [("complete", 0.55, [set(slots)]), ("unknown", 0.15, [set()])]
            partial_count = max(1, len(slots) // 2)
            partials = [set((slots[index:] + slots[:index])[:partial_count]) for index in range(min(len(slots), 3))]
            branches.insert(1, ("partial", 0.30, partials))
            expected = 0.0
            for label, probability, possibilities in branches:
                followups = [max((len(other["_actionable"] - answered) for other in future), default=0)
                             for answered in possibilities]
                immediate = sum(len(answered) for answered in possibilities) / len(possibilities)
                subsequent = sum(followups) / len(followups)
                expected += probability * (immediate + 0.6 * subsequent)
                item["_forecast"].append({"answer": label, "probability": probability,
                                          "expected_new_slots": round(immediate, 2),
                                          "expected_followup_slots": round(subsequent, 2)})
            item["_two_step_coverage"] = expected
        else:
            item["_two_step_coverage"] = len(item["_actionable"]) + item["_lookahead_slots"]
        max_future = max(max_future, item["_two_step_coverage"])

    for item in prepared:
        lookahead_score = item.get("_two_step_coverage", 0) / max_future if max_future else 0.0
        scores = {
            "coverage": item["_coverage_score"],
            "redundancy": item["_redundancy_score"],
            "burden": item["_burden_score"],
            "waiting": item["_waiting_score"],
            "cooldown": item["_cooldown_score"],
            "lookahead": lookahead_score,
        }
        item["_scores"] = {key: round(score, 4) for key, score in scores.items()}
        item["_score"] = round(100 * (
            0.30 * scores["coverage"]
            + 0.15 * scores["redundancy"]
            + 0.15 * scores["burden"]
            + 0.10 * scores["waiting"]
            + 0.10 * scores["cooldown"]
            + 0.08 * item["_priority_score"]
            + 0.12 * lookahead_score
        ), 2)

    prepared.sort(key=lambda item: (
        -item["_score"],
        -len(item["_actionable"]),
        -item["priority"],
        item["burden"],
        item["id"],
    ))

    result = []
    for rank, item in enumerate(prepared[:limit], start=1):
        result.append({
            **{key: value for key, value in item.items() if not key.startswith("_")},
            "scores": item["_scores"],
            "score": item["_score"],
            "rank": rank,
            "coverage_slots": [
                {"target_id": target, "slot": slot}
                for target, slot in sorted(item["_actionable"])
            ],
            "redundant_slots": [
                {"target_id": target, "slot": slot}
                for target, slot in sorted(item["_redundant"])
            ],
            "waiting_targets": sorted({target for target, _ in item["_waiting"]}),
            "lookahead_slots": item["_lookahead_slots"],
            "next_question_id": item["_next_id"],
            "forecast": {"hypothetical": True, "branches": item["_forecast"]} if item["_forecast"] else None,
        })
    return result

"""A deterministic simulated user for exercising question plans offline."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串。")
    return value.strip()


def _pairs(values: Any) -> list[tuple[str, str]]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("provide 必须是 (对象ID, 槽位名) 列表。")
    result = []
    for pair in values:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("provide 中的每项必须是 (对象ID, 槽位名)。")
        normalized = (_nonempty(pair[0], "对象ID"), _nonempty(pair[1], "槽位名"))
        if normalized not in result:
            result.append(normalized)
    return result


class QuestionSimulator:
    """Answer selected structured questions from private preset facts.

    ``response_plan`` belongs to the simulator and is never added to planner
    candidates. Each plan is keyed by question ID and may select ``complete``,
    ``partial``, ``defer``, or ``correction`` behavior.
    """

    def __init__(
        self,
        facts: Mapping[str, Mapping[str, str]],
        *,
        labels: Mapping[str, str] | None = None,
        response_plan: Mapping[str, str | Mapping[str, Any]] | None = None,
    ) -> None:
        if not isinstance(facts, Mapping):
            raise ValueError("facts 必须按对象ID映射槽位和值。")
        self._facts: dict[str, dict[str, str]] = {}
        for target, slots in facts.items():
            target = _nonempty(target, "facts 对象ID")
            if not isinstance(slots, Mapping):
                raise ValueError("每个对象的 facts 必须是槽位和值映射。")
            self._facts[target] = {
                _nonempty(slot, "facts 槽位名"): _nonempty(value, "facts 值")
                for slot, value in slots.items()
            }
        self._labels = {
            target: _nonempty(label, "labels")
            for target, label in (labels or {}).items()
        }
        self._response_plan: dict[str, dict[str, Any]] = {}
        for question_id, plan in (response_plan or {}).items():
            question_id = _nonempty(question_id, "response_plan 问题ID")
            if isinstance(plan, str):
                self._response_plan[question_id] = {"mode": plan}
            elif isinstance(plan, Mapping):
                self._response_plan[question_id] = dict(plan)
            else:
                raise ValueError("response_plan 值必须是模式名或配置对象。")
        self._last_values: dict[tuple[str, str], str] = {}

    def answer(self, question: Mapping[str, Any]) -> dict[str, Any]:
        """Respond naturally and return explicit slot updates for the caller."""

        if not isinstance(question, Mapping):
            raise ValueError("question 必须是结构化对象。")
        question_id = _nonempty(question.get("id"), "question.id")
        _nonempty(question.get("text"), "question.text")
        raw_targets = question.get("targets")
        if not isinstance(raw_targets, (list, tuple)):
            raise ValueError("question.targets 必须是列表。")
        targets = list(dict.fromkeys(_nonempty(item, "question.targets") for item in raw_targets))
        raw_required = question.get("required_slots")
        if not isinstance(raw_required, Mapping):
            raise ValueError("question.required_slots 必须按对象ID映射槽位列表。")
        required: dict[str, list[str]] = {}
        for target, slots in raw_required.items():
            target = _nonempty(target, "question.required_slots 对象ID")
            if not isinstance(slots, (list, tuple)):
                raise ValueError("required_slots 的值必须是槽位列表。")
            required[target] = list(dict.fromkeys(_nonempty(slot, "槽位名") for slot in slots))
        if set(targets) != set(required):
            raise ValueError("question.targets 必须与 required_slots 中的对象ID一致。")

        requested = [(target, slot) for target in targets for slot in required[target]]
        coverage = question.get("coverage_slots")
        if coverage is not None:
            actionable = {(item["target_id"], item["slot"]) for item in coverage}
            requested = [pair for pair in requested if pair in actionable]
            targets = [target for target in targets if any(pair[0] == target for pair in requested)]
        plan = self._response_plan.get(question_id, {})
        mode = plan.get("mode", "complete")
        if mode not in {"complete", "partial", "defer", "correction"}:
            raise ValueError(f"不支持的模拟回答模式：{mode}")

        provided_pairs = list(requested)
        if mode == "partial":
            provided_pairs = _pairs(plan["provide"]) if "provide" in plan else requested[: max(1, len(requested) // 2)]
        elif mode == "defer":
            provided_pairs = []
        elif mode == "correction" and "provide" in plan:
            provided_pairs = _pairs(plan["provide"])
        if coverage is not None:
            provided_pairs = [pair for pair in provided_pairs if pair in requested]
        if any(pair not in requested for pair in provided_pairs):
            raise ValueError("response_plan.provide 必须是本问题要求的槽位。")

        deferred_targets = plan.get("defer_targets", [])
        if not isinstance(deferred_targets, (list, tuple)):
            raise ValueError("defer_targets 必须是对象ID列表。")
        deferred_targets = list(dict.fromkeys(_nonempty(item, "defer_targets") for item in deferred_targets))
        if coverage is not None:
            deferred_targets = [target for target in deferred_targets if target in targets]
        if mode == "defer" and not deferred_targets:
            deferred_targets = list(targets)
        if any(target not in targets for target in deferred_targets):
            raise ValueError("defer_targets 必须来自本问题的 targets。")
        provided_pairs = [pair for pair in provided_pairs if pair[0] not in deferred_targets]

        value_overrides = plan.get("values", {})
        if not isinstance(value_overrides, Mapping):
            raise ValueError("response_plan.values 必须按对象ID映射槽位和值。")

        provided_values = []
        corrections = []
        for target, slot in provided_pairs:
            override_slots = value_overrides.get(target, {})
            if not isinstance(override_slots, Mapping):
                raise ValueError("response_plan.values 的对象值必须是槽位和值映射。")
            value = override_slots.get(slot, self._facts.get(target, {}).get(slot))
            if value is None or not str(value).strip():
                continue
            value = str(value).strip()
            previous = self._last_values.get((target, slot))
            provided_values.append({"target_id": target, "slot": slot, "value": value})
            if mode == "correction" and previous != value:
                corrections.append({
                    "target_id": target,
                    "slot": slot,
                    "previous_value": previous,
                    "value": value,
                })
            self._last_values[(target, slot)] = value

        provided_keys = {(item["target_id"], item["slot"]) for item in provided_values}
        unresolved = [pair for pair in requested if pair not in provided_keys]
        if not unresolved:
            status = "answered"
        elif provided_values:
            status = "partial"
        elif deferred_targets:
            status = "deferred"
        else:
            status = "unanswered"

        parts = []
        for item in provided_values:
            target_label = self._labels.get(item["target_id"], item["target_id"])
            slot_label = {"purpose": "用途", "remarks": "备注"}.get(item["slot"], item["slot"])
            if mode == "correction":
                parts.append(f"更正一下，{target_label}的{slot_label}应该是{item['value']}。")
            elif item["slot"] == "purpose":
                parts.append(f"{target_label}的用途是{item['value']}。")
            else:
                parts.append(f"{target_label}的{slot_label}是{item['value']}。")

        if deferred_targets:
            deferred_labels = [self._labels.get(target, target) for target in deferred_targets]
            parts.append(f"{'、'.join(deferred_labels)}的相关信息我暂时还不能确认，之后再补充。")
        unanswered = [pair for pair in unresolved if pair[0] not in deferred_targets]
        if unanswered:
            descriptions = []
            for target, slot in unanswered:
                target_label = self._labels.get(target, target)
                slot_label = {"purpose": "用途", "remarks": "备注"}.get(slot, slot)
                descriptions.append(f"{target_label}的{slot_label}")
            parts.append(f"{'、'.join(descriptions)}我还需要再确认。")
        if not parts:
            parts.append("我现在还不能确认这些信息。")

        return {
            "question_id": question_id,
            "status": status,
            "text": "".join(parts),
            "provided_slots": [
                {"target_id": item["target_id"], "slot": item["slot"]}
                for item in provided_values
            ],
            "provided_values": provided_values,
            "deferred_targets": deferred_targets,
            "unresolved_slots": [
                {"target_id": target, "slot": slot}
                for target, slot in unresolved
            ],
            "corrections": corrections,
        }

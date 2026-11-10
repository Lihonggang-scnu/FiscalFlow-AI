"""Select a task-relevant conversation window without changing saved history."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, messages_to_dict


def estimated_tokens(value: Any) -> int:
    """A local multilingual estimate; provider usage remains the billing source."""

    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, (len(text.encode("utf-8")) + 1) // 2)


def pinned_turn_ids(memory: dict[str, Any]) -> set[str]:
    result = {item.get("source_turn_id", "") for item in memory.get("drafts", {}).values()}
    result.update(item.get("source_turn_id", "") for item in memory.get("notes", {}).values())
    result.update(memory.get("deferred_sources", {}).values())
    result.discard("")
    result.discard(None)
    return result


def compile_context(
    messages: list[Any], *, token_budget: int, reserved_tokens: int = 0,
    recent_turns: int = 8, pinned: set[str] | None = None, fold_every: int = 3,
) -> tuple[list[Any], dict[str, Any]]:
    """Retain complete recent or pinned turns under a soft input budget.

    The most recent turn is always kept, including all of its tool calls and
    results. Older turns remain in the checkpoint and can be recalled by ID.
    """

    pinned = pinned or set()
    positions = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if not positions:
        return list(messages), {"token_budget": token_budget, "estimated_tokens": estimated_tokens(messages_to_dict(messages)),
                                "kept_turns": 0, "folded_turns": 0, "pinned_turn_ids": []}
    prefix = list(messages[:positions[0]])
    groups = [messages[start:(positions[index + 1] if index + 1 < len(positions) else len(messages))]
              for index, start in enumerate(positions)]
    costs = [estimated_tokens(messages_to_dict(group)) for group in groups]
    prefix_cost = estimated_tokens(messages_to_dict(prefix)) if prefix else 0
    available = max(0, token_budget - max(0, reserved_tokens) - prefix_cost)
    latest = len(groups) - 1
    selected = {latest}
    used = costs[latest]
    hot = [index for index, group in enumerate(groups[:-1]) if str(getattr(group[0], "id", "") or "") in pinned]
    fold_every = max(1, fold_every)
    boundary = max(0, len(groups) - max(1, recent_turns)) // fold_every * fold_every
    recent = list(range(boundary, latest))
    for index in list(reversed(hot)) + list(reversed(recent)):
        if index in selected:
            continue
        if used + costs[index] <= available:
            selected.add(index)
            used += costs[index]
    compiled = prefix + [message for index, group in enumerate(groups) if index in selected for message in group]
    kept_ids = [str(getattr(groups[index][0], "id", "") or "") for index in sorted(selected)]
    folded_ids = [str(getattr(group[0], "id", "") or "") for index, group in enumerate(groups) if index not in selected]
    return compiled, {
        "token_budget": token_budget,
        "estimated_tokens": reserved_tokens + prefix_cost + used,
        "kept_turns": len(selected),
        "folded_turns": len(groups) - len(selected),
        "kept_turn_ids": [value for value in kept_ids if value],
        "folded_turn_ids": [value for value in folded_ids if value][-16:],
        "pinned_turn_ids": sorted(pinned),
        "fold_boundary": boundary,
        "fold_every": fold_every,
        "latest_turn_over_budget": costs[latest] > available,
        "estimate": "local_utf8_estimate",
    }

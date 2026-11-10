"""Turn task feedback into reusable, situational handling strategies."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .errors import InvalidInputError
from .task_ledger import TaskLedger


def situation_tags(frontier: dict[str, Any], feedback: dict[str, Any]) -> set[str]:
    tags = set()
    if len(frontier.get("target_invoice_ids", [])) > 1:
        tags.add("multiple_invoices")
    if frontier.get("needs_input"):
        tags.add("needs_input")
    if frontier.get("waiting"):
        tags.add("has_waiting")
    if frontier.get("drafts"):
        tags.add("has_drafts")
    if feedback.get("repeated"):
        tags.add("repeated_attempt")
    if feedback.get("issues"):
        tags.add("tool_issue")
    return tags


def matching_strategies(memory: dict[str, Any], frontier: dict[str, Any], feedback: dict[str, Any]) -> list[dict[str, Any]]:
    tags = situation_tags(frontier, feedback)
    return [strategy for strategy in memory.get("strategies", {}).values()
            if strategy["status"] == "active" and set(strategy.get("tags", [])).issubset(tags)][:8]


class ExperienceBook:
    def __init__(self, ledger: TaskLedger):
        self.ledger = ledger

    def propose(self, *, user_id: str, company_id: str, session_id: str, turn_id: str,
                situation: str, approach: str, tags: list[str], source_turn_id: str = "") -> dict[str, Any]:
        if not situation.strip() or not approach.strip():
            raise InvalidInputError("请提供适用情形和处理方法。")
        strategy_id = "strategy-" + uuid4().hex
        strategy = {"strategy_id": strategy_id, "situation": situation.strip(), "approach": approach.strip(),
                    "tags": list(dict.fromkeys(tags)), "source_turn_id": source_turn_id or turn_id}
        self.ledger.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                           kind="strategy_proposed", payload=strategy)
        return {"status": "proposed", "strategy": {**strategy, "status": "candidate"}}

    def set_active(self, *, user_id: str, company_id: str, session_id: str, turn_id: str,
                   strategy_id: str, active: bool) -> dict[str, Any]:
        memory = self.ledger.memory(user_id=user_id, company_id=company_id)
        strategy = memory["strategies"].get(strategy_id)
        if strategy is None:
            return {"status": "not_found", "summary": "没有找到这条处理策略。"}
        kind = "strategy_activated" if active else "strategy_retired"
        self.ledger.append(user_id=user_id, company_id=company_id, session_id=session_id, turn_id=turn_id,
                           kind=kind, payload={"strategy_id": strategy_id})
        return {"status": "ok", "strategy": {**strategy, "status": "active" if active else "retired"}}

"""Select a task-relevant conversation window without changing saved history."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from .errors import ContextBudgetExceeded


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


def model_context(base: str, snapshot: dict[str, Any], tools: list[dict], messages: list[Any],
                  *, token_budget: int, tag: str, pinned: set[str] | None = None) -> tuple[str, list[Any]]:
    """Keep stored facts intact while limiting the context sent in this model call."""

    state = json.loads(json.dumps(snapshot, ensure_ascii=False))
    shortened = []
    for container_name, notes in (("task_memory", state.get("task_memory", {}).get("notes", {})),
                                  ("task_frontier", state.get("task_frontier", {}).get("frontier", {}).get("notes", {}))):
        for note in notes.values():
            value = note.get("value")
            if isinstance(value, str) and len(value) > 1200:
                note["value"] = value[:1200] + "…"
                note["value_truncated"] = True
                shortened.append(container_name + ".notes")
    for attachment in state.get("attachment_observations", []):
        text = attachment.get("pdf_text")
        if isinstance(text, str) and len(text) > 3000:
            attachment["pdf_text"] = text[:3000] + "…"
            attachment["text_truncated"] = True
            shortened.append("attachment.pdf_text")
    if shortened:
        state["context_shortened"] = sorted(set(shortened))
    reserved = estimated_tokens({"system": base, "state": state, "tools": tools}) + 512
    compiled, manifest = compile_context(messages, token_budget=token_budget, reserved_tokens=reserved, pinned=pinned)
    state["context_manifest"] = manifest
    system = base + f"\n\n<{tag}>" + json.dumps(state, ensure_ascii=False) + f"</{tag}>"
    total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget and tag == "taxharness_program_context":
        condensed = []
        tool_names = {item.name for item in compiled if isinstance(item, ToolMessage)}
        for message in compiled:
            if isinstance(message, ToolMessage) and message.name in {"update_task_memory", "save_invoice", "prepare_invoice_actions", "commit_invoice_actions", "read_invoice", "query_finance", "list_missing_purposes", "read_rule", "plan_followup_questions"} and isinstance(message.content, str):
                try:
                    value = json.loads(message.content)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    if message.name == "update_task_memory":
                        value.pop("memory", None)
                        value.pop("work", None)
                        value["current_state_in_program_context"] = True
                    elif message.name == "plan_followup_questions":
                        value.pop("frontier", None)
                        value["current_state_in_program_context"] = True
                    elif message.name == "prepare_invoice_actions":
                        value = {key: value.get(key) for key in ("status", "plan_id", "invoice_id", "company_id", "summary") if key in value} | {
                            "actions": [{key: action.get(key) for key in ("action_id", "field", "mode", "condition") if key in action}
                                        for action in value.get("actions", [])]}
                    elif message.name == "read_invoice" and isinstance(value.get("data"), dict):
                        value["data"] = {key: value["data"].get(key) for key in ("company_id", "invoice_id", "issue_date", "seller_name", "project_name", "amount", "purpose", "remarks", "revision")}
                    elif message.name in {"save_invoice", "commit_invoice_actions"}:
                        for key in ("before", "after", "current"):
                            if isinstance(value.get(key), dict):
                                value[key] = {field: value[key].get(field) for field in ("company_id", "invoice_id", "purpose", "remarks", "revision")}
                        if isinstance(value.get("task"), dict):
                            value["task"] = {field: value["task"].get(field) for field in ("task_id", "status", "pending_invoice_ids")}
                    elif message.name == "query_finance" and isinstance(value.get("data"), dict):
                        data = value["data"]
                        if "read_rule" in tool_names:
                            data.pop("rules", None)
                        elif isinstance(data.get("rules"), list):
                            data["rules"] = [{key: rule.get(key) for key in ("rule_id", "version", "name", "execution_summary")}
                                             for rule in data["rules"]]
                        invoice = data.get("invoice", {})
                        bank = data.get("bank", {})
                        for section in (invoice, bank):
                            if isinstance(section, dict):
                                section.pop("rules", None)
                        if isinstance(invoice, dict) and isinstance(invoice.get("invoices"), list):
                            invoice["invoices"] = [{key: row.get(key) for key in ("invoice_id", "amount")}
                                                   for row in invoice["invoices"]]
                            invoice["details_shortened"] = True
                    elif message.name == "list_missing_purposes" and isinstance(value.get("data"), dict):
                        data = value["data"]
                        if isinstance(data.get("rule"), dict):
                            data["rule"] = {key: data["rule"].get(key) for key in ("rule_id", "version", "name", "execution_summary")}
                        if isinstance(data.get("missing"), list):
                            data["missing"] = [{key: row.get(key) for key in ("invoice_id", "amount", "seller_name", "project_name")}
                                               for row in data["missing"]]
                    elif message.name == "read_rule" and isinstance(value.get("rule"), dict):
                        value["rule"] = {key: value["rule"].get(key) for key in ("rule_id", "version", "name", "content", "condition", "execution_summary")}
                        if not value["rule"].get("condition"):
                            value["rule"].pop("condition", None)
                        if value["rule"].get("execution_summary") == "该规则类型不执行发票用途条件。":
                            value["rule"].pop("execution_summary", None)
                    message = message.model_copy(update={"content": json.dumps(value, ensure_ascii=False)})
            condensed.append(message)
        compiled = condensed
        total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget and "supported_preferences" in state:
        state.pop("supported_preferences")
        state["preference_catalog_via_tool"] = True
        system = base + f"\n\n<{tag}>" + json.dumps(state, ensure_ascii=False) + f"</{tag}>"
        total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget and tag == "taxharness_program_context":
        frontier = state.get("task_frontier", {}).get("frontier", {})
        for dependency in frontier.get("dependencies", {}).values():
            for key in ("rule", "task_id", "task_revision"):
                dependency.pop(key, None)
        memory = state.get("task_memory", {})
        if frontier.get("drafts") and memory.get("drafts"):
            memory.pop("drafts")
            memory["drafts_in_frontier"] = True
        if frontier.get("notes") and memory.get("notes"):
            memory.pop("notes")
            memory["notes_in_frontier"] = True
        system = base + f"\n\n<{tag}>" + json.dumps(state, ensure_ascii=False) + f"</{tag}>"
        total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget and tag == "taxharness_program_context":
        latest_user = max((index for index, item in enumerate(compiled) if isinstance(item, HumanMessage)), default=-1)
        index = latest_user + 1
        while index < len(compiled) and total > token_budget:
            message = compiled[index]
            if not isinstance(message, AIMessage) or not message.tool_calls:
                index += 1
                continue
            end = index + 1
            while end < len(compiled) and isinstance(compiled[end], ToolMessage):
                end += 1
            results = compiled[index + 1:end]
            foldable = {"read_invoice", "read_rule", "save_invoice", "prepare_invoice_actions", "commit_invoice_actions",
                        "update_task_memory", "task_create", "task_correct", "task_get", "task_resume"}
            if len(results) != len(message.tool_calls) or any(item.name not in foldable for item in results):
                index = end
                continue
            summaries = []
            for item in results:
                try:
                    value = json.loads(item.content)
                except (TypeError, json.JSONDecodeError):
                    break
                if not isinstance(value, dict):
                    break
                summary = {"tool": item.name, "result_ref": item.tool_call_id, "status": value.get("status"), "summary": value.get("summary", "")}
                if item.name == "read_invoice" and isinstance(value.get("data"), dict):
                    summary["data"] = {key: value["data"].get(key) for key in ("company_id", "invoice_id", "issue_date", "seller_name", "project_name", "purpose", "remarks", "revision", "amount")}
                if item.name == "read_rule" and isinstance(value.get("rule"), dict):
                    summary["rule"] = {key: value["rule"].get(key) for key in ("rule_id", "version", "name", "condition", "execution_summary")}
                if item.name in {"task_create", "task_correct", "task_get", "task_resume"} and isinstance(value.get("task"), dict):
                    summary["task"] = {key: value["task"].get(key) for key in
                        ("task_id", "company_id", "month", "scope", "status", "target_invoice_ids", "pending_invoice_ids", "revision")}
                if item.name == "prepare_invoice_actions":
                    summary.update({key: value.get(key) for key in ("plan_id", "invoice_id", "company_id", "actions") if key in value})
                if item.name in {"save_invoice", "commit_invoice_actions"}:
                    for key in ("invoice_id", "plan_id", "action_ids", "written_fields", "preserved_fields", "drafted_fields", "deferred_fields"):
                        if key in value:
                            summary[key] = value[key]
                    for key in ("before", "after", "current"):
                        if key in value:
                            summary[key] = value[key]
                    if isinstance(value.get("result"), dict):
                        summary["historical_result"] = {key: value["result"].get(key) for key in ("status", "before", "after")}
                summaries.append(summary)
            if len(summaries) != len(results):
                index = end
                continue
            later_plan_ids = set()
            completed_plan_actions: dict[str, set[str]] = {}
            for later in compiled[end:]:
                if isinstance(later, ToolMessage) and later.name == "prepare_invoice_actions":
                    try:
                        later_value = json.loads(later.content)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if later_value.get("status") == "prepared" and later_value.get("plan_id"):
                        later_plan_ids.add(later_value["plan_id"])
                elif isinstance(later, ToolMessage) and later.name == "commit_invoice_actions":
                    try:
                        later_value = json.loads(later.content)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    success = later_value.get("status") in {"committed", "no_change"} or (
                        later_value.get("status") == "duplicate" and later_value.get("original_status") in {"committed", "no_change"})
                    receipt = later_value.get("result", later_value)
                    plan_id = later_value.get("plan_id") or receipt.get("plan_id")
                    action_ids = later_value.get("action_ids") or receipt.get("action_ids") or []
                    if success and plan_id:
                        completed_plan_actions.setdefault(plan_id, set()).update(action_ids)
            summaries = [summary for summary in summaries if not
                         (summary["tool"] == "prepare_invoice_actions" and (
                             summary.get("plan_id") in later_plan_ids or
                             bool(summary.get("actions")) and {action.get("action_id") for action in summary["actions"]}
                             <= completed_plan_actions.get(summary.get("plan_id"), set())))]
            state.setdefault("earlier_turn_tool_results", []).extend(summaries)
            compiled = compiled[:index] + compiled[end:]
            system = base + f"\n\n<{tag}>" + json.dumps(state, ensure_ascii=False) + f"</{tag}>"
            total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget and state.get("earlier_turn_tool_results"):
        for summary in state["earlier_turn_tool_results"]:
            if summary.get("tool") == "read_invoice" and isinstance(summary.get("data"), dict):
                data = summary["data"]
                summary["data"] = {key: data.get(key) for key in ("company_id", "invoice_id", "revision") if key in data}
                summary["omitted_fields"] = [key for key in data if key not in summary["data"]]
            elif summary.get("tool") == "read_rule" and isinstance(summary.get("rule"), dict):
                rule = summary["rule"]
                summary["rule"] = {key: rule.get(key) for key in ("rule_id", "version", "name") if key in rule}
                summary["omitted_fields"] = [key for key in rule if key not in summary["rule"]]
            if not summary.get("summary"):
                summary.pop("summary", None)
        state["context_manifest"]["result_retrieval_tool"] = "recall_tool_result"
        system = base + f"\n\n<{tag}>" + json.dumps(state, ensure_ascii=False) + f"</{tag}>"
        total = estimated_tokens({"system": system, "messages": messages_to_dict(compiled), "tools": tools})
    if total > token_budget:
        raise ContextBudgetExceeded("当前对话和业务资料过长，请新建对话接续任务，或缩小本次处理范围。")
    return system, compiled


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

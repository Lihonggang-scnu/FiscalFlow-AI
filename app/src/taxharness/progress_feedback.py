"""Summarize tool feedback so the agent can change course within a turn."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def progress_feedback(messages: list[Any]) -> dict[str, Any]:
    """Find repeated tool outcomes and useful failures in the current user turn."""

    starts = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    current = messages[starts[-1]:] if starts else messages
    calls: dict[str, dict[str, Any]] = {}
    plans: dict[str, dict[str, Any]] = {}
    observed = []
    signatures = Counter()
    for message in current:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call["id"]] = call
        elif isinstance(message, ToolMessage):
            call = calls.get(message.tool_call_id, {})
            name = message.name or call.get("name", "")
            try:
                result = json.loads(message.content) if isinstance(message.content, str) else message.content
            except json.JSONDecodeError:
                result = {"status": "unknown", "summary": str(message.content)}
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "unknown"))
            args = call.get("args", {})
            company = args.get("company_id") or result.get("company_id")
            invoice = args.get("invoice_id") or result.get("invoice_id")
            fields = [item.get("field") for item in args.get("actions", []) if isinstance(item, dict) and item.get("mode") in {"commit", "clear"}]
            if result.get("field"):
                fields = [result["field"]]
            if not fields and isinstance(args.get("changes"), dict):
                fields = list(args["changes"])
            if name == "prepare_invoice_actions" and status == "prepared" and result.get("plan_id"):
                plans[result["plan_id"]] = {"company_id": company, "invoice_id": invoice, "fields": fields}
            if name in {"save_invoice", "commit_invoice_actions"} and status in {"committed", "no_change"}:
                target = plans.get(args.get("plan_id"), {}) if name == "commit_invoice_actions" else {"company_id": company, "invoice_id": invoice, "fields": fields}
                written = set(result.get("written_fields") or target.get("fields") or [])
                if target.get("company_id") and target.get("invoice_id") and written:
                    observed = [issue for issue in observed if not (issue.get("company_id") == target["company_id"] and
                        issue.get("invoice_id") == target["invoice_id"] and issue.get("fields") and set(issue["fields"]).issubset(written))]
            signature = json.dumps({"tool": name, "args": args, "result": result}, ensure_ascii=False, sort_keys=True, default=str)
            signatures[signature] += 1
            if status not in {"ok", "created", "existing", "updated", "resumed", "completed", "committed", "prepared",
                              "duplicate", "no_change", "cancelled", "deleted", "already_undone", "unchanged"}:
                observed.append({"tool": name, "status": status, "summary": str(result.get("summary", ""))[:300],
                                 "company_id": company, "invoice_id": invoice, "fields": fields})
            if name == "enterprise_confirm_drafts":
                for item in result.get("items", []):
                    if isinstance(item, dict) and item.get("status") not in {"committed", "duplicate", "no_change"}:
                        observed.append({"tool": name, "status": str(item.get("status", "unknown")),
                                         "summary": str(item.get("summary", ""))[:300]})
    repeated = []
    for signature, count in signatures.items():
        if count > 1:
            value = json.loads(signature)
            repeated.append({"tool": value["tool"], "count": count, "status": value["result"].get("status", "unknown")})
    return {
        "issues": observed[-8:],
        "repeated": repeated,
        "suggestion": "如果重复操作没有带来新信息，调整工具路径或询问真正缺少的内容。" if repeated else "",
    }

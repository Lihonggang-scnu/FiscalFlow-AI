"""Export a small, redacted set of real turns for the submission package."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


SENSITIVE = ("api_key", "password", "authorization", "secret", "access_token", "refresh_token", "credential")
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"']+")


def redact(value: Any, key: str = "") -> Any:
    if key.lower() == "source_path":
        return "[external-source]"
    if any(token in key.lower() for token in SENSITIVE):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        value = ABSOLUTE_PATH.sub("[path]", value)
        if len(value) > 12000:
            return value[:12000] + "\n[truncated]"
    return value


def parsed(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def full_sanitize(value: Any, key: str = "") -> Any:
    if any(token in key.lower() for token in SENSITIVE):
        return "[redacted]"
    if key.lower() == "source_path":
        return "[external-source]"
    if isinstance(value, dict):
        return {str(k): full_sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [full_sanitize(item, key) for item in value]
    if isinstance(value, str):
        return ABSOLUTE_PATH.sub("[path]", value)
    return value


def compact_model_input(value: Any) -> Any:
    if not isinstance(value, dict):
        return redact(value)
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key == "tools" and isinstance(child, list):
            compact_tools = []
            for tool in child:
                function = tool.get("function", {}) if isinstance(tool, dict) else {}
                compact_tools.append({
                    "name": function.get("name", tool.get("name") if isinstance(tool, dict) else None),
                    "description": str(function.get("description", ""))[:800],
                    "parameter_names": sorted((function.get("parameters", {}).get("properties", {}) or {}).keys()),
                })
            result[key] = compact_tools
        elif key == "messages" and isinstance(child, list):
            result[key] = [redact({
                "role": item.get("data", {}).get("type") if isinstance(item, dict) else None,
                "content": str(item.get("data", {}).get("content", ""))[:3000] if isinstance(item, dict) else str(item)[:3000],
                "tool_calls": [
                    {"name": call.get("name"), "args": redact(call.get("args", {}))}
                    for call in (item.get("data", {}).get("tool_calls", []) if isinstance(item, dict) else [])
                ],
            }) for item in child]
        elif key == "system":
            result[key] = str(child)[:10000]
        else:
            result[key] = redact(child, key)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=Path("artifacts/agent/agent.sqlite3"))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--user-id", default="U001")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    ledger = args.ledger.expanduser().resolve()
    output = args.output.expanduser().resolve()
    connection = sqlite3.connect(ledger)
    connection.row_factory = sqlite3.Row
    try:
        turns = connection.execute(
            "SELECT * FROM agent_turns WHERE session_id=? AND user_id=? ORDER BY created_at",
            (args.session_id, args.user_id),
        ).fetchall()
        records = []
        full_input_done = False
        for index, turn in enumerate(turns, start=1):
            result = parsed(turn["result_json"])
            messages = result.get("messages", []) if isinstance(result, dict) else []
            last_human = max(
                (index for index, message in enumerate(messages)
                 if isinstance(message, dict) and message.get("data", {}).get("type") == "human"),
                default=-1,
            )
            current_messages = messages[last_human:] if last_human >= 0 else messages
            tool_calls = []
            tool_results = []
            for message in current_messages:
                data = message.get("data", {}) if isinstance(message, dict) else {}
                for call in data.get("tool_calls", []) or []:
                    tool_calls.append({"name": call.get("name"), "args": redact(call.get("args", {}))})
                if data.get("type") == "tool":
                    tool_results.append({"name": data.get("name"), "content": redact(parsed(data.get("content", "")))})
            model_calls = connection.execute(
                "SELECT model,status,input_tokens,output_tokens,actual_usd,input_json,output_json FROM agent_model_calls WHERE session_id=? AND user_id=? AND turn_id=? ORDER BY created_at",
                (args.session_id, args.user_id, turn["turn_id"]),
            ).fetchall()
            exported_calls = []
            for call in model_calls:
                input_value = parsed(call["input_json"])
                if not full_input_done:
                    input_value = full_sanitize(input_value)
                    full_input_done = True
                    input_label = "full_selected_model_input"
                else:
                    input_value = compact_model_input(input_value)
                    input_label = "compact_selected_model_input"
                exported_calls.append({
                    "model": call["model"], "status": call["status"],
                    "input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"],
                    "actual_usd": call["actual_usd"], "input_label": input_label,
                    "input": input_value, "output": redact(parsed(call["output_json"])),
                })
            records.append({
                "turn": index,
                "request": turn["request_text"],
                "status": turn["status"],
                "reply": turn["output_text"],
                "context": redact(parsed(turn["input_context_json"])),
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "model_calls": exported_calls,
            })
    finally:
        connection.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "source": "Selected real Chainlit acceptance turns; raw SQLite and full ledger are excluded.",
        "session_turn_count": len(records),
        "turns": records,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "output": str(output), "turns": len(records)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

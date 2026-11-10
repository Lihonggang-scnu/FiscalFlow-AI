"""Single-agent LangGraph runtime over the deterministic tax/task services."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from .agent_store import AgentStore
from .agent_tools import AgentContext, make_agent_tools
from .context_compiler import estimated_tokens, model_context, pinned_turn_ids
from .errors import ContextBudgetExceeded, LiveModelDisabled, SessionOwnershipError, TaxHarnessError, TurnCancelled
from .experience import matching_strategies
from .preferences import PreferenceManager, PreferenceStore, default_preference_path, preference_catalog
from .progress_feedback import progress_feedback
from .provider import _peak_authorized_now, _peak_window, build_live_model, provider_config
from .safe_json import safe_case_json
from .services import TaxService
from .storage import TaxDatabase, database_initialized
from .task_ledger import TaskLedger
from .task_manager import TaskManager


SYSTEM_PROMPT = """你是财税办理助手。
理解用户的自然语言后，使用工具查询当前业务数据、规则和任务；不要自己计算金额、猜用途或编造权限。
每条新用户消息要求查询当前业务时，必须在本轮调用相应工具重新读取，包括与上一轮完全相同的查询。历史金额、用途、备注、缺项和任务进度只供理解上下文，不能直接作为本轮查询结果，也不能未经重查就说“与上次一致”。只有用户明确要求解释上一次结果时，才可引用并注明那是上一次查询结果。
用户问某张发票“该填什么用途”时，销售方和项目只能用于定位。用户尚未提供真实业务目的前，只说明信息不足并询问实际用于什么业务或活动，不给任何拟定用途、候选词句或用途示例；即使用户要求“不用问”也不编造。用户提供实际目的后，可以据此整理表达，并按明确保存要求办理。
用户已经明确提供真实业务目的时，不因用途描述比票面项目更具体、或两者字面不同就提示“不对应”、要求再次确认或暗示风险；只有实际缺少必要信息或适用规则有明确冲突时才说明问题。
程序已经绑定当前用户身份；目标企业可以在工具参数中明确指定。临时查询其他企业不会改变当前办理企业，只有用户明确要求切换时才调用 switch_company。
相对月份按程序上下文提供的北京时间当前日期解释，并在回复中写出绝对年月；不确定时先追问。
缺少月份、企业、发票或修改内容时先追问。用户明确要求清空用途或备注时，使用空字符串表示清空；缺省字段不要修改。
保存前使用 read_invoice 得到当前 revision；保存结果以工具返回为准。不要重复提交同一已完成动作，也不要在用户取消后执行旧的未执行写入。
用户同一条消息要求切换企业并继续办理时，在本轮完成切换及其后续操作，不要求用户重复。临时查询另一企业不切换当前办理企业。
一次工具调用批次同时包含企业切换和其他业务操作时，每个业务工具都要按用户原意明确传入 company_id；切换前后的操作分别使用其对应企业。工具提示补充企业时，直接依据用户原话重试，不要重复询问已经说明的信息。
切换企业与保存、撤销或任务修改有先后关系时，按用户说的顺序分步调用，等待上一步工具结果后再执行下一步；仍在同一轮办理。
用户明确提供待保存草稿、要求某票暂缓或以后继续时，调用 update_task_memory 记录用户的实际决定；暂缓不等于完成。用户明确要求保存且信息充分时，直接调用保存工具，不因记录草稿要求额外确认。
程序上下文的 task_frontier 和 task_memory 给出当前任务、未决事项、用户暂缓决定及草稿。用户更正范围后按新的工作面继续；只询问真正缺少的信息，用户已经表示等待的事项不要反复追问。
多个事项缺信息时，可根据当前任务提出自然中文候选问题并调用 plan_followup_questions，选择能推进工作的问法；只围绕返回的 coverage_slots 提问，不重复问已知或暂缓的事项。难以取舍时可启用假设回答前瞻，它只是问法比较，不能当成用户已经提供的信息。用户已明确提供且要求保存的内容先办理，简单问题不必额外规划。
用户要求预览规则变化时，使用 preview_rule_impact 计算受影响票据和任务，说明这是预览，不要声称规则已经更新。
用户明确要求撤销已保存的用途或备注修改时，可调用 undo_saved_invoice；未保存的草稿用 update_task_memory 删除。撤销结果以实际工具返回为准。
需要核对更早的任务记忆原话时，可以用记忆中的 source_turn_id 调用 recall_task_turn；历史中的业务数值不能代替当前查询。
如果 progress_feedback 显示同一操作在本轮反复得到相同结果，分析缺少的信息并调整方法；用户明确要求再次核对时仍应重新查询。
程序上下文中的 attachment_observations 是上传票据的识别结果，可能有看不清的字段；结合用户文字和当前业务查询处理，不要根据票面推测用途或付款状态。
若程序上下文含 recovered_operations，其中 result 是已查到的历史操作回执，current 是重新读取的当前数据；两者可能不同。回答历史保存时依据回执，回答当前状态时依据 current，不把当前值相同当作历史提交的唯一证明。
程序上下文中的 earlier_turn_tool_results 是本轮较早的工具结果摘要，status 和保存回执仍然有效；完整记录保留在历史中。
工具结果若标记 details_shortened，所需票据详情可以用 read_invoice 重新读取。
从用户反馈或实际办理经历中发现可复用的方法时，可以提出候选策略；用户明确要求采用时可启用。当前上下文的 task_strategies 是适用于本轮的处理建议，不能改变业务规则、权限或用户本轮要求。
回答中说明实际企业、月份、规则依据、已完成和未完成事项；工具失败不能说成成功。
面向普通业务用户时先给本次实际结果、必要依据和剩余事项；不要默认输出 task_id、revision、操作编号、rechecked、文件路径或原始 JSON。只有用户明确询问技术细节时才展开这些内部字段；不要重复解释与本次请求无关的票或规则。
程序上下文中的 available_companies 已给出企业名称与编号；名称能够对应时直接使用对应编号，不要再次向用户索要企业编号。
只回答当前请求，不在查询后自动追加未被询问的完整待办、银行规则说明或“是否继续”等话术。
对于 selected 任务，只报告本任务指定范围的进度和剩余事项；用户未问全企业缺项时，不额外列出任务范围以外的发票。
准确区分操作范围：2026-09限制针对初赛用途检查及用途补全任务，不代表2026-08不能查询金额。金额、开票日期、销售方和银行记录不能修改，应以字段或操作限制解释拒绝；当前办理企业不同本身不是拒绝有权限的临时跨企业操作的理由。
用途缺项必须调用 list_missing_purposes 取得程序结果，不自行从金额和规则正文手算。只回答题内财税能力；超出本题字段和操作范围时说明不能办理并给出题内可做项，不扩展现实业务建议。
如果程序上下文带有 task_refresh_warning，说明当前任务缺少可执行规则依据；保留原任务未完成状态并如实说明，不能把它说成已完成。该 warning 不影响有独立依据的金额查询或用户明确 selected 办理。
只有用户明确要求“记住、以后都这样、长期按此方式”等稳定表达偏好时，才调用 preference_create 或 preference_update；本轮临时要求只服从本轮，不保存。
偏好上下文只包含受控的回答语言、详略、回答顺序、金额排列和回答格式。当前用户本轮明确要求优先；历史消息中的旧偏好不自动恢复，删除或更正后的旧值不再生效。偏好不能改变权限、业务规则、金额计算、发票字段或任务状态。
`preferences` 是当前响应企业的生效值；`global_preferences` 是用户通用回退值，用于解释未单独设置的其他企业，不要把企业覆盖值误报成通用值。
模型必须执行当前 `preferences` 中的每一项有效表达偏好：`response_language` 决定回答语言，`response_detail` 决定详略，`answer_order` 决定结果与依据先后，`response_format=table` 时对本轮已有的可比较结果使用 Markdown 表格，`amount_order` 只重排用户已要求的发票总额、银行收入和银行支出。除非用户本轮明确覆盖，不要退回默认表达方式。
如果用户本轮明确要求纯文字或不要 Markdown，则本轮覆盖表格偏好，不使用 Markdown 表格、粗体标记、反引号或项目符号；不要把这一临时要求保存为长期偏好。
金额排列偏好只能改变用户已经要求返回的金额字段的呈现顺序，不新增用户未询问的金额；发票总额、银行收入和银行支出的实际值仍必须来自 query_finance 工具。
"""


def _compact_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: row.get(key) for key in ("company_id", "month", "scope", "status", "pending_invoice_ids", "next_step")}
        for row in tasks
    ]


def _beijing_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _recent_message_window(messages: list[Any], human_turns: int = 8) -> list[Any]:
    positions = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if len(positions) <= human_turns:
        return list(messages)
    return list(messages[positions[-human_turns]:])


def _sanitize_reply(text: str) -> str:
    """Keep business facts while hiding implementation identifiers by default."""

    text = re.sub(r"`task-[0-9a-f]{32}`", "该任务", text, flags=re.IGNORECASE)
    text = re.sub(r"\btask-[0-9a-f]{32}\b", "该任务", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(?rechecked\s*:\s*(?:true|false)\)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\brevision\s*(\d+)\b", r"保存版本 \1", text, flags=re.IGNORECASE)
    text = re.sub(r"`(?:U\d+):(?:C\d+):invoice:[^`]+`", "本次操作", text)
    text = re.sub(r"`?pref-[0-9a-f]{32}`?", "该偏好", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpreference[_ -]?id\s*[:：]?\s*[^\s，。；;]+", "该偏好", text, flags=re.IGNORECASE)
    text = re.sub(r"[\"`]?preference[_ -]?id[\"`]?\s*[:：=]\s*[\"`]?[^\"`，。；;\s]+[\"`]?", "该偏好", text, flags=re.IGNORECASE)
    text = re.sub(r"[\"`]?revision[\"`]?\s*[:：=]\s*(\d+)", r"保存版本 \1", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _completed_writes(messages: list[Any]) -> list[dict[str, Any]]:
    start = next((index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], HumanMessage)), 0)
    completed = []
    for message in messages[start:]:
        if not isinstance(message, ToolMessage) or message.name not in {"save_invoice", "enterprise_confirm_drafts"}:
            continue
        try:
            receipt = json.loads(message.content)
        except (TypeError, json.JSONDecodeError):
            continue
        items = receipt.get("items", []) if message.name == "enterprise_confirm_drafts" else [receipt]
        for item in items:
            if item.get("status") not in {"committed", "no_change", "duplicate"}:
                continue
            original = item.get("result", item)
            after = original.get("after") or original.get("current") or {}
            before = original.get("before") or {}
            invoice_id = item.get("invoice_id") or after.get("invoice_id") or item.get("draft_id")
            if invoice_id:
                completed.append({"invoice_id": str(invoice_id), "company_id": after.get("company_id"),
                                  "status": item["status"], "changes": {field: after.get(field) for field in ("purpose", "remarks")
                                  if before.get(field) != after.get(field)}})
    return completed


class BatchGuardMiddleware(AgentMiddleware):
    """Ask the model to identify company scope for work batched with a switch."""

    def wrap_tool_call(self, request, handler):
        calls = []
        messages = request.state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage):
            calls = messages[-1].tool_calls
        name = request.tool_call.get("name")
        switch_names = {"switch_company", "enterprise_switch_company"}
        switches = [call for call in calls if call.get("name") in switch_names]
        writes = {"save_invoice", "undo_saved_invoice", "task_create", "task_correct", "task_complete", "task_cancel",
                  "task_delete", "update_task_memory", "propose_task_strategy", "set_task_strategy",
                  "enterprise_create_draft", "enterprise_correct_draft", "enterprise_confirm_drafts"}
        if name == "save_invoice" and any(call.get("name") == "read_invoice" and
                call.get("args", {}).get("invoice_id") == request.tool_call.get("args", {}).get("invoice_id")
                for call in calls):
            return ToolMessage(content=json.dumps({"status": "ordered_actions_needed", "summary": "请先等待发票读取结果，再按该结果决定是否保存。"}, ensure_ascii=False),
                               tool_call_id=request.tool_call["id"], name=name)
        scoped_tools = {"query_finance", "read_invoice", "read_rule", "list_missing_purposes", "task_create",
                        "task_list", "task_get", "task_resume", "task_correct", "task_complete", "task_cancel",
                        "task_delete", "update_task_memory", "inspect_task_frontier", "recall_task_turn", "recall_task_state", "plan_followup_questions", "list_task_experiences", "propose_task_strategy", "set_task_strategy", "preview_rule_impact", "undo_saved_invoice", "save_invoice",
                        "enterprise_list_work_items", "enterprise_inspect_work_item", "enterprise_create_draft", "enterprise_correct_draft", "enterprise_confirm_drafts", "enterprise_finance_summary", "enterprise_read_attachment"}
        if switches and any(call.get("name") in writes for call in calls) and (name in switch_names or name in writes):
            return ToolMessage(
                content=json.dumps({"status": "ordered_actions_needed", "summary": "切换企业和修改操作在同一批次，请根据用户原话按先后顺序分步调用；企业范围已明确时无需向用户重问。"}, ensure_ascii=False),
                tool_call_id=request.tool_call["id"], name=name,
            )
        if switches and name in scoped_tools:
            args = request.tool_call.get("args", {})
            if not args.get("company_id"):
                return ToolMessage(
                    content=json.dumps({"status": "company_scope_needed", "summary": "本批同时有企业切换，请根据用户原意明确此操作的 company_id 并再次调用；已说明的信息无需向用户重问。"}, ensure_ascii=False),
                    tool_call_id=request.tool_call["id"], name=name,
                )
        return handler(request)


class ToolErrorMiddleware(AgentMiddleware):
    """Return structured business errors to the model instead of aborting the turn."""

    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        except TaxHarnessError as error:
            return ToolMessage(
                content=json.dumps({"status": error.code, "summary": str(error)}, ensure_ascii=False),
                tool_call_id=request.tool_call["id"],
                name=request.tool_call.get("name"),
            )


class CancellationMiddleware(AgentMiddleware):
    def before_model(self, state, runtime):
        context = runtime.context
        if context is not None and AgentStore(context.agent_store_path).cancel_requested(context.session_id, context.user_id):
            raise TurnCancelled("本轮已取消。")
        return None


class ProgramContextMiddleware(AgentMiddleware):
    """Rebuild current program state before every model call."""

    def wrap_model_call(self, request, handler):
        context = request.runtime.context
        response_company = context.response_company_id or context.current_company_id
        snapshot = {"user_id": context.user_id, "current_company_id": context.current_company_id, "response_company_id": response_company, "current_date_beijing": _beijing_today(), "recent_objects": list(context.recent_objects), "supported_preferences": preference_catalog()}
        if context.attachment_observations:
            snapshot["attachment_observations"] = context.attachment_observations
        feedback = progress_feedback(request.messages)
        with TaxDatabase(context.database_path) as database:
            if context.recovered_operations:
                recovered = []
                for item in context.recovered_operations:
                    try:
                        current = TaxService(database).read_invoice(user_id=context.user_id, company_id=item["company_id"], invoice_id=item["invoice_id"])
                    except TaxHarnessError as error:
                        recovered.append({"status": error.code, "summary": str(error)})
                        continue
                    if current.get("status") != "ok":
                        recovered.append(current)
                        continue
                    original = item["result"]
                    before, after = original.get("before", {}), original.get("after", {})
                    fields = [field for field in ("purpose", "remarks") if before.get(field) != after.get(field)]
                    recovered.append({**item, "current": current["data"],
                                      "current_matches_receipt": all(current["data"].get(field) == after.get(field) for field in fields)})
                snapshot["recovered_operations"] = recovered
            task_manager = TaskManager(database)
            task_refresh = task_manager.resume_task(task_id=None, user_id=context.user_id, company_id=context.current_company_id)
            if task_refresh.get("status") not in {"resumed", "ok", "no_task", "not_resumable"}:
                snapshot["task_refresh_warning"] = {
                    "status": task_refresh.get("status"),
                    "summary": task_refresh.get("summary", "当前任务规则无法自动刷新，原待办保持不变。"),
                }
            snapshot["tasks"] = _compact_tasks(task_manager.list_tasks(user_id=context.user_id, company_id=context.current_company_id).get("tasks", []))
            ledger = TaskLedger(database)
            snapshot["task_frontier"] = ledger.frontier(user_id=context.user_id, company_id=context.current_company_id,
                session_id=context.session_id, turn_id=context.turn_id)
            frontier = snapshot["task_frontier"]["frontier"]
            scope = (context.user_id, context.current_company_id, frontier.get("task_id"), frontier.get("month"))
            if scope != context.last_frontier_scope:
                context.last_frontier_scope = scope
                context.last_frontier_diff = None
            diff = snapshot["task_frontier"]["diff"]
            if diff.get("added_targets") or diff.get("removed_targets") or diff.get("changes"):
                context.last_frontier_diff = diff
            elif context.last_frontier_diff is not None:
                snapshot["task_frontier"]["diff"] = context.last_frontier_diff
            snapshot["task_memory"] = ledger.memory(user_id=context.user_id, company_id=context.current_company_id)
            snapshot["task_memory"].pop("last_frontier", None)
            snapshot["task_strategies"] = (matching_strategies(snapshot["task_memory"], snapshot["task_frontier"]["frontier"], feedback)
                if response_company == context.current_company_id else [])
            snapshot["task_memory"].pop("strategies", None)
            snapshot["task_memory"].pop("cases", None)
            snapshot["task_memory"].pop("tasks", None)
            snapshot["task_memory"].pop("invoice_events", None)
            snapshot["task_memory"].pop("rule_events", None)
            for draft in snapshot["task_memory"]["drafts"].values():
                draft["save_requested"] = bool(draft.get("save_requested") is True and context.turn_id
                    and draft.get("source_turn_id") == context.turn_id and draft.get("source_session_id") == context.session_id)
            snapshot["available_companies"] = [dict(row) for row in database.connection.execute(
                """SELECT c.company_id,c.name,c.short_name FROM companies c
                   JOIN user_company_permissions p ON p.company_id=c.company_id
                   WHERE p.user_id=? AND p.can_view=1 ORDER BY c.company_id""", (context.user_id,)
            ).fetchall()]
            preference_store = PreferenceStore(context.preference_store_path or default_preference_path(context.database_path))
            preference_view = PreferenceManager(preference_store, database).list(
                user_id=context.user_id, company_id=response_company
            )
            snapshot["preferences"] = preference_view["effective"]
            snapshot["global_preferences"] = [
                row for row in preference_view["preferences"] if row["scope"] == "user"
            ]
        base = request.system_message.content if request.system_message is not None else ""
        snapshot["progress_feedback"] = feedback
        from langchain_core.utils.function_calling import convert_to_openai_tool
        tools = [convert_to_openai_tool(tool) for tool in request.tools]
        budget = int(os.getenv("TAXHARNESS_CONTEXT_BUDGET_TOKENS", str(context.context_budget_tokens)))
        system, compiled = model_context(base, snapshot, tools, request.messages, token_budget=budget,
            tag="taxharness_program_context", pinned=pinned_turn_ids(snapshot["task_memory"]))
        return handler(request.override(system_message=SystemMessage(content=system), messages=compiled))


class BudgetMiddleware(AgentMiddleware):
    """Reserve before the model handler; settle usage or retain an unknown hold."""

    def wrap_model_call(self, request, handler):
        context = request.runtime.context
        if context is None or context.budget_usd is None:
            return handler(request)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if os.getenv("TAXHARNESS_OFFPEAK_ONLY", "false").lower() == "true" and _peak_window(now) and not _peak_authorized_now(now):
            raise LiveModelDisabled("当前处于工作日高峰时段，候选版模型将在空闲时段开放。")
        from langchain_core.utils.function_calling import convert_to_openai_tool

        payload = {
            "system": request.system_message.content if request.system_message is not None else "",
            "messages": messages_to_dict(request.messages),
            "tools": [convert_to_openai_tool(tool) for tool in request.tools],
        }
        encoded = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
        # One token per UTF-8 byte is a conservative upper bound for reservation.
        estimated_input = max(1, len(encoded))
        price_factor = 2 if context.provider_kind == "deepseek" and _peak_window(now) else 1
        input_rate = context.input_price_cny * 0.3 * price_factor
        output_rate = context.output_price_cny * 0.3 * price_factor
        budget_store = AgentStore(context.budget_ledger_path or context.agent_store_path)
        call_id = budget_store.reserve_model_call(
            session_id=context.session_id,
            user_id=context.user_id,
            model=context.model_name,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=context.max_output_tokens,
            budget_usd=context.budget_usd,
            input_payload=payload,
            turn_id=context.turn_id,
            input_rate=input_rate,
            output_rate=output_rate,
        )
        try:
            response = handler(request)
            usage = {}
            if response.result:
                usage = getattr(response.result[0], "usage_metadata", None) or {}
            input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
            output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
            if input_tokens or output_tokens:
                budget_store.settle_model_call(call_id, input_tokens, output_tokens, {"messages": messages_to_dict(response.result), "usage": usage}, input_rate=input_rate, output_rate=output_rate)
            else:
                budget_store.mark_model_unknown(call_id, "provider_usage_missing")
            return response
        except Exception as error:
            budget_store.mark_model_unknown(call_id, type(error).__name__)
            raise


class AgentRuntime:
    def __init__(self, *, database_path: str | Path, agent_store_path: str | Path | None = None, checkpoint_path: str | Path | None = None, preference_store_path: str | Path | None = None, budget_ledger_path: str | Path | None = None):
        self.database_path = str(Path(database_path).expanduser().resolve())
        root = Path(self.database_path).parent
        agent_root = root.parent / "agent" if root.name in {"runtime", "agent"} else root / "agent"
        self.agent_store = AgentStore(agent_store_path or agent_root / "agent.sqlite3")
        self.budget_ledger_path = str(Path(budget_ledger_path or os.getenv("TAXHARNESS_BUDGET_STORE") or self.agent_store.path).expanduser().resolve())
        self.preference_store_path = str(Path(preference_store_path or default_preference_path(self.database_path)).expanduser().resolve())
        self._checkpoint_connection = sqlite3.connect(str(checkpoint_path or agent_root / "agent_checkpoints.sqlite3"), check_same_thread=False, timeout=30)
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self._graph_cache: dict[int, Any] = {}
        self._live_model = None

    def _graph(self, model):
        key = id(model)
        if key not in self._graph_cache:
            self._graph_cache[key] = create_agent(
                model=model,
                tools=make_agent_tools(),
                system_prompt=SYSTEM_PROMPT,
                context_schema=AgentContext,
                checkpointer=self.checkpointer,
                middleware=[CancellationMiddleware(), BatchGuardMiddleware(), ToolErrorMiddleware(), ProgramContextMiddleware(), BudgetMiddleware()],
                name="taxharness-agent",
            )
        return self._graph_cache[key]

    def _database_ready(self) -> bool:
        return database_initialized(self.database_path)

    def _prepare_context(self, context: AgentContext) -> None:
        pass

    def _context_snapshot(self, user_id: str, company_id: str) -> dict[str, Any]:
        with TaxDatabase(self.database_path) as database:
            tasks = TaskManager(database).list_tasks(user_id=user_id, company_id=company_id)
            companies = [dict(row) for row in database.connection.execute(
                """SELECT c.company_id,c.name,c.short_name FROM companies c
                   JOIN user_company_permissions p ON p.company_id=c.company_id
                   WHERE p.user_id=? AND p.can_view=1 ORDER BY c.company_id""", (user_id,)
            ).fetchall()]
            preference_view = PreferenceManager(PreferenceStore(self.preference_store_path), database).list(user_id=user_id, company_id=company_id)
            preferences = preference_view["effective"]
            global_preferences = [row for row in preference_view["preferences"] if row["scope"] == "user"]
        return {"user_id": user_id, "current_company_id": company_id, "current_date_beijing": _beijing_today(), "available_companies": companies, "tasks": _compact_tasks(tasks.get("tasks", [])), "supported_preferences": preference_catalog(), "preferences": preferences, "global_preferences": global_preferences}

    def _recover_unfinished_tools(self, graph, config, context: AgentContext, previous_turn_id: str | None) -> bool:
        """Recover pending tool messages, consulting business operations before saying not-executed."""

        snapshot = graph.get_state(config)
        if not snapshot.next:
            return False
        messages = snapshot.values.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage) or not messages[-1].tool_calls:
            return False
        recovered = []
        store = AgentStore(context.agent_store_path)
        for call in messages[-1].tool_calls:
            status = "interrupted_not_executed"
            payload: dict[str, Any] = {"status": status, "summary": "上一轮工具尚未执行，本轮由新用户消息接管。"}
            if previous_turn_id and call["name"] == "save_invoice":
                args = call.get("args", {})
                invoice_id = args.get("invoice_id")
                company_id = args.get("company_id") or context.current_company_id
                operation = store.operation_for_turn(session_id=context.session_id, turn_id=previous_turn_id, user_id=context.user_id, company_id=company_id, object_type="invoice", object_id=str(invoice_id or ""))
                if operation is not None:
                    with TaxDatabase(context.database_path) as database:
                        business = database.connection.execute("SELECT status,result_json FROM operations WHERE idempotency_key=? AND user_id=? AND company_id=? AND object_type='invoice' AND object_id=?", (operation["operation_id"], context.user_id, company_id, str(invoice_id or ""))).fetchone()
                    if business is not None and business["status"] == "committed":
                        status = "duplicate"
                        original = json.loads(business["result_json"])
                        try:
                            with TaxDatabase(context.database_path) as database:
                                current = TaxService(database).read_invoice(user_id=context.user_id, company_id=company_id, invoice_id=str(invoice_id or ""))
                        except TaxHarnessError as error:
                            current = {"status": error.code, "summary": str(error)}
                        if current.get("status") == "ok":
                            before, after = original.get("before", {}), original.get("after", {})
                            fields = [field for field in ("purpose", "remarks") if before.get(field) != after.get(field)]
                            matches = all(current["data"].get(field) == after.get(field) for field in fields)
                            summary = "上一轮保存曾经提交，已回查当前发票。" if matches else "上一轮保存曾经提交，但相关字段后来已变化；请以当前回查为准。"
                            payload = {"status": status, "recovered": True, "result": original, "current": current["data"],
                                       "current_matches_receipt": matches, "summary": summary}
                        else:
                            payload = {"status": current.get("status", "error"), "summary": current.get("summary", "当前发票无法回查。")}
                        store.finish_operation(operation["operation_id"], status="committed", result=payload)
                    else:
                        store.finish_operation(operation["operation_id"], status="interrupted", result=payload)
            if payload.get("recovered"):
                context.recovered_operations.append({"tool": call["name"], "company_id": company_id,
                                                     "invoice_id": invoice_id, **payload})
            recovered.append(ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=call["id"], name=call["name"]))
        graph.update_state(config, {"messages": recovered}, as_node="tools")
        return True

    def run_turn(self, *, session_id: str, user_id: str, company_id: str | None, message: str, model=None, live_authorized: bool = False, budget_usd: float | None = None,
                 attachment_observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self._database_ready():
            return {"status": "database_not_initialized", "summary": "业务数据库尚未初始化；请先运行 init-db，并使用项目 data/材料创建独立数据库。"}
        stored = self.agent_store.session(session_id, user_id)
        if stored is None:
            if not company_id:
                return {"status": "missing_input", "summary": "新 session 必须先选择企业。"}
            try:
                with TaxDatabase(self.database_path) as database:
                    from .services import TaxService
                    TaxService(database).require_access(user_id=user_id, company_id=company_id)
            except TaxHarnessError as error:
                return {"status": error.code, "summary": str(error)}
            session = self.agent_store.ensure_session(session_id, user_id, company_id)
        else:
            session = stored
            if company_id and session["current_company_id"] != company_id:
                return {"status": "session_company_mismatch", "summary": "已有 session 的当前企业不同，请用 switch_company 明确切换。"}
        current_company = session["current_company_id"]
        self.agent_store.set_cancel(session_id, user_id, False)
        context_snapshot = self._context_snapshot(user_id, current_company)
        if attachment_observations:
            context_snapshot["attachment_observations"] = attachment_observations
        provider = provider_config()
        if live_authorized and budget_usd is None:
            budget_usd = provider.budget_usd
        context = AgentContext(user_id=user_id, session_id=session_id, database_path=self.database_path, agent_store_path=str(self.agent_store.path), current_company_id=current_company, preference_store_path=self.preference_store_path, budget_usd=budget_usd if live_authorized else None, model_name=provider.model, max_output_tokens=provider.max_output_tokens, budget_ledger_path=self.budget_ledger_path, provider_kind=provider.kind, input_price_cny=provider.input_price_cny or 0.0, output_price_cny=provider.output_price_cny or 0.0, context_budget_tokens=provider.context_budget_tokens)
        context.current_user_text = message
        context.attachment_observations = list(attachment_observations or [])
        self._prepare_context(context)
        graph = None
        turn_id = ""
        try:
            if model is None:
                if live_authorized and self.budget_ledger_path == str(self.agent_store.path):
                    raise LiveModelDisabled("候选版尚未连接共享预算账本，模型调用保持关闭。")
                if self._live_model is None:
                    self._live_model = build_live_model(authorized=live_authorized, config=provider)
                model = self._live_model
            graph = self._graph(model)
            graph_config = {"configurable": {"thread_id": session_id}}
            previous = self.agent_store.latest_recoverable_turn(session_id, user_id)
            self._recover_unfinished_tools(graph, graph_config, context, previous["turn_id"] if previous else None)
            turn_id = self.agent_store.start_turn(session_id, user_id, message, context_snapshot)
            context.turn_id = turn_id
            result = graph.invoke(
                {"messages": [HumanMessage(content=message, id=turn_id)]},
                config=graph_config,
                context=context,
            )
            messages = result.get("messages", [])
            feedback = progress_feedback(messages)
            case_warning = ""
            if feedback["issues"] or feedback["repeated"]:
                try:
                    with TaxDatabase(context.database_path) as database:
                        TaskLedger(database).append(user_id=context.user_id, company_id=context.current_company_id,
                            session_id=context.session_id, turn_id=context.turn_id, kind="case_observed",
                            payload={"turn_id": context.turn_id, "feedback": feedback})
                except sqlite3.Error:
                    case_warning = "本轮已执行，但未能记录改进案例。"
            output = ""
            if messages:
                output = messages[-1].content if isinstance(messages[-1], AIMessage) and isinstance(messages[-1].content, str) else ""
            output = _sanitize_reply(output)
            blocked = [issue for issue in feedback["issues"] if issue["status"] in {"write_not_authorized", "precondition_required", "precondition_not_met"}]
            if blocked and re.search(r"(?<!未)(?<!不)(?<!没有)(?:已保存|保存成功|已写入|已提交|已确认)", output):
                completed = [item["invoice_id"] for item in _completed_writes(messages)]
                prefix = "已办理：" + "、".join(dict.fromkeys(completed)) + "。" if completed else ""
                output = prefix + "本轮有保存操作未执行：" + "；".join(dict.fromkeys(issue["summary"] for issue in blocked if issue["summary"]))
            serialized = messages_to_dict(messages)
            payload = {"status": "ok", "session_id": session_id, "turn_id": turn_id, "current_company_id": context.current_company_id, "messages": serialized, "tool_call_count": sum(len(message.tool_calls) for message in messages if isinstance(message, AIMessage))}
            if case_warning:
                payload["case_warning"] = case_warning
            self.agent_store.finish_turn(turn_id, status="ok", output_text=output, result=payload)
            return {**payload, "reply": output}
        except TurnCancelled as error:
            result = {"status": "cancelled", "session_id": session_id, "turn_id": turn_id, "summary": str(error)}
            if turn_id:
                self.agent_store.finish_turn(turn_id, status="cancelled", output_text=str(error), result=result)
            return result
        except ContextBudgetExceeded as error:
            completed = []
            if graph is not None and turn_id:
                try:
                    state = graph.get_state(graph_config)
                    completed = _completed_writes(state.values.get("messages", []))
                except (sqlite3.Error, ValueError, KeyError):
                    completed = []
            summary = str(error)
            if completed:
                details = []
                for item in completed:
                    label = item["invoice_id"]
                    if label.startswith("ei-"):
                        with sqlite3.connect(context.database_path) as database:
                            row = database.execute("SELECT source_invoice_id FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=? LIMIT 1", (item["company_id"], label)).fetchone()
                        if row:
                            label = row[0]
                    fields = "、".join(f"{name}：{item['changes'][field] or '空'}" for field, name in (("purpose", "用途"), ("remarks", "备注")) if field in item["changes"])
                    details.append(label + (f"（{fields}）" if fields else ""))
                summary = "本轮已办理：" + "；".join(details) + "。后续回复未完成。" + summary
            result = {"status": "error", "session_id": session_id, "turn_id": turn_id,
                      "error_type": type(error).__name__, "summary": summary, "completed_writes": completed}
            if turn_id:
                self.agent_store.finish_turn(turn_id, status="error", output_text=summary, result=result)
            return result
        except Exception as error:
            result = {"status": "error", "session_id": session_id, "turn_id": turn_id, "error_type": type(error).__name__, "summary": str(error)[:500]}
            if turn_id:
                self.agent_store.finish_turn(turn_id, status="error", output_text=result["summary"], result=result)
            return result

    def cancel(self, *, session_id: str, user_id: str) -> bool:
        return self.agent_store.set_cancel(session_id, user_id, True)

    def list_turns(self, *, session_id: str, user_id: str, limit: int = 20) -> dict[str, Any]:
        return {"status": "ok", "turns": self.agent_store.list_turns(session_id, user_id, limit)}

    def close(self) -> None:
        if self._live_model is not None:
            client = getattr(self._live_model, "http_client", None)
            if client is not None and hasattr(client, "close"):
                client.close()
            self._live_model = None
        self._checkpoint_connection.close()

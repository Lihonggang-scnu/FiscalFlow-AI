"""Agent-facing tools; natural language stays in the model, business truth stays in services."""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from .agent_store import AgentStore
from .conversation_history import companies_used
from .errors import InvalidInputError, TaxHarnessError, TurnCancelled
from .experience import ExperienceBook
from .invoice_undo import undo_invoice_change
from .invoice_plans import InvoicePlans
from .preferences import PreferenceManager, PreferenceStore, default_preference_path, preference_catalog_text
from .question_planner import rank_questions
from .rule_impact import preview_purpose_rule
from .rules import applicable_rule
from .services import TaxService
from .storage import TaxDatabase
from .task_ledger import TaskLedger
from .task_manager import TaskManager
from .turn_intent import company_aliases, condition_supported_by_quote, draft_write_restricted, parsed_write_condition, write_requested, write_restricted


class MemoryOperation(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: Literal["draft", "remove_draft", "defer", "resume", "note", "remove_note", "question_asked"] = Field(
        description="必填：draft 表示新增或更正草稿；defer 表示暂缓；resume 表示恢复。"
    )
    invoice_id: str | None = None
    field: Literal["purpose", "remarks"] | None = None
    value: str | None = None
    save_requested: bool | None = None
    reason: str | None = None
    key: str | None = None
    id: str | None = None
    targets: list[str] | None = None
    effective_at: str | None = None
    source_quote: str | None = None


class InvoiceFieldAction(BaseModel):
    field: Literal["purpose", "remarks"]
    mode: Literal["preserve", "draft", "commit", "clear", "defer"]
    value: str | None = None
    source_quote: str | None = None
    condition: dict[str, Any] | None = None
    condition_quote: str | None = None
    reason: str | None = None


@dataclass
class AgentContext:
    user_id: str
    session_id: str
    database_path: str
    agent_store_path: str
    current_company_id: str
    preference_store_path: str = ""
    response_company_id: str | None = None
    turn_id: str = ""
    budget_usd: float | None = None
    model_name: str = "deepseek-flash"
    max_output_tokens: int = 4096
    provider_kind: str = "deepseek"
    input_price_cny: float = 1.0
    output_price_cny: float = 4.0
    context_budget_tokens: int = 24000
    budget_ledger_path: str = ""
    recent_objects: list[dict[str, Any]] = field(default_factory=list)
    active_work_item_id: str | None = None
    current_user_text: str = ""
    attachment_observations: list[dict[str, Any]] = field(default_factory=list)
    recovered_operations: list[dict[str, Any]] = field(default_factory=list)
    last_frontier_diff: dict[str, Any] | None = None
    last_frontier_scope: tuple[str, str, str | None, str | None] | None = None
    observed_invoices: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)


def _check_cancel(runtime: ToolRuntime[AgentContext]) -> None:
    context = runtime.context
    if context.cancel_event.is_set() or AgentStore(context.agent_store_path).cancel_requested(context.session_id, context.user_id):
        raise TurnCancelled("本轮已取消，未继续执行新的业务工具。")


def _company(runtime: ToolRuntime[AgentContext], company_id: str | None) -> tuple[AgentContext, str]:
    context = runtime.context
    target = company_id or context.current_company_id
    with TaxDatabase(context.database_path) as database:
        TaxService(database).require_access(user_id=context.user_id, company_id=target)
    if company_id is not None:
        with context.lock:
            context.response_company_id = target
    return context, target


@contextmanager
def _services(runtime: ToolRuntime[AgentContext], company_id: str | None = None):
    _check_cancel(runtime)
    context, target = _company(runtime, company_id)
    with TaxDatabase(context.database_path) as database:
        service = TaxService(database)
        yield context, target, service, TaskManager(database, service)


@contextmanager
def _preferences(runtime: ToolRuntime[AgentContext], company_id: str | None = None):
    _check_cancel(runtime)
    context = runtime.context
    path = context.preference_store_path or str(default_preference_path(context.database_path))
    with TaxDatabase(context.database_path) as database:
        manager = PreferenceManager(PreferenceStore(path), database)
        if company_id is not None:
            manager.list(user_id=context.user_id, company_id=company_id)
            with context.lock:
                context.response_company_id = company_id
        yield context, company_id, manager


def _remember(context: AgentContext, company_id: str, invoice_id: str, month: str) -> None:
    with context.lock:
        context.recent_objects = [row for row in context.recent_objects if not (row.get("company_id") == company_id and row.get("invoice_id") == invoice_id)]
        context.recent_objects.insert(0, {"company_id": company_id, "invoice_id": invoice_id, "month": month})
        del context.recent_objects[10:]


def _compact_for_user(value: Any):
    if isinstance(value, dict):
        # Keep identifiers and revisions available to the model for follow-up actions;
        # final user-facing text is sanitized separately by AgentRuntime.
        return {key: _compact_for_user(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_compact_for_user(child) for child in value]
    return value


def _task_result(value: dict) -> dict:
    return _compact_for_user(value)


def _affirmative(text: str) -> bool:
    text = re.sub(r"[\s。.!！,，]", "", text)
    return bool(re.fullmatch(r"(?:是|是的|对|对的|没错|正确|好|好的|可以|可以的|行|同意|确认|没问题|没有问题|就这样|就这么办|照办|就按这个|按这个来|按你说的|按你的建议|照此办理)(?:请?(?:确认)?(?:保存|写入|确认|记录草稿|这样办|就这样))?", text))


def _confirmed_proposal(context: AgentContext, company_id: str, invoice_id: str, field: str, value: str, mode: str) -> dict | None:
    """Accept a short confirmation only for the one proposal actually shown in the previous turn."""
    if not _affirmative(context.current_user_text):
        return None
    turns = AgentStore(context.agent_store_path).list_turns(context.session_id, context.user_id, limit=10)
    previous = next((turn for turn in turns if turn["turn_id"] != context.turn_id and turn["status"] == "ok"), None)
    if not previous:
        return None
    try:
        messages = json.loads(previous["result_json"] or "{}").get("messages", [])
        proposals = []
        for message in messages:
            if message.get("type") != "tool":
                continue
            result = json.loads(message.get("data", {}).get("content", "{}"))
            if not isinstance(result, dict):
                continue
            items = result.get("items")
            for candidate in [result, *(items if isinstance(items, list) else [])]:
                if isinstance(candidate, dict) and candidate.get("status") == "confirmation_required" and candidate.get("proposal"):
                    proposals.append(candidate["proposal"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    unique = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in proposals
              if item.get("prompt") and item["prompt"] in (previous.get("output_text") or "")}
    if len(unique) != 1:
        return None
    proposal = json.loads(unique.pop())
    expected = {"company_id": company_id, "invoice_id": invoice_id, "field": field, "value": value, "mode": mode}
    if any(proposal.get(key) != wanted for key, wanted in expected.items()):
        return None
    if proposal.get("prompt") not in (previous.get("output_text") or ""):
        return None
    return {**proposal, "displayed_turn_id": previous["turn_id"]}


def _proposal(context: AgentContext, company_id: str, invoice_id: str, field: str, value: str,
              source_quote: str | None, mode: str, revision: int, *, display_invoice: str | None = None) -> dict:
    if not source_quote or source_quote not in context.current_user_text or len(context.current_user_text) > 2000:
        return {"status": "source_required", "field": field, "summary": "请提供这项内容的实际说明；发票尚未修改。"}
    label = "用途" if field == "purpose" else "备注"
    action = "保存" if mode == "commit" else "记录草稿"
    prompt = f"请核对：{company_id} 的发票 {display_invoice or invoice_id}，{label}拟写为“{value}”。你的原要求是“{context.current_user_text}”。确认后我再{action}。"
    return {"status": "confirmation_required", "field": field, "summary": prompt,
            "proposal": {"company_id": company_id, "invoice_id": invoice_id, "field": field, "value": value,
                         "mode": mode, "revision": revision, "original_instruction": context.current_user_text,
                         "source_quote": source_quote, "prompt": prompt}}


def _value_source(context: AgentContext, database: TaxDatabase, company_id: str, invoice_id: str,
                  field: str, value: str, source_quote: str | None) -> dict | None:
    if value.strip() in context.current_user_text:
        return {"kind": "user_message", "turn_id": context.turn_id, "quote": value.strip()}
    memory = TaskLedger(database).memory(user_id=context.user_id, company_id=company_id)
    draft = memory["drafts"].get(f"{invoice_id}:{field}")
    if not draft or str(draft.get("value", "")).strip() != value.strip():
        return None
    previous_source = draft.get("value_source")
    if isinstance(previous_source, dict) and previous_source.get("kind") == "user_confirmed_proposal":
        store = AgentStore(context.agent_store_path)
        confirmed = store.get_turn(previous_source.get("turn_id", ""), context.user_id)
        displayed = store.get_turn(previous_source.get("displayed_turn_id", ""), context.user_id)
        if (confirmed and displayed and confirmed.get("request_text") == previous_source.get("quote") and
                _affirmative(confirmed["request_text"]) and value.strip() in (displayed.get("output_text") or "") and
                invoice_id in (displayed.get("output_text") or "") and company_id in (displayed.get("output_text") or "")):
            return previous_source
    if isinstance(previous_source, dict) and previous_source.get("kind") in {"user_message", "earlier_user_message"}:
        previous_turn = AgentStore(context.agent_store_path).get_turn(previous_source.get("turn_id", ""), context.user_id)
        quote = previous_source.get("quote")
        if previous_turn and quote and value.strip() in quote and quote in (previous_turn.get("request_text") or ""):
            return previous_source
    if isinstance(previous_source, dict) and previous_source.get("kind") == "workspace_draft":
        previous = database.connection.execute("""SELECT payload_json FROM task_ledger_events
            WHERE event_id=? AND user_id=? AND company_id=? AND kind='draft_set'""",
            (previous_source.get("event_id"), context.user_id, company_id)).fetchone()
        if previous:
            payload = json.loads(previous["payload_json"])
            if payload.get("source") == "workspace" and payload.get("invoice_id") == invoice_id and payload.get("field") == field and payload.get("value") == value:
                return previous_source
    if draft.get("source") == "workspace":
        return {"kind": "workspace_draft", "event_id": draft.get("event_id"), "turn_id": draft.get("source_turn_id")}
    turn = AgentStore(context.agent_store_path).get_turn(draft.get("source_turn_id", ""), context.user_id)
    if not turn:
        return None
    source = turn.get("request_text") or ""
    quote = draft.get("source_quote") or value.strip()
    return {"kind": "earlier_user_message", "turn_id": draft.get("source_turn_id"),
            "event_id": draft.get("event_id"), "quote": quote} if quote and value.strip() in quote and quote in source else None


def _finish_invoice_result(context: AgentContext, target: str, service: TaxService, tasks: TaskManager,
                           invoice_id: str, month: str, operation_id: str, observed: dict | None,
                           result: dict) -> dict:
    status = result.get("status")
    if status == "committed":
        active = tasks._active(context.user_id, target)
        if active:
            try:
                refreshed = tasks.refresh_task(task_id=active["task_id"], user_id=context.user_id, company_id=target)
                result.update(task=refreshed.get("task"), task_progress_updated=refreshed.get("status") == "ok", task_refresh_status=refreshed.get("status"))
            except Exception as error:
                result.update(task_progress_updated=False, task_refresh_status="failed", task_refresh_error=type(error).__name__)
        else:
            result.update(task_progress_updated=False, task_refresh_status="no_active_task")
    if status == "duplicate":
        fresh = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
        if fresh.get("status") == "ok":
            current = fresh["data"]
            historic = result.get("result", {}).get("after") or result.get("result", {}).get("current") or {}
            result["current_state"] = current
            result["receipt_matches_current"] = bool(historic and historic.get("revision") == current.get("revision"))
    if status in {"committed", "no_change"}:
        verified = result.get("after") or result.get("current")
        if verified and (status == "committed" or observed is None or verified["revision"] == observed["revision"]):
            with context.lock:
                context.observed_invoices[(target, invoice_id)] = dict(verified)
    final_status = status
    if status == "duplicate":
        original = result.get("original_status")
        final_status = original if original in {"committed", "no_change"} else "failed"
    elif status not in {"committed", "no_change"}:
        final_status = "failed"
    AgentStore(context.agent_store_path).finish_operation(operation_id, status=final_status, result=result)
    _remember(context, target, invoice_id, month)
    return _compact_for_user(result)


def _resolve_task_id(tasks: TaskManager, context: AgentContext, company_id: str, task_id: str | None) -> tuple[str | None, dict | None]:
    if task_id:
        return task_id, None
    current = tasks.resume_task(task_id=None, user_id=context.user_id, company_id=company_id)
    if current.get("status") not in {"resumed", "ok"}:
        return None, _task_result(current)
    return current.get("task", {}).get("task_id"), None


def make_agent_tools() -> list:
    @tool(description="查询指定企业和月份的发票总额、银行收入和支出；银行总笔数以及收入、支出各自的笔数分别返回。company_id 省略时使用当前办理企业；临时指定企业不会改变当前办理企业。")
    def query_finance(month: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (_, target, service, _):
            return _compact_for_user(service.query_all(user_id=runtime.context.user_id, company_id=target, month=month))

    @tool(description="读取一张发票当前字段、用途、备注和可信 revision。月份可以省略，程序按企业和发票编号定位；company_id 省略时使用当前办理企业。")
    def read_invoice(invoice_id: str, month: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, _):
            result = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
            if result.get("status") == "ok":
                with context.lock:
                    context.observed_invoices.setdefault((target, invoice_id), dict(result["data"]))
                _remember(context, target, invoice_id, result["data"]["issue_date"][:7])
            return _compact_for_user(result)

    @tool(description="读取当前企业和月份适用的规则、名称、版本和程序执行条件。kind 可为 amount_statistics、invoice_purpose 或 bank_requirements；条件不支持自动判定时如实返回。")
    def read_rule(kind: str, month: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (_, target, _, _):
            with TaxDatabase(runtime.context.database_path) as database:
                return _compact_for_user({"status": "ok", "rule": applicable_rule(database.connection, kind=kind, company_id=target, month=month)})

    @tool(description="按当前企业、月份和有效规则列出实际必须补用途的发票。只读，不创建或修改任务；不要自行根据金额门槛推算缺项。")
    def list_missing_purposes(month: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, _):
            return _compact_for_user(service.list_missing_purposes(user_id=context.user_id, company_id=target, month=month))

    @tool(description="明确切换当前办理企业。先检查权限；切换后后续默认工具使用新企业。临时跨企业查询不要调用本工具。")
    def switch_company(company_id: str, runtime: ToolRuntime[AgentContext] = None) -> dict:
        _check_cancel(runtime)
        context = runtime.context
        with TaxDatabase(context.database_path) as database:
            TaxService(database).require_access(user_id=context.user_id, company_id=company_id)
        AgentStore(context.agent_store_path).set_company(context.session_id, context.user_id, company_id)
        with context.lock:
            context.current_company_id = company_id
            context.response_company_id = None
            context.recent_objects = []
        return {"status": "ok", "current_company_id": company_id, "summary": "当前办理企业已切换。"}

    @tool(description="创建或返回当前用户和企业的发票用途补全任务。scope=all_missing 或 selected。")
    def task_create(month: str | None = None, scope: str = "all_missing", invoice_ids: list[str] | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            result = tasks.create_task(user_id=context.user_id, company_id=target, month=month, scope=scope, invoice_ids=invoice_ids)
            if result.get("task") and result.get("status") in {"created", "existing"}:
                TaskLedger(tasks.database).bind_task_turn(user_id=context.user_id, company_id=target,
                    session_id=context.session_id, turn_id=context.turn_id, task=result["task"])
            return _task_result(result)

    @tool(description="读取当前用户和企业的任务列表；include_history=true 时包括已完成和已取消历史。")
    def task_list(include_history: bool = False, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            return _task_result(tasks.list_tasks(user_id=context.user_id, company_id=target, include_history=include_history))

    @tool(description="读取任务详情，可选 refresh 重新查询当前发票和规则。")
    def task_get(task_id: str | None = None, refresh: bool = True, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            resolved, error = _resolve_task_id(tasks, context, target, task_id)
            if error is not None:
                return error
            return _task_result(tasks.get_task(task_id=resolved, user_id=context.user_id, company_id=target, refresh=refresh))

    @tool(description="接续当前用户和企业的未完成任务；不需要旧 task_id。已完成/取消任务不会自动恢复。")
    def task_resume(task_id: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            return _task_result(tasks.resume_task(task_id=task_id, user_id=context.user_id, company_id=target))

    @tool(description="更正任务月份、范围或 selected 发票编号。月份不在 2026-09 时返回超范围且不改原任务。")
    def task_correct(task_id: str | None = None, changes: dict | None = None, expected_revision: int | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            resolved, error = _resolve_task_id(tasks, context, target, task_id)
            if error is not None:
                return error
            return _task_result(tasks.correct_task(task_id=resolved, user_id=context.user_id, company_id=target, changes=changes or {}, expected_revision=expected_revision))

    @tool(description="完成任务检查；只有当前目标已满足才会完成，仍有待办时返回 incomplete。")
    def task_complete(task_id: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            resolved, error = _resolve_task_id(tasks, context, target, task_id)
            if error is not None:
                return error
            return _task_result(tasks.complete_task(task_id=resolved, user_id=context.user_id, company_id=target))

    @tool(description="取消任务，不撤销已经写入的发票用途。")
    def task_cancel(task_id: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            resolved, error = _resolve_task_id(tasks, context, target, task_id)
            if error is not None:
                return error
            return _task_result(tasks.cancel_task(task_id=resolved, user_id=context.user_id, company_id=target))

    @tool(description="删除任务记录入口，不删除发票、流水或已经保存的用途。")
    def task_delete(task_id: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            resolved, error = _resolve_task_id(tasks, context, target, task_id)
            if error is not None:
                return error
            return _task_result(tasks.delete_task(task_id=resolved, user_id=context.user_id, company_id=target))

    @tool(description="记录用户明确表达的草稿、暂缓和待办，不写发票。草稿用 action=draft、invoice_id、field、value；转述时用 source_quote 引用用户原话。其他动作有 remove_draft、defer、resume、note、remove_note、question_asked。")
    def update_task_memory(operations: list[MemoryOperation], company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, _):
            with TaxDatabase(context.database_path) as database:
                ledger = TaskLedger(database)
                prepared = []
                rejected = []
                companies = company_aliases(database)
                for item in operations:
                    operation = item.model_dump(exclude_none=True)
                    operation.pop("value_source", None)
                    if item.action in {"draft", "remove_draft", "defer", "resume", "note", "remove_note"} and draft_write_restricted(
                            context.current_user_text, [item.invoice_id] if item.invoice_id else [],
                            [item.field] if item.field else ["purpose", "remarks"], company_id=target, companies=companies):
                        rejected.append({"invoice_id": item.invoice_id, "field": item.field, "status": "write_not_authorized"})
                        continue
                    if item.action == "draft" and item.invoice_id and item.field and isinstance(item.value, str):
                        source = _value_source(context, database, target, item.invoice_id, item.field, item.value, item.source_quote)
                        if source is None:
                            rejected.append({"invoice_id": item.invoice_id, "field": item.field, "status": "source_required"})
                            continue
                        operation["value_source"] = source
                    prepared.append(operation)
                if not prepared:
                    denied = any(item["status"] == "write_not_authorized" for item in rejected)
                    return {"status": "write_not_authorized" if denied else "source_required", "items": rejected,
                            "summary": "本轮要求只查看，任务内容没有修改。" if denied else "请引用草稿内容的实际说明。"}
                result = ledger.apply(user_id=context.user_id, company_id=target, session_id=context.session_id,
                                      turn_id=context.turn_id, operations=prepared)
                result["work"] = ledger.frontier(user_id=context.user_id, company_id=target,
                                                 session_id=context.session_id, turn_id=context.turn_id)
                result["memory"].pop("last_frontier", None)
                result["memory"].pop("cases", None)
                result["memory"].pop("strategies", None)
                if rejected:
                    result.update(status="partial", rejected=rejected, summary="部分事项未满足本轮要求，其余事项已记录。")
                return result

    @tool(description="读取当前任务的可办理事项、待询问信息、暂缓事项、草稿与最近变化。先从当前发票和规则刷新，不执行保存。")
    def inspect_task_frontier(company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, _):
            with TaxDatabase(context.database_path) as database:
                return TaskLedger(database).frontier(user_id=context.user_id, company_id=target,
                                                    session_id=context.session_id, turn_id=context.turn_id)

    @tool(description="按任务记忆中的 source_turn_id 回看用户自己的较早对话原话和回复，跨新会话也可使用。历史金额或保存状态只是当时记录，需要当前值时仍要重新查询。")
    def recall_task_turn(turn_id: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, _):
            turn = AgentStore(context.agent_store_path).get_turn(turn_id, context.user_id)
            if turn is None:
                return {"status": "not_found", "summary": "未找到可查看的对话。"}
            turn_context = json.loads(turn.get("input_context_json") or "{}")
            scope = {"current_company_id": turn_context.get("current_company_id") or target}
            involved = companies_used(scope, [turn])
            if target not in involved:
                return {"status": "not_found", "summary": "该对话不属于当前企业范围。"}
            for company in involved:
                service.require_access(user_id=context.user_id, company_id=company)
            return {"status": "ok", "turn_id": turn_id, "request": turn["request_text"],
                    "reply": turn.get("output_text") or "", "created_at": turn["created_at"],
                    "summary": "这是当时的对话记录；当前业务数据请重新查询。"}

    @tool(description="按上下文中折叠结果的 result_ref 取回本会话的历史工具结果；可分页读取。历史结果不是当前数据，当前状态需重新查询。")
    def recall_tool_result(result_ref: str, offset: int = 0, length: int = 6000,
                           company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, _):
            if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 12000:
                return {"status": "invalid_input", "summary": "分页范围无效。"}
            messages = runtime.state.get("messages", [])
            result = next((item for item in messages if isinstance(item, ToolMessage) and item.tool_call_id == result_ref), None)
            if result is None:
                return {"status": "not_found", "summary": "没有找到这个工具结果。"}
            call = next((call for item in messages if isinstance(item, AIMessage) for call in item.tool_calls if call.get("id") == result_ref), {})
            try:
                payload = json.loads(result.content) if isinstance(result.content, str) else result.content
            except json.JSONDecodeError:
                payload = result.content
            scopes = set()
            def collect(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "company_id" and isinstance(child, str) and child and child != "*":
                            scopes.add(child)
                        else:
                            collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(payload)
            collect(call.get("args", {}))
            if not scopes and result.name != "read_rule":
                return {"status": "scope_unavailable", "summary": "这条历史结果缺少可核对的企业范围，请重新查询。"}
            for company in scopes or {target}:
                service.require_access(user_id=context.user_id, company_id=company)
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            return {"status": "ok", "result_ref": result_ref, "tool_name": result.name, "offset": offset,
                    "total_chars": len(text), "has_more": offset + length < len(text), "content": text[offset:offset + length],
                    "summary": "这是历史工具结果；当前业务状态请重新查询。"}

    @tool(description="查看过去某个记录时间和业务生效时间下的任务记忆。as_of_recorded 是系统获知截止时间，valid_at 是业务生效时间；使用 ISO 日期或日期时间。返回历史状态，不代表当前发票或规则。")
    def recall_task_state(as_of_recorded: str | None = None, valid_at: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            memory = TaskLedger(tasks.database).memory(user_id=context.user_id, company_id=target,
                as_of_recorded=as_of_recorded, valid_at=valid_at)
            memory.pop("last_frontier", None)
            return {"status": "ok", "memory": memory, "as_of_recorded": as_of_recorded,
                    "valid_at": valid_at, "summary": "这是所选时间下的历史任务记忆；当前业务请重新查询。"}

    @tool(description="为多张待办票据比较你提出的自然语言追问，减少重复询问。candidates 每项包含 id、text、targets、required_slots（对象ID到所需槽位列表）、burden 和 priority（1到5）。程序结合已知草稿和暂缓状态排序；只围绕返回的 coverage_slots 提问。复杂问法可设 simulate_responses=true，以全部回答、部分回答和不知道的假设比较下一步；假设不是用户事实。")
    def plan_followup_questions(candidates: list[dict], simulate_responses: bool = False, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            ledger = TaskLedger(tasks.database)
            view = ledger.frontier(user_id=context.user_id, company_id=target,
                                   session_id=context.session_id, turn_id=context.turn_id)
            task_targets = set(view["frontier"]["target_invoice_ids"])
            for candidate in candidates:
                required = candidate.get("required_slots") if isinstance(candidate, dict) else None
                if isinstance(required, dict) and any(invoice_id not in task_targets or
                        not isinstance(slots, list) or any(slot != "purpose" for slot in slots)
                        for invoice_id, slots in required.items()):
                    raise InvalidInputError("候选问题只能涉及当前任务中的票据用途，请按当前待办重新组织。")
            memory = ledger.memory(user_id=context.user_id, company_id=target)
            known = {(item["invoice_id"], item["field"]) for item in memory["drafts"].values()}
            known.update((invoice_id, "purpose") for invoice_id in view["frontier"]["completed"])
            recently_asked = {item["id"] for item in memory["asked"] if item.get("id")}
            try:
                ranked = rank_questions(candidates, known_slots=known,
                    deferred_targets=memory["deferred"], recently_asked=recently_asked,
                    simulate_responses=simulate_responses)
            except ValueError as error:
                raise InvalidInputError(str(error)) from error
            return {"status": "ok", "questions": ranked, "frontier": view["frontier"],
                    "guidance": "只问 coverage_slots 中仍需补充的内容；不要重复问 redundant_slots 或 waiting_targets。可将候选问题自然改写。"}

    @tool(description="查看当前用户和企业的办理反馈案例与处理策略。案例记录实际工具反馈，供分析或改进问询与办理方法。")
    def list_task_experiences(company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            memory = TaskLedger(tasks.database).memory(user_id=context.user_id, company_id=target)
            return {"status": "ok", "cases": memory["cases"][-10:], "strategies": list(memory["strategies"].values())}

    @tool(description="根据实际办理反馈或用户建议，提出可复用的交互/办理策略。tags 可使用 multiple_invoices、needs_input、has_waiting、has_drafts、repeated_attempt、tool_issue；不适用标签可留空。该策略是候选，不改变业务规则、金额或权限。")
    def propose_task_strategy(situation: str, approach: str, tags: list[str], source_turn_id: str = "", company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            return ExperienceBook(TaskLedger(tasks.database)).propose(user_id=context.user_id, company_id=target,
                session_id=context.session_id, turn_id=context.turn_id, situation=situation, approach=approach,
                tags=tags, source_turn_id=source_turn_id)

    @tool(description="用户明确要求采用或停用某条办理策略时，更新这条策略。active=true 采用，false 停用；策略不会改变业务规则和权限。")
    def set_task_strategy(strategy_id: str, active: bool, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            return ExperienceBook(TaskLedger(tasks.database)).set_active(user_id=context.user_id, company_id=target,
                session_id=context.session_id, turn_id=context.turn_id, strategy_id=strategy_id, active=active)

    @tool(description="预览用途规则变化会影响哪些发票和未完成任务，不会修改规则。condition_type 为 all、none 或 amount_gte；金额门槛用 threshold_yuan 表示元。")
    def preview_rule_impact(month: str, condition_type: str, threshold_yuan: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            return preview_purpose_rule(tasks.database, user_id=context.user_id, company_id=target, month=month,
                                        condition_type=condition_type, threshold_yuan=threshold_yuan)

    @tool(description="仅当用户明确要求撤销已保存修改时，恢复当前用户最近一次对指定发票用途或备注的修改。field 可省略或为 purpose/remarks；程序会检查当前字段并刷新任务。未保存草稿用 update_task_memory 的 remove_draft 处理。")
    def undo_saved_invoice(invoice_id: str, field: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, tasks):
            if write_restricted(context.current_user_text, [invoice_id], [field] if field else ["purpose", "remarks"],
                                company_id=target, companies=company_aliases(tasks.database)):
                return {"status": "write_not_authorized", "summary": "你本轮要求查看或暂不撤销，这张发票没有修改。"}
            return undo_invoice_change(tasks.database, user_id=context.user_id, company_id=target,
                invoice_id=invoice_id, field=field, session_id=context.session_id, turn_id=context.turn_id)

    @tool(description="查看已保存的受控长期表达偏好。company_id 省略时只查看用户通用偏好；指定企业时返回用户通用偏好和该企业覆盖值。")
    def preference_list(company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _preferences(runtime, company_id) as (context, _, manager):
            return _compact_for_user(manager.list(user_id=context.user_id, company_id=company_id))

    @tool(description="读取一条已保存的长期偏好；只能读取当前程序用户自己的记录。")
    def preference_get(preference_id: str, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _preferences(runtime) as (context, _, manager):
            return _compact_for_user(manager.get(user_id=context.user_id, preference_id=preference_id))

    @tool(description="保存用户明确要求长期使用的受控表达偏好。只允许以下预设名称和值，不接受任意 prompt：" + preference_catalog_text() + "。scope 默认 user，指定某企业时必须使用 scope=company；临时本轮要求不要调用。")
    def preference_create(key: str, value: str, scope: str = "user", company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _preferences(runtime, company_id) as (context, _, manager):
            return _compact_for_user(manager.create(user_id=context.user_id, key=key, value=value, scope=scope, company_id=company_id))

    @tool(description="更正当前用户自己的长期偏好。用户明确提出新的长期值时直接更新；同值返回 unchanged。")
    def preference_update(preference_id: str, value: str, expected_revision: int | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _preferences(runtime) as (context, _, manager):
            return _compact_for_user(manager.update(user_id=context.user_id, preference_id=preference_id, value=value, expected_revision=expected_revision))

    @tool(description="删除当前用户自己的长期偏好。删除后不再注入新会话或旧会话的动态上下文；不会从旧聊天消息重新恢复。")
    def preference_delete(preference_id: str, expected_revision: int | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _preferences(runtime) as (context, _, manager):
            return _compact_for_user(manager.delete(user_id=context.user_id, preference_id=preference_id, expected_revision=expected_revision))

    @tool(description="把用户本轮明确的逐字段决定整理成计划。每项说明 field、mode（preserve/draft/commit/clear/defer）、实际内容及可选 source_quote；intent_quote 必须是用户本轮原话。支持按字段附加 condition 和 condition_quote。此工具只准备，不修改发票。")
    def prepare_invoice_actions(invoice_id: str, intent_quote: str, actions: list[InvoiceFieldAction],
                                month: str | None = None, company_id: str | None = None,
                                runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, tasks):
            if not intent_quote or intent_quote not in context.current_user_text:
                return {"status": "source_required", "summary": "请引用当前用户实际作出的决定。"}
            current = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
            if current.get("status") != "ok":
                return current
            companies = company_aliases(tasks.database)
            normalized = []
            for item in actions:
                action = item.model_dump(exclude_none=True)
                field, mode = action["field"], action["mode"]
                confirmed = (_confirmed_proposal(context, target, invoice_id, field, action["value"], mode)
                             if mode in {"draft", "commit"} and action.get("value") else None)
                if confirmed and current["data"]["revision"] != confirmed["revision"]:
                    return {"status": "revision_conflict", "field": field, "summary": "票据在确认期间发生变化，请重新查看。"}
                if mode in {"commit", "clear"} and not confirmed and not write_requested(context.current_user_text, [invoice_id], [field], company_id=target, companies=companies):
                    return {"status": "write_not_authorized", "field": field, "summary": "尚未收到对该字段的保存决定。"}
                if mode in {"commit", "clear"} and write_restricted(context.current_user_text, [invoice_id], [field], company_id=target, companies=companies):
                    return {"status": "write_not_authorized", "field": field, "summary": "用户没有允许修改这一字段。"}
                if mode == "clear" and not any(word in intent_quote for word in ("清空", "清除", "置空", "删除", "改为空", "改成空", "去掉")):
                    return {"status": "source_required", "field": field, "summary": "清空字段需要用户明确提出。"}
                if mode in {"draft", "commit"} and action.get("value"):
                    source = ({"kind": "user_confirmed_proposal", "turn_id": context.turn_id, "quote": context.current_user_text,
                               "displayed_turn_id": confirmed["displayed_turn_id"], "source_quote": confirmed["source_quote"]}
                              if confirmed else _value_source(context, tasks.database, target, invoice_id, field, action["value"], action.get("source_quote")))
                    if not source:
                        return _proposal(context, target, invoice_id, field, action["value"], action.get("source_quote"), mode, current["data"]["revision"])
                    action["value_source"] = source
                condition_text = confirmed["original_instruction"] if confirmed else context.current_user_text
                conditional, original_condition = parsed_write_condition(condition_text, [invoice_id], company_id=target, companies=companies)
                if mode in {"commit", "clear"} and conditional:
                    proposed = action.get("condition")
                    if original_condition and proposed and proposed != original_condition:
                        return {"status": "precondition_required", "summary": "计划中的条件与用户原话不一致。"}
                    if original_condition:
                        action["condition"] = original_condition
                        action["condition_quote"] = condition_text
                    elif (not proposed or not action.get("condition_quote") or action["condition_quote"] not in context.current_user_text
                          or not condition_supported_by_quote(proposed, action["condition_quote"])):
                        return {"status": "precondition_required", "summary": "请引用并准确表达用户提出的保存条件。"}
                elif mode in {"commit", "clear"} and action.get("condition"):
                    if (not action.get("condition_quote") or action["condition_quote"] not in context.current_user_text
                            or not condition_supported_by_quote(action["condition"], action["condition_quote"])):
                        return {"status": "precondition_required", "summary": "请引用并准确表达保存条件。"}
                action.pop("source_quote", None)
                normalized.append(action)
            observed = context.observed_invoices.get((target, invoice_id))
            revision = observed["revision"] if observed else current["data"]["revision"]
            plan = InvoicePlans(tasks.database).prepare(user_id=context.user_id, company_id=target, invoice_id=invoice_id,
                session_id=context.session_id, turn_id=context.turn_id, user_message=context.current_user_text,
                intent_quote=intent_quote, expected_revision=revision, actions=normalized)
            return {**plan, "status": "prepared", "invoice_id": invoice_id, "company_id": target,
                    "summary": "已整理逐字段决定；尚未修改发票。"}

    @tool(description="执行刚准备并接受的计划动作，只传 plan_id 和 action_ids；按原计划的字段、值和条件办理。多个有先后关系的动作应分步执行。")
    def commit_invoice_actions(plan_id: str, action_ids: list[str], company_id: str | None = None,
                               runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, service, tasks):
            row = tasks.database.connection.execute("""SELECT * FROM invoice_action_plans
                WHERE plan_id=? AND user_id=? AND company_id=?""", (plan_id, context.user_id, target)).fetchone()
            if row is None:
                return {"status": "not_found", "summary": "没有找到当前用户的办理计划。"}
            invoice_id = row["invoice_id"]
            current = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id)
            if current.get("status") != "ok":
                return current
            month = current["data"]["issue_date"][:7]
            observed = context.observed_invoices.get((target, invoice_id))
            store = AgentStore(context.agent_store_path)
            operation = store.get_or_create_operation(session_id=context.session_id, turn_id=context.turn_id,
                user_id=context.user_id, company_id=target, object_type="invoice", object_id=invoice_id,
                payload={"plan_id": plan_id, "action_ids": action_ids}, expected_revision=row["expected_revision"])
            try:
                result = InvoicePlans(tasks.database).commit(user_id=context.user_id, company_id=target,
                    session_id=context.session_id, turn_id=context.turn_id, plan_id=plan_id,
                    action_ids=action_ids, request_key=operation["operation_id"])
            except Exception as error:
                store.finish_operation(operation["operation_id"], status="unknown", result={"error": type(error).__name__})
                raise
            return _finish_invoice_result(context, target, service, tasks, invoice_id, month, operation["operation_id"], observed, result)

    @tool(description="按用户本轮明确决定保存发票用途或备注；未提及的字段不传。空字符串表示明确清空。优先保留用户实际给出的文字；整理后的措辞可引用 source_quotes，并会在必要时由用户确认。用户条件由程序绑定并在提交时核对。")
    def save_invoice(invoice_id: str, changes: dict, month: str | None = None, company_id: str | None = None,
                     expected_fields: dict[str, str | None] | None = None, source_quotes: dict[str, str] | None = None,
                     runtime: ToolRuntime[AgentContext] = None) -> dict:
        _check_cancel(runtime)
        with _services(runtime, company_id) as (context, target, service, tasks):
            companies = company_aliases(tasks.database)
            if not isinstance(changes, dict) or not changes or set(changes) - {"purpose", "remarks"}:
                return {"status": "invalid_input", "summary": "只能修改用途或备注。"}
            if any(value is not None and not isinstance(value, str) for value in changes.values()):
                return {"status": "invalid_input", "summary": "用途或备注必须是文字。"}
            confirmed = {field: _confirmed_proposal(context, target, invoice_id, field, value, "commit")
                         for field, value in changes.items() if isinstance(value, str) and value.strip()}
            preserved = [field for field in changes if not confirmed.get(field) and not write_requested(context.current_user_text, [invoice_id], [field], company_id=target, companies=companies)]
            changes = {field: value for field, value in changes.items() if field not in preserved}
            if not changes:
                return {"status": "write_not_authorized", "summary": "本轮没有对这些字段作出保存决定，发票没有修改。", "preserved_fields": preserved}
            current = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
            if current.get("status") != "ok":
                return current
            quotes = source_quotes or {}
            sources = {}
            for field, value in changes.items():
                if value is not None and value.strip():
                    accepted = confirmed.get(field)
                    if accepted and current["data"]["revision"] != accepted["revision"]:
                        return {"status": "revision_conflict", "field": field, "summary": "票据在确认期间发生变化，请重新查看。"}
                    sources[field] = ({"kind": "user_confirmed_proposal", "turn_id": context.turn_id, "quote": context.current_user_text,
                                       "displayed_turn_id": accepted["displayed_turn_id"], "source_quote": accepted["source_quote"]}
                                      if accepted else _value_source(context, tasks.database, target, invoice_id, field, value, quotes.get(field)))
                    if not sources[field]:
                        return _proposal(context, target, invoice_id, field, value, quotes.get(field), "commit", current["data"]["revision"])
            accepted = next((item for item in confirmed.values() if item), None)
            condition_text = accepted["original_instruction"] if accepted else context.current_user_text
            conditional, condition = parsed_write_condition(condition_text, [invoice_id], company_id=target, companies=companies)
            if expected_fields:
                if set(expected_fields) - {"purpose", "remarks"} or any(value is not None and not isinstance(value, str) for value in expected_fields.values()):
                    return {"status": "unsupported_precondition", "summary": "当前只能按用途或备注的明确原值判断；发票尚未修改。"}
            if conditional and condition is None:
                return {"status": "precondition_required", "summary": "请准确确认用户提出的保存条件，发票尚未修改。"}
            if expected_fields:
                extra = [{"field": field, "operator": "equals", "value": value} for field, value in expected_fields.items()]
                if conditional:
                    leaves = condition.get("conditions", [condition])
                    if any(not any(leaf["field"] == field and leaf["operator"] == "empty" and not (value or "").strip()
                                   for leaf in leaves) for field, value in expected_fields.items()):
                        return {"status": "precondition_required", "summary": "提供的条件与用户原话不一致；发票尚未修改。"}
                else:
                    condition = extra[0] if len(extra) == 1 else {"operator": "all", "conditions": extra}
            month = month or current["data"]["issue_date"][:7]
            observed = context.observed_invoices.get((target, invoice_id))
            revision = observed["revision"] if observed is not None else current["data"]["revision"]
            payload = {"changes": changes}
            store = AgentStore(context.agent_store_path)
            prior = store.find_operation(session_id=context.session_id, turn_id=context.turn_id, user_id=context.user_id, company_id=target, object_type="invoice", object_id=invoice_id)
            if prior is not None and json.loads(prior["payload_json"]).get("changes") == changes:
                operation_id = prior["operation_id"]
                revision = int(prior["expected_revision"])
            else:
                operation = store.get_or_create_operation(session_id=context.session_id, turn_id=context.turn_id, user_id=context.user_id, company_id=target, object_type="invoice", object_id=invoice_id, payload=payload, expected_revision=revision)
                operation_id = operation["operation_id"]
                revision = int(operation["expected_revision"])
            actions = [{"field": field, "mode": "clear" if value is None or not value.strip() else "commit",
                        "value": value, "value_source": sources.get(field), "condition": condition,
                        "condition_quote": condition_text if conditional else None} for field, value in changes.items()]
            try:
                plans = InvoicePlans(tasks.database)
                plan = plans.prepare(user_id=context.user_id, company_id=target, invoice_id=invoice_id,
                    session_id=context.session_id, turn_id=context.turn_id, user_message=context.current_user_text,
                    intent_quote=context.current_user_text, expected_revision=revision, actions=actions)
                result = plans.commit(user_id=context.user_id, company_id=target, session_id=context.session_id,
                    turn_id=context.turn_id, plan_id=plan["plan_id"], action_ids=[item["action_id"] for item in plan["actions"]],
                    request_key=operation_id)
            except Exception as error:
                store.finish_operation(operation_id, status="unknown", result={"error": type(error).__name__})
                raise
            if preserved:
                result["preserved_fields"] = preserved
            return _finish_invoice_result(context, target, service, tasks, invoice_id, month, operation_id, observed, result)

    return [query_finance, read_invoice, read_rule, list_missing_purposes, switch_company, task_create, task_list, task_get, task_resume, task_correct, task_complete, task_cancel, task_delete, update_task_memory, inspect_task_frontier, recall_task_turn, recall_tool_result, recall_task_state, plan_followup_questions, list_task_experiences, propose_task_strategy, set_task_strategy, preview_rule_impact, undo_saved_invoice, preference_list, preference_get, preference_create, preference_update, preference_delete, prepare_invoice_actions, commit_invoice_actions, save_invoice]

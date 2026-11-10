"""Agent-facing tools; natural language stays in the model, business truth stays in services."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field

from .agent_store import AgentStore
from .conversation_history import companies_used
from .errors import InvalidInputError, TaxHarnessError, TurnCancelled
from .experience import ExperienceBook
from .invoice_undo import undo_invoice_change
from .preferences import PreferenceManager, PreferenceStore, default_preference_path, preference_catalog_text
from .question_planner import rank_questions
from .rule_impact import preview_purpose_rule
from .rules import applicable_rule
from .services import TaxService
from .storage import TaxDatabase
from .task_ledger import TaskLedger
from .task_manager import TaskManager
from .turn_intent import company_aliases, write_conditions, write_restricted


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
    context_budget_tokens: int = 16000
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


def _resolve_task_id(tasks: TaskManager, context: AgentContext, company_id: str, task_id: str | None) -> tuple[str | None, dict | None]:
    if task_id:
        return task_id, None
    current = tasks.resume_task(task_id=None, user_id=context.user_id, company_id=company_id)
    if current.get("status") not in {"resumed", "ok"}:
        return None, _task_result(current)
    return current.get("task", {}).get("task_id"), None


def make_agent_tools() -> list:
    @tool(description="查询指定企业和月份的发票总额、银行收入和支出。company_id 省略时使用当前办理企业；临时指定企业不会改变当前办理企业。")
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

    @tool(description="记录用户明确表达的任务草稿、暂缓、继续或待办事项，使新对话也能接续。每项必须有 action，例如 {\"action\":\"draft\",\"invoice_id\":\"F003\",\"field\":\"purpose\",\"value\":\"用户提供的用途\",\"save_requested\":false}。更正草稿也使用 action=draft 并提供新值；remove_draft 需 invoice_id、field；defer 或 resume 需 invoice_id；note 或 remove_note 需 key，note 另需 value；question_asked 可记录实际要问的 id 和 targets。用户明确指定事项生效时间时可提供 ISO 格式 effective_at。只记录用户实际提供的信息，不代替业务保存。")
    def update_task_memory(operations: list[MemoryOperation], company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _services(runtime, company_id) as (context, target, _, _):
            with TaxDatabase(context.database_path) as database:
                ledger = TaskLedger(database)
                result = ledger.apply(user_id=context.user_id, company_id=target, session_id=context.session_id,
                                      turn_id=context.turn_id, operations=[item.model_dump(exclude_none=True) for item in operations])
                result["work"] = ledger.frontier(user_id=context.user_id, company_id=target,
                                                 session_id=context.session_id, turn_id=context.turn_id)
                result["memory"].pop("last_frontier", None)
                result["memory"].pop("cases", None)
                result["memory"].pop("strategies", None)
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

    @tool(description="统一保存发票用途或备注。changes 只包含需要改变的字段；空字符串表示明确清空。用户提出条件时，必须在 expected_fields 中填写用途或备注的期望原值，空值用空字符串；条件不满足不会保存。本轮已读取过的发票使用首次读取的版本。")
    def save_invoice(invoice_id: str, changes: dict, month: str | None = None, company_id: str | None = None,
                     expected_fields: dict[str, str | None] | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        _check_cancel(runtime)
        with _services(runtime, company_id) as (context, target, service, tasks):
            companies = company_aliases(tasks.database)
            if write_restricted(context.current_user_text, [invoice_id], list(changes), company_id=target, companies=companies):
                return {"status": "write_not_authorized", "summary": "你本轮要求查看或暂不保存，这张发票没有修改。"}
            current = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
            if current.get("status") != "ok":
                return current
            conditional, empty_fields = write_conditions(context.current_user_text, [invoice_id], company_id=target, companies=companies)
            if expected_fields:
                if set(expected_fields) - {"purpose", "remarks"} or any(value is not None and not isinstance(value, str) for value in expected_fields.values()):
                    return {"status": "unsupported_precondition", "summary": "当前只能按用途或备注的明确原值判断；发票尚未修改。"}
            if conditional and (not expected_fields or any(field not in expected_fields or (expected_fields[field] or "").strip() for field in empty_fields)):
                return {"status": "precondition_required", "summary": "用户对保存提出了条件；请明确期望字段值并核对，发票尚未修改。"}
            if expected_fields:
                differences = {field: {"expected": value, "current": current["data"].get(field)}
                    for field, value in expected_fields.items()
                    if (current["data"].get(field) or "").strip() != (value or "").strip()}
                if differences:
                    return {"status": "precondition_not_met", "summary": "当前内容不满足你提出的保存条件，发票没有修改。", "fields": differences}
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
            try:
                result = tasks.save_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month, changes=changes, expected_revision=revision, idempotency_key=operation_id)
            except Exception as error:
                store.finish_operation(operation_id, status="unknown", result={"error": type(error).__name__})
                raise
            if result.get("status") == "committed":
                store.finish_operation(operation_id, status="committed", result=result)
            elif result.get("status") == "duplicate":
                original_status = result.get("original_status")
                replay_status = "committed" if original_status == "committed" else ("no_change" if original_status == "no_change" else "failed")
                store.finish_operation(operation_id, status=replay_status, result=result)
            elif result.get("status") == "no_change":
                store.finish_operation(operation_id, status="no_change", result=result)
            else:
                store.finish_operation(operation_id, status="failed", result=result)
            if result.get("status") in {"committed", "no_change"} or (result.get("status") == "duplicate" and result.get("original_status") == "committed"):
                import sqlite3
                try:
                    business_operation = tasks.database.connection.execute(
                        "SELECT operation_id FROM operations WHERE user_id=? AND company_id=? AND object_type='invoice' AND object_id=? AND idempotency_key=?",
                        (context.user_id, target, invoice_id, operation_id),
                    ).fetchone()
                    source = result.get("result", {}) if result.get("status") == "duplicate" else result
                    saved = source.get("after") or source.get("current") or {}
                    TaskLedger(tasks.database).invoice_saved(user_id=context.user_id, company_id=target,
                        session_id=context.session_id, turn_id=context.turn_id, invoice_id=invoice_id, fields=list(changes),
                        task_id=(result.get("task") or {}).get("task_id"),
                        source_operation_id=business_operation["operation_id"] if business_operation else None,
                        saved_values={field: saved[field] for field in changes if field in saved})
                except sqlite3.Error:
                    result["task_memory_warning"] = "发票结果已保存或已核对，但任务记忆未刷新。"
            _remember(context, target, invoice_id, month)
            return _compact_for_user(result)

    return [query_finance, read_invoice, read_rule, list_missing_purposes, switch_company, task_create, task_list, task_get, task_resume, task_correct, task_complete, task_cancel, task_delete, update_task_memory, inspect_task_frontier, recall_task_turn, recall_task_state, plan_followup_questions, list_task_experiences, propose_task_strategy, set_task_strategy, preview_rule_impact, undo_saved_invoice, preference_list, preference_get, preference_create, preference_update, preference_delete, save_invoice]

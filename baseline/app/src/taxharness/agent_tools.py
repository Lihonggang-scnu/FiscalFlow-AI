"""Agent-facing tools; natural language stays in the model, business truth stays in services."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain.tools import ToolRuntime, tool

from .agent_store import AgentStore
from .errors import TaxHarnessError, TurnCancelled
from .preferences import PreferenceManager, PreferenceStore, default_preference_path, preference_catalog_text
from .rules import applicable_rule
from .services import TaxService
from .storage import TaxDatabase
from .task_manager import TaskManager


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
    budget_ledger_path: str = ""
    recent_objects: list[dict[str, Any]] = field(default_factory=list)
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
            return _task_result(tasks.create_task(user_id=context.user_id, company_id=target, month=month, scope=scope, invoice_ids=invoice_ids))

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

    @tool(description="统一保存发票用途或备注。changes 只包含需要改变的字段；缺省字段保持不变，空字符串表示明确清空。程序读取当前 revision 并绑定稳定操作记录，成功后刷新相关任务。")
    def save_invoice(invoice_id: str, changes: dict, month: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        _check_cancel(runtime)
        with _services(runtime, company_id) as (context, target, service, tasks):
            current = service.read_invoice(user_id=context.user_id, company_id=target, invoice_id=invoice_id, month=month)
            if current.get("status") != "ok":
                return current
            month = month or current["data"]["issue_date"][:7]
            revision = current["data"]["revision"]
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
            _remember(context, target, invoice_id, month)
            return _compact_for_user(result)

    return [query_finance, read_invoice, read_rule, list_missing_purposes, switch_company, task_create, task_list, task_get, task_resume, task_correct, task_complete, task_cancel, task_delete, preference_list, preference_get, preference_create, preference_update, preference_delete, save_invoice]

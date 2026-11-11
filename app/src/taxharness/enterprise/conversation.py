"""Conversational access to the enterprise workbench's existing business records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, messages_to_dict
from pydantic import BaseModel, Field

from ..agent import AgentRuntime, BatchGuardMiddleware, BudgetMiddleware, CancellationMiddleware
from ..agent_store import AgentStore
from ..agent_tools import AgentContext, _check_cancel, _confirmed_proposal, _proposal
from ..context_compiler import model_context
from ..errors import SessionOwnershipError, TaxHarnessError
from ..invoice_plans import _condition_met
from ..services import TaxService
from ..turn_intent import company_aliases, draft_write_restricted, parsed_write_condition, write_requested
from .service import EnterpriseService
from .storage import EnterpriseDatabase


SYSTEM = """你是代账机构的财税办理助手，帮助当前登录用户处理企业工作台中的票据补充和复核。
用户身份由系统提供，不能切换成其他人。先按当前企业和期间查工作项，再读取真实票据、草稿及状态；多个企业或票据不明确时先澄清。
经办人回复生成的是待会计确认的草稿，不是已经保存的发票。用户要求更正草稿时，读取最新 draft_revision，再调用更正工具；明确清空时用 clear_fields 指定用途或备注。用户明确要求保存/确认时才调用确认工具，结果以工具回执为准。
用户明确提供了某张工作项发票的用途或备注而尚无草稿时，用 enterprise_create_draft 记录；如同一轮明确要求保存，创建草稿后直接确认，无需重复询问。
确认保存后仍需按企业流程复核；不能把待复核说成工作项已完成。只读或复核角色不能借用会计权限。不可根据票面、销售方或金额编造用途。
用户明确给出同一句话里的多个动作时按业务先后顺序执行；涉及切换企业时为各动作指定 company_id。回复简洁说明已完成、未完成和下一步，不默认输出内部编号或版本。
程序上下文中的 recovered_operations 是之前已确认的操作回执及重新读取的当前值；两者不同时分别说明，不重复提交。
attachment_observations 是上传票据的识别结果，可能有不确定字段；uncertain_candidates 只是待核对候选，不当成确定事实。结合用户文字及当前工作项核对，不能从票面猜用途或付款状态。
"""


class DraftReference(BaseModel):
    draft_id: str = Field(description="工作项中读取到的草稿编号")
    draft_revision: int = Field(description="刚读取到的草稿版本")


def _actor(database: EnterpriseDatabase, user_id: str) -> dict[str, Any]:
    row = database.connection.execute(
        "SELECT user_id,username,display_name,is_admin,active FROM enterprise_users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None or not row["active"]:
        raise PermissionError("当前账号不存在或已停用。")
    return dict(row)


@contextmanager
def _service(runtime: ToolRuntime[AgentContext], company_id: str | None):
    _check_cancel(runtime)
    context = runtime.context
    target = company_id or context.current_company_id
    with EnterpriseDatabase(context.database_path) as database:
        actor = _actor(database, context.user_id)
        TaxService(database).require_access(user_id=context.user_id, company_id=target)
        service = EnterpriseService(database)
        service._role(actor, target, {"accountant", "reviewer"})
        yield context, target, database, actor, service


def make_enterprise_tools() -> list:
    @tool(description="列出当前企业或明确指定企业的工作项，可按 YYYY-MM 期间筛选。")
    def enterprise_list_work_items(period: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (_, target, _, actor, service):
            return service.list_work_items(user=actor, company_id=target, period=period, page_size=100)

    @tool(description="读取企业工作项的实时票据、用途草稿、经办人原话、待确认和待复核状态。")
    def enterprise_inspect_work_item(task_id: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        from .work_projection import project_enterprise_work

        with _service(runtime, company_id) as (_, target, database, actor, _):
            task = database.connection.execute("SELECT company_id FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["company_id"] != target:
                raise ValueError("当前企业没有该工作项。")
            return project_enterprise_work(database, user=actor, task_id=task_id)

    @tool(description="查看工作项交接摘要：已完成、等待的人员和资料、最近变化、交付版本及下一步。")
    def enterprise_handover(task_id: str | None = None, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        from .handover import build_handover

        with _service(runtime, company_id) as (context, target, database, actor, _):
            selected = task_id or context.active_work_item_id
            task = database.connection.execute("SELECT company_id FROM enterprise_work_items WHERE task_id=?", (selected,)).fetchone()
            if task is None or task["company_id"] != target:
                raise ValueError("当前企业没有该工作项。")
            return build_handover(database, user=actor, task_id=selected)

    @tool(description="为当前工作项中的一张发票记录用户明确提供的用途或备注草稿；不会修改发票。先检查工作项取得内部 invoice_id。")
    def enterprise_create_draft(invoice_id: str, purpose: str | None = None, remarks: str | None = None,
                                task_id: str | None = None, company_id: str | None = None,
                                clear_fields: list[str] | None = None,
                                runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (context, target, database, actor, service):
            selected = task_id or context.active_work_item_id
            task = database.connection.execute("SELECT company_id FROM enterprise_work_items WHERE task_id=?", (selected,)).fetchone()
            if task is None or task["company_id"] != target:
                raise ValueError("当前企业没有该工作项。")
            link = database.connection.execute("SELECT source_invoice_id FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (target, invoice_id)).fetchone()
            fields = [field for field, value in (("purpose", purpose), ("remarks", remarks)) if value is not None or field in (clear_fields or [])]
            if draft_write_restricted(context.current_user_text, [invoice_id, link["source_invoice_id"] if link else None], fields,
                                      company_id=target, companies=company_aliases(database)):
                return {"status": "write_not_authorized", "summary": "本轮要求只查看，草稿没有修改。"}
            if clear_fields and not any(word in context.current_user_text for word in ("清空", "清除", "置空", "删除", "删掉", "改为空", "改成空", "去掉")):
                return {"status": "write_not_authorized", "summary": "请先明确需要清空的字段。"}
            return service.create_conversation_draft(user=actor, task_id=selected, invoice_id=invoice_id,
                purpose=purpose, remarks=remarks, source_turn_id=context.turn_id, user_text=context.current_user_text, clear_fields=clear_fields)

    @tool(description="更正已读取的企业草稿，不写发票。必须提供草稿最新 draft_revision；未提及的用途或备注保持原样，明确清空时使用空字符串。")
    def enterprise_correct_draft(draft_id: str, expected_draft_revision: int, purpose: str | None = None,
                                 remarks: str | None = None, invoice_id: str | None = None,
                                 company_id: str | None = None, clear_fields: list[str] | None = None,
                                 runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (context, target, database, actor, service):
            draft = database.connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=? AND company_id=?", (draft_id, target)).fetchone()
            if draft is None:
                raise ValueError("当前企业没有该草稿。")
            if purpose is None and remarks is None and invoice_id is None and not clear_fields:
                raise ValueError("请提供需要更正的字段或票据。")
            link = database.connection.execute("SELECT source_invoice_id FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (target, draft["invoice_id"])).fetchone()
            fields = [field for field, value in (("purpose", purpose), ("remarks", remarks)) if value is not None or field in (clear_fields or [])] or ["purpose", "remarks"]
            if draft_write_restricted(context.current_user_text, [draft["invoice_id"], link["source_invoice_id"] if link else None], fields,
                                      company_id=target, companies=company_aliases(database)):
                return {"status": "write_not_authorized", "summary": "本轮要求只查看，草稿没有修改。"}
            if clear_fields and not any(word in context.current_user_text for word in ("清空", "清除", "置空", "删除", "删掉", "改为空", "改成空", "去掉")):
                return {"status": "write_not_authorized", "summary": "请先明确需要清空的字段。"}
            updated = service.update_draft(user=actor, draft_id=draft_id, invoice_id=invoice_id or draft["invoice_id"],
                purpose=draft["proposed_purpose"] if purpose is None else purpose,
                remarks=draft["proposed_remarks"] if remarks is None else remarks,
                expected_draft_revision=expected_draft_revision, source_text=context.current_user_text, source_turn_id=context.turn_id,
                clear_fields=clear_fields)
            return {"status": "ok", "draft": service._decode_draft(updated), "invoice_written": False,
                    "summary": "草稿已更正，发票尚未修改。"}

    @tool(description="会计明确要求保存或确认时，按草稿编号和最新 draft_revision 逐项确认；写入后仍按企业流程等待复核。")
    def enterprise_confirm_drafts(drafts: list[DraftReference], company_id: str | None = None,
                                  runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (context, target, database, actor, service):
            ids = [draft.draft_id for draft in drafts]
            allowed = []
            blocked = []
            conditions = {}
            selected_fields = {}
            preserved_fields = {}
            confirmation_records = {}
            companies = company_aliases(database)
            for draft_id in ids:
                row = database.connection.execute("""SELECT d.invoice_id,d.proposed_purpose,d.proposed_remarks,d.field_context_json,
                    d.source_turn_id,d.draft_revision,l.source_invoice_id,i.revision AS invoice_revision,
                    i.purpose AS current_purpose,i.remarks AS current_remarks
                    FROM enterprise_drafts d LEFT JOIN enterprise_invoice_links l USING(company_id,invoice_id)
                    LEFT JOIN invoices i USING(company_id,invoice_id)
                    WHERE d.draft_id=? AND d.company_id=? LIMIT 1""", (draft_id, target)).fetchone()
                if row is None:
                    raise ValueError("所选草稿不属于当前企业。")
                fields = [field for field in ("purpose", "remarks") if row["proposed_" + field] is not None]
                identifiers = [row["invoice_id"], row["source_invoice_id"]]
                conditional_now, condition_now = parsed_write_condition(context.current_user_text, identifiers, company_id=target, companies=companies)
                requested_now = [field for field in fields if write_requested(context.current_user_text, identifiers, [field], company_id=target, companies=companies)]
                current_fields = {"purpose": row["current_purpose"], "remarks": row["current_remarks"], "revision": row["invoice_revision"]}
                if conditional_now and condition_now and requested_now and not _condition_met(condition_now, current_fields):
                    allowed.append(draft_id)
                    selected_fields[draft_id] = requested_now
                    preserved_fields[draft_id] = [field for field in fields if field not in requested_now]
                    conditions[draft_id] = condition_now
                    continue
                contexts = json.loads(row["field_context_json"] or "{}")
                permitted, accepted = [], None
                blocked_before = len(blocked)
                for field in fields:
                    value = row["proposed_" + field]
                    confirmation = (_confirmed_proposal(context, target, row["invoice_id"], field, value, "commit") if value else None)
                    if confirmation and (confirmation.get("draft_id") != draft_id or confirmation.get("draft_revision") != row["draft_revision"]):
                        confirmation = None
                    requested = confirmation or write_requested(context.current_user_text, identifiers, [field], company_id=target, companies=companies)
                    if not requested:
                        continue
                    if confirmation and row["invoice_revision"] != confirmation["revision"]:
                        blocked.append({"draft_id": draft_id, "status": "revision_conflict", "summary": "票据在确认期间发生变化，请重新查看。"})
                        continue
                    turn_id = contexts.get(field, {}).get("source_turn_id") or row["source_turn_id"]
                    original = database.connection.execute("""SELECT content FROM enterprise_actor_messages
                        WHERE turn_id=? AND user_id=? AND company_id=? LIMIT 1""", (turn_id, context.user_id, target)).fetchone() if turn_id else None
                    field_context = contexts.get(field, {})
                    grounded = (not value or value in context.current_user_text or (original and value in original["content"]) or
                                (field_context.get("source_recorded") and value in field_context.get("evidence_quote", "")))
                    if not grounded and not confirmation:
                        pending = _proposal(context, target, row["invoice_id"], field, value, context.current_user_text,
                                            "commit", row["invoice_revision"], display_invoice=row["source_invoice_id"])
                        if pending.get("proposal"):
                            pending["proposal"].update(draft_id=draft_id, draft_revision=row["draft_revision"])
                        blocked.append({"draft_id": draft_id, **pending})
                        continue
                    permitted.append(field)
                    accepted = confirmation or accepted
                    if confirmation:
                        confirmation_records.setdefault(draft_id, {})[field] = {
                            "displayed_turn_id": confirmation["displayed_turn_id"], "prompt": confirmation["prompt"],
                            "confirmed_turn_id": context.turn_id, "confirmation": context.current_user_text}
                condition_text = accepted["original_instruction"] if accepted else context.current_user_text
                conditional, condition = parsed_write_condition(condition_text, identifiers, company_id=target, companies=companies)
                if not permitted and len(blocked) == blocked_before:
                    blocked.append({"draft_id": draft_id, "status": "write_not_authorized", "summary": "你本轮要求查看或暂不保存，草稿尚未写入。"})
                elif permitted and conditional and condition is None:
                    blocked.append({"draft_id": draft_id, "status": "precondition_required", "summary": "请明确需要核对的用途或备注条件，草稿尚未写入。"})
                elif permitted:
                    allowed.append(draft_id)
                    selected_fields[draft_id] = permitted
                    preserved_fields[draft_id] = [field for field in fields if field not in permitted]
                    if conditional:
                        conditions[draft_id] = condition
            result = service.confirm_drafts(user=actor, draft_ids=allowed,
                expected_draft_revisions={draft.draft_id: draft.draft_revision for draft in drafts},
                fields_by_draft=selected_fields, conditions_by_draft=conditions,
                source_confirmations_by_draft=confirmation_records)
            for item in result["items"]:
                item["preserved_fields"] = preserved_fields.get(item["draft_id"], [])
            if result["items"] and not result["confirmed"] and all(item["status"] == "precondition_not_met" for item in result["items"]):
                result["status"] = "precondition_not_met"
                result["summary"] = "当前内容不满足你提出的保存条件，发票没有修改。"
            result["items"].extend(blocked)
            result["failed"] += len(blocked)
            if blocked:
                result["status"] = "partial" if result["confirmed"] else blocked[0]["status"]
                result["summary"] = blocked[0]["summary"] if len(blocked) == 1 else "部分草稿的保存要求尚未满足。"
            return result

    @tool(description="查询当前企业指定期间的金额汇总；多币种分别列示，不自动折算。")
    def enterprise_finance_summary(period: str, company_id: str | None = None, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (_, target, _, actor, service):
            return service.finance_summary(user=actor, company_id=target, period=period)

    @tool(description="读取当前用户在这个工作项上传的附件识别结果；PDF 较长时可指定从1开始的页码读取对应文本。")
    def enterprise_read_attachment(attachment_id: str, page: int | None = None, company_id: str | None = None,
                                   runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (context, target, database, _, _):
            original = database.connection.execute("""SELECT session_id,task_id FROM enterprise_original_attachments
                WHERE attachment_id=? AND company_id=? AND user_id=? AND source_kind='assistant_turn'""",
                (attachment_id, target, context.user_id)).fetchone()
            if original:
                if original["task_id"] != context.active_work_item_id:
                    raise ValueError("没有找到当前工作项下可读取的附件。")
                from .reply_attachments import get_attachment
                attachment = get_attachment(database, user={"user_id": context.user_id}, attachment_id=attachment_id)
                prepared = attachment["prepared"] or {}
                result = {"status": "ok", "attachment_id": attachment_id, "filename": attachment["filename"],
                          "processing_status": attachment["processing_status"], "error": attachment["last_error_message"],
                          "page_count": prepared.get("page_count"), **(attachment["recognized"] or {})}
                if page is not None:
                    pages = prepared.get("pages", [])
                    if page < 1 or page > len(pages):
                        raise ValueError("PDF 页码超出范围。")
                    result["page_text"] = pages[page - 1].get("text", "")
                return result
            row = database.connection.execute("""SELECT extracted_json FROM enterprise_assistant_attachments
                WHERE attachment_id=? AND company_id=? AND task_id=? AND user_id=?""",
                (attachment_id, target, context.active_work_item_id, context.user_id)).fetchone()
            if row is None:
                raise ValueError("没有找到当前工作项下可读取的附件。")
            result = json.loads(row["extracted_json"])
            text = result.pop("pdf_text", "")
            if page is not None:
                pages = text.split("\f")
                if page < 1 or page > result["page_count"]:
                    raise ValueError("PDF 页码超出范围。")
                result["page_text"] = pages[page - 1] if page <= len(pages) else ""
            return {"status": "ok", "attachment_id": attachment_id, **result}

    @tool(description="按用户明确要求切换当前办理企业，不改变原企业工作项。")
    def enterprise_switch_company(company_id: str, runtime: ToolRuntime[AgentContext] = None) -> dict:
        with _service(runtime, company_id) as (context, target, _, _, _):
            AgentStore(context.agent_store_path).set_company(context.session_id, context.user_id, target)
            with context.lock:
                context.current_company_id = target
                context.response_company_id = None
                context.active_work_item_id = None
            return {"status": "ok", "current_company_id": target, "summary": "当前办理企业已切换。"}

    return [enterprise_list_work_items, enterprise_inspect_work_item, enterprise_handover, enterprise_create_draft, enterprise_correct_draft,
            enterprise_confirm_drafts, enterprise_finance_summary, enterprise_read_attachment, enterprise_switch_company]


class EnterpriseToolErrors(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        except (TaxHarnessError, PermissionError, ValueError) as error:
            status = getattr(error, "code", "permission_denied" if isinstance(error, PermissionError) else "invalid_input")
            return ToolMessage(content=json.dumps({"status": status, "summary": str(error)}, ensure_ascii=False),
                               tool_call_id=request.tool_call["id"], name=request.tool_call.get("name"))


class EnterpriseProgramContext(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        context = request.runtime.context
        with EnterpriseDatabase(context.database_path) as database:
            actor = _actor(database, context.user_id)
            service = EnterpriseService(database)
            service._role(actor, context.current_company_id, {"accountant", "reviewer"})
            TaxService(database).require_access(user_id=context.user_id, company_id=context.current_company_id)
            items = service.list_work_items(user=actor, company_id=context.current_company_id, page_size=100)["items"]
        snapshot = {"user_id": context.user_id, "current_company_id": context.current_company_id,
                    "current_date_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                    "active_work_item_id": context.active_work_item_id,
                    "work_items": [{key: item.get(key) for key in ("task_id", "period", "status", "invoice_count", "pending_count", "review_count")} for item in items]}
        if context.recovered_operations:
            snapshot["recovered_operations"] = context.recovered_operations
        if context.attachment_observations:
            snapshot["attachment_observations"] = context.attachment_observations
        from langchain_core.utils.function_calling import convert_to_openai_tool
        tools = [convert_to_openai_tool(item) for item in request.tools]
        base = request.system_message.content if request.system_message else ""
        system, compiled = model_context(base, snapshot, tools, request.messages,
            token_budget=context.context_budget_tokens, tag="enterprise_work_context")
        return handler(request.override(system_message=SystemMessage(content=system), messages=compiled))


class EnterpriseAgentRuntime(AgentRuntime):
    def __init__(self, *, active_task_id: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.active_task_id = active_task_id

    def _prepare_context(self, context: AgentContext) -> None:
        context.active_work_item_id = self.active_task_id

    def _database_ready(self) -> bool:
        try:
            with closing(sqlite3.connect(Path(self.database_path).resolve().as_uri() + "?mode=ro", uri=True)) as database:
                row = database.execute("SELECT value FROM meta WHERE key='enterprise_schema_version'").fetchone()
                return bool(row and database.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        except sqlite3.Error:
            return False

    def _context_snapshot(self, user_id: str, company_id: str) -> dict[str, Any]:
        with EnterpriseDatabase(self.database_path) as database:
            actor = _actor(database, user_id)
            service = EnterpriseService(database)
            service._role(actor, company_id, {"accountant", "reviewer"})
            return {"user_id": user_id, "current_company_id": company_id,
                    "work_items": service.list_work_items(user=actor, company_id=company_id, page_size=100)["items"]}

    def run_turn(self, *, session_id: str, user_id: str, company_id: str | None, message: str, model=None,
                 live_authorized: bool = False, budget_usd: float | None = None,
                 attachment_observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self._database_ready():
            return {"status": "database_not_initialized", "summary": "企业工作台尚未完成初始化。"}
        try:
            session = self.agent_store.session(session_id, user_id)
        except SessionOwnershipError as error:
            return {"status": "permission_denied", "summary": str(error)}
        target = company_id or (session or {}).get("current_company_id")
        if not target:
            return {"status": "missing_input", "summary": "请先选择企业。"}
        try:
            with EnterpriseDatabase(self.database_path) as database:
                actor = _actor(database, user_id)
                EnterpriseService(database)._role(actor, target, {"accountant", "reviewer"})
                TaxService(database).require_access(user_id=user_id, company_id=target)
        except (TaxHarnessError, PermissionError) as error:
            return {"status": "permission_denied", "summary": str(error)}
        return super().run_turn(session_id=session_id, user_id=user_id, company_id=company_id, message=message,
                                model=model, live_authorized=live_authorized, budget_usd=budget_usd,
                                attachment_observations=attachment_observations)

    def _graph(self, model):
        key = id(model)
        if key not in self._graph_cache:
            self._graph_cache[key] = create_agent(model=model, tools=make_enterprise_tools(), system_prompt=SYSTEM,
                context_schema=AgentContext, checkpointer=self.checkpointer,
                middleware=[CancellationMiddleware(), BatchGuardMiddleware(), EnterpriseToolErrors(), EnterpriseProgramContext(), BudgetMiddleware()],
                name="enterprise-taxharness-agent")
        return self._graph_cache[key]

    def _recover_unfinished_tools(self, graph, config, context: AgentContext, previous_turn_id: str | None) -> bool:
        snapshot = graph.get_state(config)
        messages = snapshot.values.get("messages", [])
        if not snapshot.next or not messages or not isinstance(messages[-1], AIMessage) or not messages[-1].tool_calls:
            return False
        recovered = []
        for call in messages[-1].tool_calls:
            payload: dict[str, Any] = {"status": "interrupted_unknown", "summary": "上一轮操作被中断，请先读取工作项和草稿状态再决定下一步。"}
            if call["name"] == "enterprise_confirm_drafts":
                items = []
                with EnterpriseDatabase(context.database_path) as database:
                    actor = _actor(database, context.user_id)
                    for reference in call.get("args", {}).get("drafts", []):
                        draft = database.connection.execute("SELECT company_id,invoice_id FROM enterprise_drafts WHERE draft_id=?", (reference.get("draft_id"),)).fetchone()
                        if draft is None:
                            items.append({"draft_id": reference.get("draft_id"), "status": "no_receipt"})
                            continue
                        EnterpriseService(database)._role(actor, draft["company_id"], {"accountant", "reviewer"})
                        TaxService(database).require_access(user_id=context.user_id, company_id=draft["company_id"])
                        row = database.connection.execute("SELECT * FROM enterprise_confirmation_receipts WHERE draft_id=?", (reference.get("draft_id"),)).fetchone()
                        if row is None:
                            row = database.connection.execute("""SELECT r.* FROM enterprise_confirmation_receipts r JOIN operations o
                                ON o.idempotency_key=r.operation_id AND o.company_id=r.company_id AND o.object_id=r.invoice_id
                                WHERE o.user_id=? AND o.company_id=? AND json_extract(o.result_json,'$.draft_id')=?
                                AND json_extract(o.result_json,'$.confirmed_draft_revision')=? ORDER BY r.created_at DESC LIMIT 1""",
                                (context.user_id, draft["company_id"], reference.get("draft_id"), reference.get("draft_revision"))).fetchone()
                        if row is None:
                            items.append({"draft_id": reference.get("draft_id"), "status": "no_receipt"})
                            continue
                        original = json.loads(row["result_json"])
                        if original.get("confirmed_draft_revision") != reference.get("draft_revision"):
                            items.append({"draft_id": reference.get("draft_id"), "status": "draft_conflict"})
                            continue
                        current = TaxService(database).read_invoice(user_id=context.user_id, company_id=row["company_id"], invoice_id=row["invoice_id"])
                        fields = original.get("confirmed_fields") or [field for field in ("purpose", "remarks") if original.get("before", {}).get(field) != original.get("after", {}).get(field)]
                        matches = current.get("status") == "ok" and all(current["data"].get(field) == original.get("after", {}).get(field) for field in fields)
                        items.append({"draft_id": reference.get("draft_id"), "status": "duplicate" if matches else "duplicate_changed",
                                      "original_status": original.get("status"), "result": original, "current": current.get("data"),
                                      "current_matches_receipt": matches})
                payload = {"status": "recovered", "items": items, "summary": "已核对确认回执；未找到回执的项目请先刷新工作项。"}
                context.recovered_operations.append(payload)
            recovered.append(ToolMessage(content=json.dumps(payload, ensure_ascii=False), tool_call_id=call["id"], name=call["name"]))
        graph.update_state(config, {"messages": recovered}, as_node="tools")
        return True

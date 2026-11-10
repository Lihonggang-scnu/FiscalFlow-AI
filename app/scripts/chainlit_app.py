"""Local Chainlit UI adapter for the existing TaxAgentHarness AgentRuntime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import chainlit as cl
from chainlit.context import context as chainlit_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from taxharness.agent import AgentRuntime  # noqa: E402
from taxharness.agent_store import AgentStore  # noqa: E402
from taxharness.conversation_attachments import ConversationAttachments  # noqa: E402
from taxharness.conversation_history import ConversationHistoryStore, checkpoint_thread_ids, companies_used  # noqa: E402
from taxharness.context_presentation import read_period_context  # noqa: E402
from taxharness.errors import SessionOwnershipError, TaxHarnessError  # noqa: E402
from taxharness.invoice_undo import undo_invoice_change  # noqa: E402
from taxharness.services import TaxService  # noqa: E402
from taxharness.storage import TaxDatabase  # noqa: E402
from taxharness.task_ledger import TaskLedger  # noqa: E402
from taxharness.task_manager import TaskManager  # noqa: E402
from taxharness.workspace_presentation import read_workspace_page, sync_workspace_draft, workspace_shared_draft  # noqa: E402


_WORKER_LOCK = asyncio.Lock()
_IDENTITY_BY_BROWSER: dict[str, tuple[str, str, str | None]] = {}


def _browser_identity_key() -> str:
    """Use only the named per-tab cookie; never hash unrelated browser cookies."""

    environment = getattr(chainlit_context.session, "environ", {}) or {}
    cookie = str(environment.get("HTTP_COOKIE", ""))
    for item in cookie.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name == "taxharness_tab_id" and value:
            return "tab:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    return "session:" + str(getattr(chainlit_context.session, "id", ""))


def _path(value: str | None, default: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _config() -> dict[str, Any]:
    database = _path(os.getenv("TAXHARNESS_DATABASE"), PROJECT_ROOT / "artifacts" / "chainlit" / "live-business.sqlite3")
    checkpoint_store = _path(os.getenv("TAXHARNESS_CHECKPOINT_STORE"), PROJECT_ROOT / "artifacts" / "chainlit" / "checkpoints.sqlite3")
    return {
        "database": database,
        "agent_store": _path(os.getenv("TAXHARNESS_AGENT_STORE"), PROJECT_ROOT / "artifacts" / "agent" / "agent.sqlite3"),
        "preference_store": _path(os.getenv("TAXHARNESS_PREFERENCE_STORE"), PROJECT_ROOT / "artifacts" / "chainlit" / "preferences.sqlite3"),
        "checkpoint_store": checkpoint_store,
        "history_store": _path(os.getenv("TAXHARNESS_HISTORY_STORE"), database.with_name("conversation-history.sqlite3")),
        "budget_usd": float(os.getenv("TAXHARNESS_UI_BUDGET_USD", "2")),
    }


class UiState:
    def __init__(self, user_id: str, company_id: str, session_id: str):
        self.user_id = user_id
        self.company_id = company_id
        self.session_id = session_id
        self.generation = 0
        self.worker: asyncio.Task | None = None
        self.runtime: AgentRuntime | None = None
        self.closing = False
        self.stop_requested = False
        self.await_new_thread = False
        self.reply_messages: dict[str, dict[str, Any]] = {}
        self.diagnostic_steps: dict[str, cl.Step] = {}
        self.history_panel_session_id: str | None = None
        self.expanded_history_turns: set[str] = set()
        self.expanded_history_replies: set[str] = set()
        self.history_offset = 0
        self.context_month: str | None = None
        self.context_invoice_id: str | None = None
        self.context_invoice_ids: set[str] = set()
        self.active_page = "B"
        self.workspace_task_id: str | None = None
        self.workspace_invoice_id: str | None = None
        self.workspace_data: dict[str, Any] = {
            "month": None, "can_modify": False, "tasks": [], "invoices": [], "rules": [],
            "frontier": None, "diff": {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}},
            "recent_changes": [],
        }
        self.workspace_drafts: dict[str, dict[str, Any]] = {}
        self.workspace_recent_turns: list[dict[str, str]] = []
        self.workspace_turn_id: str | None = None
        self.workspace_notice = ""
        self.is_narrow = False
        self.welcome_panel: cl.CustomElement | None = None


def _state() -> UiState:
    state = cl.user_session.get("taxharness_ui_state")
    if not isinstance(state, UiState):
        raise RuntimeError("UI 会话尚未初始化，请重新打开对话。")
    return state


def _company_options(user_id: str, database_path: Path) -> dict[str, str]:
    with TaxDatabase(database_path) as database:
        rows = database.connection.execute(
            """SELECT c.company_id,c.name FROM companies c
               JOIN user_company_permissions p ON p.company_id=c.company_id
               WHERE p.user_id=? AND p.can_view=1 ORDER BY c.company_id""",
            (user_id,),
        ).fetchall()
    return {f"{row['company_id']} · {row['name']}": row["company_id"] for row in rows}


def _user_options(database_path: Path) -> dict[str, str]:
    with TaxDatabase(database_path) as database:
        rows = database.connection.execute("SELECT user_id,name FROM users ORDER BY user_id").fetchall()
    return {f"{row['user_id']} · {row['name']}": row["user_id"] for row in rows}


def _check_access(user_id: str, company_id: str, database_path: Path) -> None:
    with TaxDatabase(database_path) as database:
        TaxService(database).require_access(user_id=user_id, company_id=company_id)


def _has_view_access_readonly(user_id: str, company_id: str, database_path: Path) -> bool:
    uri = database_path.expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT can_view FROM user_company_permissions WHERE user_id=? AND company_id=?",
            (user_id, company_id),
        ).fetchone()
    return bool(row and row[0])


class HistoryUnavailable(Exception):
    pass


def _refresh_history(user_id: str, config: dict[str, Any]) -> ConversationHistoryStore:
    history = ConversationHistoryStore(
        config["history_store"], config["database"], config["checkpoint_store"]
    )
    checkpoint_threads = checkpoint_thread_ids(config["checkpoint_store"])
    if not checkpoint_threads:
        return history
    agent_store = AgentStore(config["agent_store"])
    for session_id in sorted(thread for thread in checkpoint_threads if thread.startswith("chainlit-")):
        try:
            session = agent_store.session(session_id, user_id)
        except SessionOwnershipError:
            continue
        if session is None:
            continue
        turns = agent_store.list_all_turns(session_id, user_id)
        if turns:
            history.upsert(session, turns)
    return history


def _load_history_conversation(
    state: UiState,
    session_id: str,
    config: dict[str, Any],
    history: ConversationHistoryStore | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    history = history or _refresh_history(state.user_id, config)
    entry = history.get(session_id, state.user_id)
    if entry is None:
        raise HistoryUnavailable("该历史对话不存在，或不属于当前用户和业务库。")
    if session_id not in checkpoint_thread_ids(config["checkpoint_store"]):
        raise HistoryUnavailable("该历史对话的持久上下文不可用，暂时无法查看或继续。")
    agent_store = AgentStore(config["agent_store"])
    try:
        session = agent_store.session(session_id, state.user_id)
    except SessionOwnershipError as error:
        raise HistoryUnavailable("该历史对话不存在，或不属于当前用户。") from error
    if session is None:
        raise HistoryUnavailable("该历史对话不存在，或不属于当前用户。")
    turns = agent_store.list_all_turns(session_id, state.user_id)
    if not turns:
        raise HistoryUnavailable("该历史对话没有可查看的已保存回合。")
    for company_id in set(entry.company_ids) | {str(session["current_company_id"])}:
        try:
            _check_access(state.user_id, company_id, config["database"])
        except TaxHarnessError as error:
            raise HistoryUnavailable("该历史对话包含当前无权访问的企业，暂时不能查看或继续。") from error
    return session, sorted(turns, key=lambda turn: str(turn["created_at"]))


def _history_actions(session_id: str) -> list[cl.Action]:
    return [
        cl.Action(
            name="conversation_history_view",
            label="查看",
            tooltip="查看已保存的用户消息和助手回复，不重放工具。",
            payload={"session_id": session_id},
        ),
        cl.Action(
            name="conversation_history_continue",
            label="继续此对话",
            tooltip="在同一 Agent 会话和持久上下文中继续。",
            payload={"session_id": session_id},
        ),
    ]


def _history_title(turns: list[dict[str, Any]]) -> str:
    prompt = next((str(turn.get("request_text", "")).strip() for turn in turns if turn.get("request_text")), "未命名对话")
    prompt = re.sub(r"\s+", " ", prompt)
    return prompt if len(prompt) <= 72 else prompt[:71] + "…"


def _history_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return value[:16]


_HISTORY_PAGE_SIZE = 50
_REPLY_PREVIEW_CHARS = 1800
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _reply_preview(reply: str) -> str:
    if len(reply) <= _REPLY_PREVIEW_CHARS:
        return reply
    boundary = max(reply.rfind("\n", 0, _REPLY_PREVIEW_CHARS), reply.rfind(" ", 0, _REPLY_PREVIEW_CHARS))
    if boundary < _REPLY_PREVIEW_CHARS // 2:
        boundary = _REPLY_PREVIEW_CHARS
    preview = reply[:boundary].rstrip() + "\n\n…"
    if preview.count("```") % 2:
        preview += "\n```"
    return preview


def _history_card(entry: Any, session: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    first_question = next((str(turn.get("request_text", "")).strip() for turn in turns if turn.get("request_text")), "")
    return {
        "session_id": entry.session_id,
        "title": _history_title(turns),
        "preview": re.sub(r"\s+", " ", first_question)[:180],
        "company_id": str(session["current_company_id"]),
        "updated_at": _history_time(entry.updated_at),
        "turn_count": entry.turn_count,
        "status": "最近回合已回复" if entry.latest_status == "ok" else f"最近回合：{entry.latest_status}",
    }


def _history_page_props(state: UiState, offset: int) -> dict[str, Any]:
    config = _config()
    history = _refresh_history(state.user_id, config)
    entries = history.list(state.user_id, limit=_HISTORY_PAGE_SIZE, offset=offset)
    cards: list[dict[str, Any]] = []
    for entry in entries:
        try:
            session, turns = _load_history_conversation(state, entry.session_id, config, history)
        except HistoryUnavailable:
            continue
        cards.append(_history_card(entry, session, turns))

    return {
        "history_entries": cards,
        "history_offset": offset,
        "history_has_more": bool(history.list(state.user_id, limit=1, offset=offset + _HISTORY_PAGE_SIZE)),
        "session_id": state.session_id,
    }


async def _publish_history(state: UiState, offset: int | None = None) -> None:
    if offset is not None:
        state.history_offset = max(0, offset)
    if state.welcome_panel is None:
        return
    state.welcome_panel.props.update({
        **_history_page_props(state, state.history_offset),
        **_workspace_panel_props(state),
    })
    await state.welcome_panel.update()


async def _render_history_transcript(
    state: UiState,
    session: dict[str, Any],
    turns: list[dict[str, Any]],
    *,
    continued: bool = False,
) -> None:
    session_id = str(session["session_id"])
    if state.history_panel_session_id != session_id:
        state.expanded_history_turns.clear()
        state.expanded_history_replies.clear()
    state.history_panel_session_id = session_id
    files_by_turn = ConversationAttachments(_config()["agent_store"]).history(session_id=session_id, user_id=state.user_id)
    elements: list[Any] = [cl.CustomElement(
        name="ConversationHistory",
        props={
            "view": "transcript",
            "title": _history_title(turns),
            "session_id": str(session["session_id"]),
            "turn_count": len(turns),
            "continued": continued,
            "turns": [
                {"request_text": str(turn.get("request_text", "")), "output_text": str(turn.get("output_text", ""))}
                for turn in turns
            ],
        },
    )]
    for number, turn in enumerate(turns, start=1):
        reply = str(turn.get("output_text") or "")
        if not reply:
            reply = f"（本轮状态：{turn.get('status', 'unknown')}）"
        turn_id = str(turn.get("turn_id", number))
        turn_key = f"{session_id}:{turn_id}"
        expanded = turn_key in state.expanded_history_turns
        reply_expanded = turn_key in state.expanded_history_replies
        visible_reply = reply if reply_expanded or len(reply) <= _REPLY_PREVIEW_CHARS else _reply_preview(reply)
        if expanded:
            turn_content = (
                f"### 第 {number} 回合\n\n"
                f"**用户**\n\n{turn.get('request_text', '')}\n\n"
                f"**助手**\n\n{visible_reply}"
            )
        else:
            question = re.sub(r"\s+", " ", str(turn.get("request_text", ""))).strip()
            question = question[:180] + ("…" if len(question) > 180 else "")
            turn_content = f"### 第 {number} 回合\n\n{question or '已保存的用户消息与助手回复'}\n\n展开以查看完整记录。"
        elements.append(cl.Text(
            name=f"第{number}回合",
            content=turn_content,
            display="inline",
        ))
        if expanded:
            for item in files_by_turn.get(turn_id, []):
                elements.append(cl.File(name=item["filename"], path=str(item["path"]), display="inline"))
        elements.append(cl.CustomElement(
            name="ReplyCard",
            props={
                "text": reply,
                "long": len(reply) > _REPLY_PREVIEW_CHARS,
                "turn_toggle": True,
                "turn_label": f"第 {number} 回合",
                "turn_expanded": expanded,
                "reply_expanded": reply_expanded,
                "history_turn_id": turn_id,
                "history_session_id": str(session["session_id"]),
                "copy_label": "复制本条回复",
            },
        ))
    await cl.ElementSidebar.set_elements(elements)
    await cl.ElementSidebar.set_title("历史对话")


async def _render_resumed_conversation(
    state: UiState,
    turns: list[dict[str, Any]],
) -> None:
    """Show stored text in the chat stream without rerunning or saving Agent turns."""

    state.reply_messages.clear()
    files_by_turn = ConversationAttachments(_config()["agent_store"]).history(session_id=state.session_id, user_id=state.user_id)
    for turn in turns:
        request = str(turn.get("request_text") or "")
        if request:
            files = [cl.File(name=item["filename"], path=str(item["path"]), display="inline")
                     for item in files_by_turn.get(str(turn.get("turn_id", "")), [])]
            await cl.Message(
                content=request,
                author=state.user_id,
                type="user_message",
                elements=files,
            ).send()

        reply = str(turn.get("output_text") or "")
        if not reply:
            reply = f"（本轮状态：{turn.get('status', 'unknown')}）"
        long_reply = len(reply) > _REPLY_PREVIEW_CHARS
        card = cl.CustomElement(
            name="ReplyCard",
            props={
                "text": reply,
                "long": long_reply,
                "expanded": not long_reply,
                "copy_label": "复制回复",
            },
        )
        answer = cl.Message(
            content=_reply_preview(reply) if long_reply else reply,
            actions=[cl.Action(name="show_history", label="刷新历史", tooltip="更新左侧对话列表。", payload={})],
            elements=[card],
        )
        card.props["message_id"] = answer.id
        await answer.send()
        state.reply_messages[answer.id] = {
            "message": answer,
            "card": card,
            "text": reply,
            "expanded": not long_reply,
        }


async def _publish_settings(state: UiState) -> None:
    config = _config()
    users = _user_options(config["database"])
    companies = _company_options(state.user_id, config["database"])
    if state.company_id not in companies.values() and companies:
        state.company_id = next(iter(companies.values()))
    settings = cl.ChatSettings([
        cl.input_widget.Select(
            id="ui_user_id", label="用户", items=users, initial_value=state.user_id,
            description="本机模拟用户；切换会开启新的 Agent 会话。",
        ),
        cl.input_widget.Select(
            id="ui_company_id", label="当前企业", items=companies or {"无可访问企业": ""}, initial_value=state.company_id,
            description="只显示该用户有查看权限的企业。",
        ),
    ])
    await settings.send()
    await _update_welcome_panel(state, config["database"])


def _welcome_identity_props(state: UiState, database_path: Path) -> dict[str, str]:
    user_label = next((label for label, value in _user_options(database_path).items() if value == state.user_id), state.user_id)
    company_label = next((label for label, value in _company_options(state.user_id, database_path).items() if value == state.company_id), state.company_id)
    return {
        "user_id": state.user_id,
        "user_label": user_label,
        "company_id": state.company_id,
        "company_label": company_label,
    }


async def _update_welcome_panel(state: UiState, database_path: Path) -> None:
    if state.welcome_panel is None:
        return
    state.welcome_panel.props.update({
        **_welcome_identity_props(state, database_path),
        **_workspace_panel_props(state),
    })
    await state.welcome_panel.update()


async def _update_workspace_element(state: UiState) -> None:
    if state.welcome_panel is None:
        return
    state.welcome_panel.props.update(_workspace_panel_props(state))
    await state.welcome_panel.update()


def _workspace_panel_props(state: UiState) -> dict[str, Any]:
    draft_key = _workspace_draft_key(state, state.workspace_invoice_id)
    return {
        "active_page": state.active_page,
        "workspace_month": state.context_month,
        "workspace_data": state.workspace_data,
        "workspace_task_id": state.workspace_task_id,
        "workspace_invoice_id": state.workspace_invoice_id,
        "workspace_draft": (state.workspace_drafts.get(draft_key) or workspace_shared_draft(state.workspace_data, state.workspace_invoice_id)) if draft_key else {},
        "workspace_recent_turns": state.workspace_recent_turns,
        "workspace_notice": state.workspace_notice,
    }


def _workspace_draft_key(state: UiState, invoice_id: str | None) -> str | None:
    if not invoice_id or not state.context_month:
        return None
    return f"{state.user_id}:{state.company_id}:{state.context_month}:{invoice_id}"


def _workspace_task_for_invoice(state: UiState, invoice_id: str, saved_task: dict[str, Any] | None = None) -> str | None:
    linked_statuses = {"waiting_input", "open", "out_of_scope", "completed"}
    if saved_task and saved_task.get("status") in linked_statuses and invoice_id in saved_task.get("target_invoice_ids", []):
        return saved_task.get("task_id")
    frontier = state.workspace_data.get("frontier") or {}
    if frontier.get("task_status") in linked_statuses and invoice_id in frontier.get("target_invoice_ids", []):
        return frontier.get("task_id")
    tasks = state.workspace_data.get("tasks", [])
    selected = next((task for task in tasks if task.get("task_id") == state.workspace_task_id), None)
    if selected and selected.get("status") in linked_statuses and invoice_id in selected.get("target_invoice_ids", []):
        return selected.get("task_id")
    task = next((item for item in tasks if invoice_id in item.get("target_invoice_ids", [])
                 and item.get("status") in {"waiting_input", "open", "out_of_scope"}), None)
    if task is None:
        task = next((item for item in tasks if invoice_id in item.get("target_invoice_ids", [])
                     and item.get("status") == "completed"), None)
    return task.get("task_id") if task else None


def _refresh_workspace_recent_turns(state: UiState, config: dict[str, Any]) -> None:
    state.workspace_turn_id = None
    if state.session_id not in checkpoint_thread_ids(config["checkpoint_store"]):
        state.workspace_recent_turns = []
        return
    agent_store_path = Path(config["agent_store"]).expanduser().resolve()
    uri = agent_store_path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        session_row = connection.execute(
            "SELECT * FROM agent_sessions WHERE session_id=?",
            (state.session_id,),
        ).fetchone()
        if session_row is None or session_row["user_id"] != state.user_id:
            state.workspace_recent_turns = []
            return
        session = dict(session_row)
        turn_rows = connection.execute(
            "SELECT * FROM agent_turns WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 100",
            (state.session_id, state.user_id),
        ).fetchall()
        turns = [dict(row) for row in turn_rows]
    if any(not _has_view_access_readonly(state.user_id, company_id, config["database"]) for company_id in companies_used(session, turns)):
        state.workspace_recent_turns = []
        return
    for turn in turns:
        if turn.get("status") != "ok":
            continue
        try:
            context = json.loads(turn.get("input_context_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if context.get("current_company_id") == state.company_id:
            state.workspace_turn_id = str(turn.get("turn_id") or "") or None
            break
    recent = []
    for turn in reversed(turns):
        if turn.get("status") != "ok":
            continue
        try:
            context = json.loads(turn.get("input_context_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if context.get("current_company_id") != state.company_id:
            continue
        request_text = str(turn.get("request_text") or "").strip()
        output_text = str(turn.get("output_text") or "").strip()
        if request_text or output_text:
            recent.append({"request_text": request_text, "output_text": output_text})
        if len(recent) == 2:
            break
    state.workspace_recent_turns = list(reversed(recent))


async def _publish_workspace_page(state: UiState, database_path: Path, *, refresh_tasks: bool = False) -> None:
    try:
        state.workspace_data = read_workspace_page(
            database_path=database_path,
            user_id=state.user_id,
            company_id=state.company_id,
            month=state.context_month,
            refresh_tasks=refresh_tasks,
            session_id=state.session_id,
            turn_id=state.workspace_turn_id or "",
        )
        state.workspace_notice = ""
    except (TaxHarnessError, ValueError) as error:
        state.workspace_data = {
            "month": state.context_month, "can_modify": False,
            "tasks": [], "invoices": [], "rules": [],
            "frontier": None, "diff": {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}},
            "recent_changes": [],
        }
        state.workspace_notice = str(error)
    except sqlite3.Error:
        state.workspace_data = {
            "month": state.context_month, "can_modify": False,
            "tasks": [], "invoices": [], "rules": [],
            "frontier": None, "diff": {"initial": True, "added_targets": [], "removed_targets": [], "changes": {}},
            "recent_changes": [],
        }
        state.workspace_notice = "工作区资料暂时无法读取，请刷新重试。"
    await _update_workspace_element(state)


async def _publish_context(state: UiState, *, error: str | None = None) -> None:
    config = _config()
    identity = {"company_label": state.company_id}
    try:
        identity = _welcome_identity_props(state, config["database"])
        context = read_period_context(
            database_path=config["database"],
            user_id=state.user_id,
            company_id=state.company_id,
            month=state.context_month,
        )
    except (TaxHarnessError, ValueError) as failure:
        context = {"invoices": [], "rules": [], "tasks": []}
        state.context_invoice_ids = set()
        state.context_invoice_id = None
        error = str(failure)
    except sqlite3.Error:
        context = {"invoices": [], "rules": [], "tasks": []}
        error = "资料暂时无法读取，请稍后重试。"

    state.context_invoice_ids = {str(invoice["invoice_id"]) for invoice in context["invoices"]}
    if state.context_invoice_id not in state.context_invoice_ids:
        state.context_invoice_id = None

    state.history_panel_session_id = None
    panel = cl.CustomElement(
        name="ContextPanel",
        props={
            "company_id": state.company_id,
            "company_label": identity["company_label"],
            "month": state.context_month,
            "invoice_id": state.context_invoice_id,
            "invoices": context["invoices"],
            "rules": context["rules"],
            "tasks": context["tasks"],
            "error": error or "",
        },
    )
    await cl.ElementSidebar.set_elements([panel])
    await cl.ElementSidebar.set_title("当前资料")


async def _close_context() -> None:
    await cl.ElementSidebar.set_elements([])


async def _wait_active(state: UiState) -> None:
    worker = state.worker
    if worker is None or worker.done():
        return
    state.stop_requested = True
    if state.runtime is not None:
        state.runtime.cancel(session_id=state.session_id, user_id=state.user_id)
    # Do not close runtime or release the lock while the worker is still using it.
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.shield(worker)
    except Exception:
        pass
    state.worker = None


def _uploaded_files(message: cl.Message) -> list[tuple[str, Path]]:
    elements = getattr(message, "elements", None) or []
    if not elements:
        return []
    if len(elements) > 10:
        raise ValueError("一次最多上传 10 个文件。")
    session = chainlit_context.session
    registered = session.files
    root = Path(session.files_dir).resolve()
    files = []
    total = 0
    for element in elements:
        item = registered.get(element.id)
        if item is None:
            raise ValueError("附件已失效，请重新上传。")
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("附件已失效，请重新上传。")
        size = path.stat().st_size
        if size > 32 * 1024 * 1024:
            raise ValueError("单个文件不能超过 32 MiB。")
        total += size
        files.append((str(item["name"]), path))
    if total > 64 * 1024 * 1024:
        raise ValueError("一次上传的文件总计不能超过 64 MiB。")
    return files


def _active_attachment_task(state: UiState, database_path: Path) -> str | None:
    with TaxDatabase(database_path) as database:
        tasks = TaskManager(database).list_tasks(user_id=state.user_id, company_id=state.company_id)["tasks"]
    return str(tasks[0]["task_id"]) if tasks else None


def _run_ui_turn(runtime: AgentRuntime, state: UiState, message: str, config: dict[str, Any],
                 files: list[tuple[str, Path]]) -> dict[str, Any]:
    attachments = ConversationAttachments(config["agent_store"])
    task_id = None
    new_ids: list[str] = []
    if files or attachments.path.exists():
        _check_access(state.user_id, state.company_id, config["database"])
        existing = AgentStore(config["agent_store"]).session(state.session_id, state.user_id)
        if existing and existing["current_company_id"] != state.company_id:
            return {"status": "session_company_mismatch", "summary": "当前企业已变化，请重新选择企业。"}
        task_id = _active_attachment_task(state, config["database"])
    if files:
        from taxharness.enterprise.multimodal import extract_document, prepare_document
        if config["budget_usd"] <= 0 or runtime.budget_ledger_path == str(runtime.agent_store.path):
            raise ValueError("票据识别需要启用模型和本阶段预算，请在可用时段重试。")
        for filename, path in files:
            if state.stop_requested:
                return {"status": "cancelled", "session_id": state.session_id, "summary": "本轮已停止。"}
            content = path.read_bytes()
            document = prepare_document(filename, content)
            extracted = extract_document(document, user_text=message, user_id=state.user_id, session_id=state.session_id,
                turn_id="vision-" + uuid.uuid4().hex, budget_ledger_path=runtime.budget_ledger_path,
                budget_usd=config["budget_usd"])
            new_ids.append(attachments.add(session_id=state.session_id, user_id=state.user_id,
                company_id=state.company_id, task_id=task_id, document=document, extracted=extracted, content=content))
    if state.stop_requested:
        return {"status": "cancelled", "session_id": state.session_id, "summary": "本轮已停止。"}
    observations = attachments.observations(session_id=state.session_id, user_id=state.user_id,
                                             company_id=state.company_id, task_id=task_id)
    result = runtime.run_turn(session_id=state.session_id, user_id=state.user_id, company_id=state.company_id,
        message=message, live_authorized=True, budget_usd=config["budget_usd"], attachment_observations=observations)
    if new_ids and result.get("turn_id"):
        task_id = task_id or _active_attachment_task(state, config["database"])
        attachments.attach_turn(new_ids, result["turn_id"], task_id)
    return result


async def _execute_turn(state: UiState, message: str, generation: int,
                        files: list[tuple[str, Path]] | None = None) -> dict[str, Any]:
    config = _config()
    runtime = AgentRuntime(
        database_path=config["database"],
        agent_store_path=config["agent_store"],
        checkpoint_path=config["checkpoint_store"],
        preference_store_path=config["preference_store"],
    )
    state.runtime = runtime
    try:
        if state.stop_requested or generation != state.generation:
            return {"status": "cancelled", "session_id": state.session_id, "summary": "本轮在排队时已停止。"}
        async with _WORKER_LOCK:
            if state.stop_requested or generation != state.generation:
                return {"status": "cancelled", "session_id": state.session_id, "summary": "本轮在排队时已停止。"}
            return await asyncio.to_thread(_run_ui_turn, runtime, state, message, config, files or [])
    finally:
        # This runs only after the worker has returned; cancellation requests are
        # written first and never close a runtime that is still in to_thread().
        runtime.close()
        if state.runtime is runtime:
            state.runtime = None


def _safe(value: Any, key: str | None = None) -> Any:
    if key and any(token in key.lower() for token in ("api_key", "password", "authorization", "secret", "access_token", "refresh_token", "credential")):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str) and len(value) > 12000:
        return value[:12000] + "\n[truncated]"
    return value


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _diagnostic(state: UiState, result: dict[str, Any]) -> dict[str, Any]:
    store = AgentStore(_config()["agent_store"])
    turns = store.list_turns(state.session_id, state.user_id, limit=10)
    turn_id = result.get("turn_id")
    turns = [turn for turn in turns if not turn_id or turn.get("turn_id") == turn_id]
    calls = store.list_model_calls(state.session_id, state.user_id, limit=50, turn_id=turn_id)
    return _safe({
        "session_id": state.session_id,
        "user_id": state.user_id,
        "current_company_id": result.get("current_company_id", state.company_id),
        "turns": [
            {**turn, "input_context_json": _json_value(turn.get("input_context_json")), "result_json": _json_value(turn.get("result_json"))}
            for turn in turns
        ],
        "model_calls": [
            {**call, "input_json": _json_value(call.get("input_json")), "output_json": _json_value(call.get("output_json"))}
            for call in calls
        ],
        "messages": result.get("messages", []),
    })


async def _send_diagnostic(state: UiState, result: dict[str, Any]) -> None:
    step = cl.Step(
        name="本轮过程",
        type="run",
        default_open=False,
        show_input="json",
        elements=[cl.CustomElement(name="DiagnosticControls", props={"step_id": "pending"})],
    )
    step.elements[0].props["step_id"] = step.id
    diagnostic = _diagnostic(state, result)
    step.input = {"session_id": state.session_id, "turn": diagnostic["turns"][:1]}
    step.output = diagnostic
    await step.send()
    state.diagnostic_steps[step.id] = step


@cl.on_app_startup
async def on_app_startup() -> None:
    config = _config()
    if config["budget_usd"] > 0:
        os.environ.setdefault("TAXHARNESS_LIVE_MODEL_ENABLED", "true")
    if not config["database"].is_file():
        print(f"Chainlit UI database is missing: {config['database']}")
    print(f"TaxAgentHarness Chainlit UI ready on configured paths; database={config['database']}")


@cl.on_chat_start
async def on_chat_start() -> None:
    config = _config()
    browser_key = _browser_identity_key()
    identity = _IDENTITY_BY_BROWSER.get(browser_key)
    user_id = str((identity[0] if identity else cl.user_session.get("taxharness_selected_user", os.getenv("TAXHARNESS_UI_USER_ID", "U001"))))
    companies = _company_options(user_id, config["database"]) if config["database"].is_file() else {}
    company_id = str((identity[1] if identity else cl.user_session.get("taxharness_selected_company", os.getenv("TAXHARNESS_UI_COMPANY_ID", next(iter(companies.values()), "")))))
    pending_session_id = identity[2] if identity and len(identity) > 2 else None
    if company_id not in companies.values():
        company_id = next(iter(companies.values()), "")
    if not companies or company_id not in companies.values():
        await cl.ErrorMessage(content="当前 UI 业务库不存在，或默认用户没有可访问企业；请先用启动脚本准备独立业务副本。").send()
        return
    _check_access(user_id, company_id, config["database"])
    state = UiState(user_id=user_id, company_id=company_id, session_id="chainlit-" + uuid.uuid4().hex)
    if pending_session_id:
        _IDENTITY_BY_BROWSER[browser_key] = (user_id, company_id, None)
    resumed_session: dict[str, Any] | None = None
    resumed_turns: list[dict[str, Any]] = []
    resume_error = False
    if pending_session_id:
        try:
            resumed_session, resumed_turns = _load_history_conversation(state, pending_session_id, config)
            state.session_id = pending_session_id
            state.company_id = str(resumed_session["current_company_id"])
        except HistoryUnavailable:
            resume_error = True
    cl.user_session.set("taxharness_ui_state", state)
    cl.user_session.set("taxharness_selected_user", user_id)
    cl.user_session.set("taxharness_selected_company", state.company_id)
    _IDENTITY_BY_BROWSER[browser_key] = (user_id, state.company_id, None)
    _refresh_workspace_recent_turns(state, config)
    await _publish_settings(state)
    welcome = cl.CustomElement(
        name="WelcomePanel",
        props={
            **_welcome_identity_props(state, config["database"]),
            **_history_page_props(state, state.history_offset),
            **_workspace_panel_props(state),
            "resumed": bool(resumed_session),
        },
    )
    welcome_content = "" if resumed_session else "在下方描述问题，或打开历史对话继续办理。"
    await cl.Message(content=welcome_content, elements=[welcome]).send()
    state.welcome_panel = welcome
    if resume_error:
        await cl.ErrorMessage(content="该历史对话不存在、超出当前业务库范围，或包含当前无权访问的企业；已打开空白新对话。历史数据仍保留。").send()
    if resumed_session:
        await _render_resumed_conversation(state, resumed_turns)


@cl.on_settings_update
async def on_settings_update(settings: dict[str, Any]) -> None:
    state = _state()
    requested_user = str(settings.get("ui_user_id") or state.user_id)
    requested_company = str(settings.get("ui_company_id") or state.company_id)
    if requested_user == state.user_id and requested_company == state.company_id:
        return
    await _wait_active(state)
    config = _config()
    if requested_user != state.user_id:
        companies = _company_options(requested_user, config["database"])
        if requested_company not in companies.values():
            requested_company = next(iter(companies.values()), "")
    try:
        if not requested_company:
            raise TaxHarnessError("所选用户没有可访问的企业。")
        _check_access(requested_user, requested_company, config["database"])
    except TaxHarnessError as error:
        await cl.ErrorMessage(content=str(error)).send()
        await _publish_settings(state)
        return
    previous_user_id = state.user_id
    discarded_invoice_draft = bool(state.workspace_drafts)
    state.user_id = requested_user
    state.company_id = requested_company
    if requested_user != previous_user_id:
        state.history_offset = 0
    state.context_month = None
    state.context_invoice_id = None
    state.context_invoice_ids.clear()
    state.workspace_task_id = None
    state.workspace_invoice_id = None
    state.workspace_drafts.clear()
    state.workspace_recent_turns = []
    state.active_page = "B"
    state.history_panel_session_id = None
    state.generation += 1
    state.stop_requested = False
    state.await_new_thread = True
    cl.user_session.set("taxharness_selected_user", requested_user)
    cl.user_session.set("taxharness_selected_company", requested_company)
    _IDENTITY_BY_BROWSER[_browser_identity_key()] = (requested_user, requested_company, None)
    if state.is_narrow:
        await _close_context()
    else:
        await _publish_context(state)
    await _publish_settings(state)
    await _publish_workspace_page(state, config["database"])
    await _publish_history(state)
    draft_notice = "当前会话未保存的用途和备注草稿已清除。" if discarded_invoice_draft else ""
    await cl.Message(content=f"已选择用户 {requested_user}、企业 {requested_company}。旧对话已保留，可从该用户的历史对话中打开；正在开始新的空白上下文。{draft_notice}").send()
    await cl.send_window_message({"type": "taxharness_new_thread", "reason": "identity_change"})


@cl.action_callback("new_conversation")
async def new_conversation(action: cl.Action) -> None:
    state = _state()
    await _wait_active(state)
    discarded_invoice_draft = bool(state.workspace_drafts)
    state.generation += 1
    state.stop_requested = False
    state.await_new_thread = True
    state.context_month = None
    state.context_invoice_id = None
    state.context_invoice_ids.clear()
    state.workspace_task_id = None
    state.workspace_invoice_id = None
    state.workspace_drafts.clear()
    state.workspace_recent_turns = []
    state.history_panel_session_id = None
    _IDENTITY_BY_BROWSER[_browser_identity_key()] = (state.user_id, state.company_id, None)
    if state.is_narrow:
        await _close_context()
    else:
        await _publish_context(state)
    await _publish_workspace_page(state, _config()["database"])
    await _publish_settings(state)
    draft_notice = "当前会话未保存的用途和备注草稿已清除。" if discarded_invoice_draft else ""
    await cl.Message(content=f"旧对话已保留，可从历史打开。正在为用户 {state.user_id}、企业 {state.company_id} 开始新的空白上下文。{draft_notice}").send()
    await cl.send_window_message({"type": "taxharness_new_thread", "reason": "new_conversation"})


@cl.action_callback("show_history")
async def show_history(action: cl.Action) -> None:
    await _publish_history(_state())


@cl.action_callback("close_history")
async def close_history(action: cl.Action) -> None:
    await _publish_context(_state())


@cl.action_callback("initialize_workspace")
async def initialize_workspace(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    is_narrow = payload.get("is_narrow")
    state.is_narrow = is_narrow if isinstance(is_narrow, bool) else False
    if state.history_panel_session_id:
        return
    if state.is_narrow:
        await _close_context()
    else:
        await _publish_context(state)


@cl.action_callback("set_workspace_page")
async def set_workspace_page(action: cl.Action) -> None:
    """Switch the visible workspace without touching the Agent session."""

    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    page = payload.get("page")
    if page not in {"A", "B", "C"}:
        return
    draft = payload.get("draft")
    if isinstance(draft, dict):
        invoice_id = draft.get("invoice_id")
        expected_revision = draft.get("expected_revision")
        changes = draft.get("changes")
        visible = {str(invoice["invoice_id"]): invoice for invoice in state.workspace_data.get("invoices", [])}
        if (
            isinstance(invoice_id, str)
            and invoice_id in visible
            and type(expected_revision) is int
            and isinstance(changes, dict)
            and set(changes) == {"purpose", "remarks"}
            and all(isinstance(value, str) for value in changes.values())
        ):
            key = _workspace_draft_key(state, invoice_id)
            if key:
                state.workspace_drafts[key] = {
                    "invoice_id": invoice_id,
                    "expected_revision": expected_revision,
                    "purpose": changes["purpose"],
                    "remarks": changes["remarks"],
                    "idempotency_key": draft.get("idempotency_key", ""),
                }
    state.active_page = page
    if page == "A":
        _refresh_workspace_recent_turns(state, _config())
    await _publish_workspace_page(state, _config()["database"])


@cl.action_callback("workspace_set_draft")
async def workspace_set_draft(action: cl.Action) -> None:
    """Keep an invoice-form edit and, when requested, share it with its task."""

    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    invoice_id = payload.get("invoice_id")
    expected_revision = payload.get("expected_revision")
    changes = payload.get("changes")
    modified_fields = payload.get("modified_fields", [])
    draft_event_ids = payload.get("draft_event_ids", {})
    idempotency_key = payload.get("idempotency_key", "")
    visible = {str(invoice["invoice_id"]): invoice for invoice in state.workspace_data.get("invoices", [])}
    key = _workspace_draft_key(state, invoice_id if isinstance(invoice_id, str) else None)
    if (
        key
        and invoice_id in visible
        and type(expected_revision) is int
        and isinstance(changes, dict)
        and set(changes) == {"purpose", "remarks"}
        and all(isinstance(value, str) for value in changes.values())
        and isinstance(modified_fields, list)
        and all(field in {"purpose", "remarks"} for field in modified_fields)
        and isinstance(draft_event_ids, dict)
    ):
        state.workspace_drafts[key] = {
            "invoice_id": invoice_id,
            "expected_revision": expected_revision,
            "purpose": changes["purpose"],
            "remarks": changes["remarks"],
            "modified_fields": modified_fields,
            "draft_event_ids": draft_event_ids,
            "idempotency_key": idempotency_key if isinstance(idempotency_key, str) else "",
        }
        if payload.get("persist") and modified_fields:
            result = sync_workspace_draft(database_path=_config()["database"], user_id=state.user_id,
                company_id=state.company_id, invoice_id=invoice_id, expected_revision=expected_revision,
                changes={field: changes[field] for field in modified_fields}, expected_draft_events=draft_event_ids,
                session_id=state.session_id, turn_id="ui-draft-" + uuid.uuid4().hex)
            if result["status"] == "ok":
                state.workspace_drafts.pop(key, None)
                await _publish_workspace_page(state, _config()["database"])
            state.workspace_notice = result.get("summary", "草稿暂存失败，请刷新后重试。")
            await _update_workspace_element(state)
            if result["status"] not in {"ok", "local_only"}:
                raise ValueError(state.workspace_notice)


@cl.action_callback("workspace_refresh")
async def workspace_refresh(action: cl.Action) -> None:
    state = _state()
    if state.active_page == "A":
        _refresh_workspace_recent_turns(state, _config())
    await _publish_workspace_page(state, _config()["database"], refresh_tasks=True)


@cl.action_callback("workspace_select_task")
async def workspace_select_task(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    task_id = payload.get("task_id")
    tasks = {str(task["task_id"]): task for task in state.workspace_data.get("tasks", [])}
    task = tasks.get(task_id) if isinstance(task_id, str) else None
    state.workspace_task_id = str(task["task_id"]) if task else None
    available_invoices = {str(invoice["invoice_id"]) for invoice in state.workspace_data.get("invoices", [])}
    requested_invoice = payload.get("invoice_id")
    target_ids = task.get("target_invoice_ids", []) if task else []
    if isinstance(requested_invoice, str) and requested_invoice in target_ids and requested_invoice in available_invoices:
        state.workspace_invoice_id = requested_invoice
    else:
        state.workspace_invoice_id = next((invoice_id for invoice_id in task.get("pending_invoice_ids", []) if invoice_id in available_invoices), None) if task else None
        if state.workspace_invoice_id is None and task:
            state.workspace_invoice_id = next((invoice_id for invoice_id in target_ids if invoice_id in available_invoices), None)
    state.active_page = "A"
    await _update_workspace_element(state)


@cl.action_callback("workspace_select_invoice")
async def workspace_select_invoice(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    invoice_id = payload.get("invoice_id")
    allowed = {str(invoice["invoice_id"]) for invoice in state.workspace_data.get("invoices", [])}
    state.workspace_invoice_id = invoice_id if isinstance(invoice_id, str) and invoice_id in allowed else None
    state.context_invoice_id = state.workspace_invoice_id
    state.context_invoice_ids = allowed
    state.active_page = "C"
    await _update_workspace_element(state)


@cl.action_callback("workspace_open_invoice")
async def workspace_open_invoice(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    task_id = payload.get("task_id")
    invoice_id = payload.get("invoice_id")
    allowed_tasks = {str(task["task_id"]) for task in state.workspace_data.get("tasks", [])}
    allowed_invoices = {str(invoice["invoice_id"]) for invoice in state.workspace_data.get("invoices", [])}
    task = next((item for item in state.workspace_data.get("tasks", []) if item["task_id"] == task_id), None)
    valid_invoice = isinstance(invoice_id, str) and invoice_id in allowed_invoices
    if task_id is not None:
        valid_invoice = valid_invoice and task is not None and invoice_id in task.get("target_invoice_ids", [])
    state.workspace_task_id = str(task["task_id"]) if task else None
    state.workspace_invoice_id = invoice_id if valid_invoice else None
    state.context_invoice_id = state.workspace_invoice_id
    state.context_invoice_ids = allowed_invoices
    state.active_page = "C"
    await _update_workspace_element(state)


@cl.action_callback("workspace_create_task")
async def workspace_create_task(action: cl.Action) -> None:
    state = _state()
    config = _config()
    result: dict[str, Any] | None = None
    try:
        if state.context_month is None:
            raise ValueError("先选择期间，再创建办件。")
        if state.context_month != "2026-09":
            raise ValueError("用途补全办件仅支持 2026-09。")
        with TaxDatabase(config["database"]) as database:
            service = TaxService(database)
            service.require_access(user_id=state.user_id, company_id=state.company_id, modify=True)
            result = TaskManager(database, service).create_task(
                user_id=state.user_id,
                company_id=state.company_id,
                month=state.context_month,
                scope="all_missing",
            )
    except (TaxHarnessError, ValueError) as error:
        state.workspace_notice = str(error)
    notice = state.workspace_notice
    await _publish_workspace_page(state, config["database"])
    if result is not None:
        task = result.get("task") or {}
        if result.get("status") in {"created", "existing"}:
            state.workspace_task_id = str(task.get("task_id") or "") or None
        state.workspace_notice = result.get("summary") or (
            "已创建办件。" if result.get("status") == "created" else f"办件状态：{result.get('status', '未知')}。"
        )
    else:
        state.workspace_notice = notice
    await _update_welcome_panel(state, config["database"])


@cl.action_callback("workspace_cancel_task")
async def workspace_cancel_task(action: cl.Action) -> None:
    state = _state()
    config = _config()
    payload = action.payload if isinstance(action.payload, dict) else {}
    task_id = payload.get("task_id")
    allowed = {str(task["task_id"]) for task in state.workspace_data.get("tasks", [])}
    result: dict[str, Any] | None = None
    if not isinstance(task_id, str) or task_id not in allowed:
        state.workspace_notice = "请从当前办件列表重新选择任务。"
    else:
        try:
            with TaxDatabase(config["database"]) as database:
                service = TaxService(database)
                service.require_access(user_id=state.user_id, company_id=state.company_id, modify=True)
                result = TaskManager(database, service).cancel_task(
                    task_id=task_id,
                    user_id=state.user_id,
                    company_id=state.company_id,
                )
        except TaxHarnessError as error:
            state.workspace_notice = str(error)
    notice = state.workspace_notice
    await _publish_workspace_page(state, config["database"])
    if result is not None:
        state.workspace_notice = result.get("summary") or (
            "办件已取消。" if result.get("status") == "cancelled" else f"办件未取消：{result.get('status', '未知状态')}。"
        )
    else:
        state.workspace_notice = notice
    await _update_welcome_panel(state, config["database"])


@cl.action_callback("workspace_save_invoice")
async def workspace_save_invoice(action: cl.Action) -> None:
    state = _state()
    config = _config()
    payload = action.payload if isinstance(action.payload, dict) else {}
    invoice_id = payload.get("invoice_id")
    expected_revision = payload.get("expected_revision")
    idempotency_key = payload.get("idempotency_key")
    changes = payload.get("changes")
    visible = {str(invoice["invoice_id"]): invoice for invoice in state.workspace_data.get("invoices", [])}
    result: dict[str, Any] | None = None
    clear_draft = False
    ledger_warning = False
    if state.context_month is None or not isinstance(invoice_id, str) or invoice_id not in visible:
        state.workspace_notice = "请先选择当前企业和期间内的发票。"
    elif type(expected_revision) is not int or expected_revision != visible[invoice_id]["revision"]:
        state.workspace_notice = "发票版本已变化，请刷新后核对最新内容。"
    elif (
        not isinstance(changes, dict)
        or not changes
        or set(changes) - {"purpose", "remarks"}
        or any(not isinstance(value, str) for value in changes.values())
    ):
        state.workspace_notice = "仅可填写用途或备注后保存。"
    else:
        try:
            with TaxDatabase(config["database"]) as database:
                service = TaxService(database)
                service.require_access(user_id=state.user_id, company_id=state.company_id, modify=True)
                if not isinstance(idempotency_key, str) or not service._valid_idempotency_key(
                    idempotency_key, state.user_id, state.company_id, invoice_id
                ):
                    state.workspace_notice = "保存请求编号无效，请重新点击保存。"
                else:
                    draft_key = _workspace_draft_key(state, invoice_id)
                    if draft_key:
                        state.workspace_drafts[draft_key] = {
                            "invoice_id": invoice_id,
                            "expected_revision": expected_revision,
                            "purpose": changes.get("purpose", visible[invoice_id].get("purpose") or ""),
                            "remarks": changes.get("remarks", visible[invoice_id].get("remarks") or ""),
                            "idempotency_key": idempotency_key,
                        }
                    result = TaskManager(database, service).save_invoice(
                        user_id=state.user_id,
                        company_id=state.company_id,
                        invoice_id=invoice_id,
                        month=state.context_month,
                        changes=changes,
                        expected_revision=expected_revision,
                        idempotency_key=idempotency_key,
                    )
                    if result.get("status") == "committed" or (
                        result.get("status") == "duplicate" and result.get("original_status") == "committed"
                    ):
                        source = result.get("result", {}) if result.get("status") == "duplicate" else result
                        before, after = source.get("before", {}), source.get("after", {})
                        saved_fields = [field for field in ("purpose", "remarks") if before.get(field) != after.get(field)]
                        operation = database.connection.execute(
                            """SELECT operation_id FROM operations WHERE user_id=? AND company_id=?
                               AND object_type='invoice' AND object_id=? AND idempotency_key=? AND status='committed'""",
                            (state.user_id, state.company_id, invoice_id, idempotency_key),
                        ).fetchone()
                        if saved_fields and operation is None:
                            ledger_warning = True
                        elif saved_fields:
                            try:
                                ledger = TaskLedger(database)
                                already_recorded = any(
                                    event["kind"] == "invoice_saved"
                                    and event["payload"].get("source_operation_id") == operation["operation_id"]
                                    for event in ledger.events(user_id=state.user_id, company_id=state.company_id)
                                )
                                if not already_recorded:
                                    ledger.invoice_saved(
                                        user_id=state.user_id,
                                        company_id=state.company_id,
                                        session_id=state.session_id,
                                        turn_id="ui-save-" + uuid.uuid4().hex,
                                        invoice_id=invoice_id,
                                        fields=saved_fields,
                                        task_id=_workspace_task_for_invoice(state, invoice_id, result.get("task")),
                                        source_operation_id=operation["operation_id"],
                                    )
                            except sqlite3.Error:
                                ledger_warning = True
                    clear_draft = result.get("status") == "no_change" or (
                        result.get("status") == "committed"
                        or (result.get("status") == "duplicate" and result.get("original_status") == "committed")
                    )
        except TaxHarnessError as error:
            state.workspace_notice = str(error)
    if clear_draft:
        key = _workspace_draft_key(state, invoice_id)
        if key:
            state.workspace_drafts.pop(key, None)
    notice = state.workspace_notice
    await _publish_workspace_page(state, config["database"])
    if result is not None:
        status = result.get("status")
        if status in {"committed", "duplicate"} and (status != "duplicate" or result.get("original_status") == "committed"):
            if ledger_warning:
                state.workspace_notice = "发票已保存，但任务变化未记录，请刷新办件。"
            elif result.get("task_refresh_status") not in {"ok", "no_active_task"}:
                state.workspace_notice = "票据已保存，但办件进度未刷新，请单独刷新办件。"
            else:
                state.workspace_notice = "已保存并回读最新发票资料。"
        elif status == "no_change":
            state.workspace_notice = result.get("summary") or "内容没有变化，无需保存。"
        else:
            state.workspace_notice = result.get("summary") or f"保存未完成：{status or '未知状态'}。"
    else:
        state.workspace_notice = notice
    await _update_welcome_panel(state, config["database"])


@cl.action_callback("workspace_undo_invoice")
async def workspace_undo_invoice(action: cl.Action) -> None:
    state = _state()
    config = _config()
    payload = action.payload if isinstance(action.payload, dict) else {}
    invoice_id = payload.get("invoice_id")
    field = payload.get("field")
    visible = {str(invoice["invoice_id"]): invoice for invoice in state.workspace_data.get("invoices", [])}
    result: dict[str, Any] | None = None
    if state.context_month is None or not isinstance(invoice_id, str) or invoice_id not in visible:
        state.workspace_notice = "请先选择当前企业和期间内的发票。"
    elif (
        field not in {"purpose", "remarks"}
        or field not in visible[invoice_id].get("undo_fields", [])
        or not state.workspace_data.get("can_modify")
    ):
        state.workspace_notice = "当前用户没有这项可撤销的已保存修改。"
    else:
        draft_key = _workspace_draft_key(state, invoice_id)
        draft = state.workspace_drafts.get(draft_key, {}) if draft_key else {}
        if draft and any(draft.get(key, "") != str(visible[invoice_id].get(key) or "") for key in ("purpose", "remarks")):
            state.workspace_notice = "请先保存或清除未保存的编辑，再撤销已保存内容。"
        else:
            try:
                with TaxDatabase(config["database"]) as database:
                    result = undo_invoice_change(
                        database,
                        user_id=state.user_id,
                        company_id=state.company_id,
                        invoice_id=invoice_id,
                        field=field,
                        session_id=state.session_id,
                        turn_id="ui-undo-" + uuid.uuid4().hex,
                    )
            except TaxHarnessError as error:
                state.workspace_notice = str(error)
    if result and result.get("undone_fields"):
        draft_key = _workspace_draft_key(state, invoice_id)
        if draft_key:
            state.workspace_drafts.pop(draft_key, None)
    notice = state.workspace_notice
    await _publish_workspace_page(state, config["database"], refresh_tasks=True)
    if result is not None:
        if result.get("undone_fields"):
            field_labels = {"purpose": "用途", "remarks": "备注"}
            state.workspace_notice = f"已撤销{field_labels[field]}，发票和办件进度已刷新。"
        else:
            state.workspace_notice = result.get("summary") or f"撤销未完成：{result.get('status', '未知状态')}。"
    else:
        state.workspace_notice = notice
    await _update_welcome_panel(state, config["database"])


@cl.action_callback("show_context")
async def show_context(action: cl.Action) -> None:
    await _publish_context(_state())


@cl.action_callback("set_context_month")
async def set_context_month(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    month = payload.get("month")
    if month in (None, ""):
        state.context_month = None
        state.context_invoice_id = None
        state.workspace_task_id = None
        state.workspace_invoice_id = None
        await _publish_context(state)
        await _publish_workspace_page(state, _config()["database"])
        return
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        state.context_month = None
        state.context_invoice_id = None
        state.workspace_task_id = None
        state.workspace_invoice_id = None
        await _publish_context(state, error="月份必须是 YYYY-MM。")
        await _publish_workspace_page(state, _config()["database"])
        state.workspace_notice = "月份必须是 YYYY-MM。"
        await _update_welcome_panel(state, _config()["database"])
        return
    if month != state.context_month:
        state.context_invoice_id = None
        state.workspace_task_id = None
        state.workspace_invoice_id = None
    state.context_month = month
    await _publish_context(state)
    await _publish_workspace_page(state, _config()["database"])


@cl.action_callback("select_context_invoice")
async def select_context_invoice(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    invoice_id = payload.get("invoice_id")
    if invoice_id in (None, ""):
        state.context_invoice_id = None
        return
    if not isinstance(invoice_id, str) or invoice_id not in state.context_invoice_ids:
        state.context_invoice_id = None
        await _publish_context(state, error="该发票不属于当前企业和期间，请重新选择。")
        return
    state.context_invoice_id = invoice_id


@cl.action_callback("show_more_history")
async def show_more_history(action: cl.Action) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    offset = payload.get("offset", 0)
    if type(offset) is not int or offset < 0:
        offset = 0
    await _publish_history(_state(), offset=min(offset, 1_000_000))


@cl.action_callback("conversation_history_view")
async def view_history(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        await cl.ErrorMessage(content="无法识别该历史对话。请从当前用户的历史列表重新选择。").send()
        return
    try:
        session, turns = _load_history_conversation(state, session_id, _config())
    except HistoryUnavailable as error:
        await cl.ErrorMessage(content=str(error)).send()
        return
    await _render_history_transcript(state, session, turns)


@cl.action_callback("toggle_reply")
async def toggle_reply(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    message_id = payload.get("message_id")
    if isinstance(message_id, str) and message_id in state.reply_messages:
        record = state.reply_messages[message_id]
        record["expanded"] = not bool(record["expanded"])
        message: cl.Message = record["message"]
        card: cl.CustomElement = record["card"]
        full = str(record["text"])
        message.content = full if record["expanded"] else _reply_preview(full)
        card.props["expanded"] = bool(record["expanded"])
        await message.update()
        return

    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if not isinstance(session_id, str) or not isinstance(turn_id, str):
        await cl.ErrorMessage(content="无法识别这条回复。请从历史对话中重新打开。").send()
        return
    try:
        session, turns = _load_history_conversation(state, session_id, _config())
    except HistoryUnavailable as error:
        await cl.ErrorMessage(content=str(error)).send()
        return
    if not any(str(turn.get("turn_id")) == turn_id for turn in turns):
        await cl.ErrorMessage(content="这条历史回复已不可用，请刷新历史列表。").send()
        return
    turn_key = f"{session_id}:{turn_id}"
    toggle = payload.get("toggle", "reply")
    if toggle not in {"turn", "reply"}:
        await cl.ErrorMessage(content="无法识别这条回合操作，请重新打开历史对话。").send()
        return
    expanded_set = state.expanded_history_turns if toggle == "turn" else state.expanded_history_replies
    if turn_key in expanded_set:
        expanded_set.remove(turn_key)
    else:
        expanded_set.add(turn_key)
    await _render_history_transcript(state, session, turns)


@cl.action_callback("conversation_history_continue")
async def continue_history(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        await cl.ErrorMessage(content="无法识别该历史对话。请从当前用户的历史列表重新选择。").send()
        return
    try:
        session, _turns = _load_history_conversation(state, session_id, _config())
    except HistoryUnavailable as error:
        await cl.ErrorMessage(content=str(error)).send()
        return
    if session_id == state.session_id:
        await cl.Message(
            content="当前已在这个 Agent 会话中，下一条消息会继续原上下文。",
            actions=_history_actions(session_id),
        ).send()
        return
    await _wait_active(state)
    state.generation += 1
    state.stop_requested = False
    state.await_new_thread = True
    company_id = str(session["current_company_id"])
    browser_key = _browser_identity_key()
    _IDENTITY_BY_BROWSER[browser_key] = (state.user_id, company_id, session_id)
    cl.user_session.set("taxharness_selected_user", state.user_id)
    cl.user_session.set("taxharness_selected_company", company_id)
    await cl.Message(content="已保留当前对话，正在打开所选历史会话；不会重放原工具操作。").send()
    await cl.send_window_message({"type": "taxharness_resume_history", "reason": "continue_history"})


@cl.action_callback("dismiss_diagnostic")
async def dismiss_diagnostic(action: cl.Action) -> None:
    state = _state()
    payload = action.payload if isinstance(action.payload, dict) else {}
    step_id = payload.get("step_id")
    if not isinstance(step_id, str):
        return
    step = state.diagnostic_steps.pop(step_id, None)
    if step is not None:
        await step.remove()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    state = _state()
    if state.await_new_thread:
        await cl.ErrorMessage(content="正在打开空白新对话或恢复所选历史；请等页面切换完成后再输入。").send()
        return
    if state.closing:
        await cl.ErrorMessage(content="当前对话正在关闭，请开启新对话。").send()
        return
    if state.worker is not None and not state.worker.done():
        await cl.ErrorMessage(content="上一轮正在停止或办理中，请稍候再输入。").send()
        return
    config = _config()
    if not config["database"].is_file():
        await cl.ErrorMessage(content="业务数据库不存在，请用启动脚本准备独立副本。").send()
        return
    if state.session_id in checkpoint_thread_ids(config["checkpoint_store"]):
        try:
            _load_history_conversation(state, state.session_id, config)
        except HistoryUnavailable as error:
            await cl.ErrorMessage(content=str(error)).send()
            return
    generation = state.generation
    state.stop_requested = False
    try:
        files = _uploaded_files(message)
    except ValueError as error:
        await cl.ErrorMessage(content=str(error)).send()
        return
    text = message.content.strip() or "请读取上传的票据。"
    state.worker = asyncio.create_task(_execute_turn(state, text, generation, files) if files
                                       else _execute_turn(state, text, generation))
    try:
        result = await asyncio.shield(state.worker)
    except asyncio.CancelledError:
        # Chainlit may cancel this callback after Stop; keep the worker alive
        # until its runtime returns and closes safely in _execute_turn().
        try:
            result = await asyncio.shield(state.worker)
        except Exception:
            return
    except Exception as error:
        await cl.ErrorMessage(content=f"本轮处理失败：{error}").send()
        return
    finally:
        state.worker = None
    if generation != state.generation:
        return
    previous_company_id = state.company_id
    state.company_id = result.get("current_company_id") or state.company_id
    if state.company_id != previous_company_id:
        state.context_month = None
        state.context_invoice_id = None
        state.context_invoice_ids.clear()
        state.history_panel_session_id = None
        if state.is_narrow:
            await _close_context()
        else:
            await _publish_context(state)
    elif state.context_month and not state.is_narrow and not state.history_panel_session_id:
        # The turn may have saved an invoice or changed a rule/task. Re-read
        # current data while preserving the selected invoice in this session.
        await _publish_context(state)
    _IDENTITY_BY_BROWSER[_browser_identity_key()] = (state.user_id, state.company_id, None)
    await _publish_settings(state)
    if result.get("status") == "ok":
        reply = str(result.get("reply") or "本轮已完成。")
        long_reply = len(reply) > _REPLY_PREVIEW_CHARS
        card = cl.CustomElement(
            name="ReplyCard",
            props={"text": reply, "long": long_reply, "expanded": not long_reply, "copy_label": "复制回复"},
        )
        answer = cl.Message(
            content=_reply_preview(reply) if long_reply else reply,
            actions=[cl.Action(name="show_history", label="对话档案", tooltip="查看、回看或继续已保存的对话。", payload={})],
            elements=[card],
        )
        card.props["message_id"] = answer.id
        await answer.send()
        state.reply_messages[answer.id] = {
            "message": answer,
            "card": card,
            "text": reply,
            "expanded": not long_reply,
        }
    elif result.get("status") == "cancelled":
        await cl.Message(content=result.get("summary") or "本轮已停止。").send()
    else:
        await cl.ErrorMessage(content=result.get("summary") or "本轮未完成。").send()
    await _send_diagnostic(state, result)
    config = _config()
    _refresh_workspace_recent_turns(state, config)
    await _publish_workspace_page(state, config["database"])
    await _publish_history(state)


@cl.on_stop
async def on_stop() -> None:
    state = _state()
    state.stop_requested = True
    if state.runtime is not None and state.worker is not None and not state.worker.done():
        state.runtime.cancel(session_id=state.session_id, user_id=state.user_id)
        await cl.Message(content="已发送停止请求；当前工具返回后会安全结束本轮。").send()


@cl.on_chat_end
async def on_chat_end() -> None:
    state = cl.user_session.get("taxharness_ui_state")
    if isinstance(state, UiState):
        state.closing = True
        state.stop_requested = True
        if state.runtime is not None and state.worker is not None and not state.worker.done():
            state.runtime.cancel(session_id=state.session_id, user_id=state.user_id)

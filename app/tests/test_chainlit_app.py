from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from taxharness.storage import initialize_database, TaxDatabase


SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


@pytest.fixture
def app_module():
    import scripts.chainlit_app as app

    return app


def test_ui_defaults_are_project_relative_and_user_company_options_are_acl_scoped(app_module, tmp_path):
    database_path = tmp_path / "business.sqlite3"
    initialize_database(database_path, SOURCE_XLSX, RULES_DOC)
    options = app_module._company_options("U002", database_path)
    assert list(options.values()) == ["C001"]
    with pytest.raises(Exception):
        app_module._check_access("U002", "C002", database_path)
    config = app_module._config()
    assert "E:\\AI-Chem" not in str(config["database"])


def test_safe_diagnostic_redacts_credentials_and_truncates_large_text(app_module):
    value = app_module._safe({"api_key": "secret", "nested": {"authorization": "token", "text": "x" * 13000}})
    assert value["api_key"] == "[redacted]"
    assert value["nested"]["authorization"] == "[redacted]"
    assert value["nested"]["text"].endswith("[truncated]")


def test_identity_switch_uses_clean_chainlit_thread_path():
    script = Path(__file__).resolve().parents[1] / "public" / "chainlit_ui.js"
    config = Path(__file__).resolve().parents[1] / ".chainlit" / "config.toml"
    script_text = script.read_text(encoding="utf-8")
    assert "new-chat-button" in script_text
    assert "taxharness_tab_id" in script_text
    assert "taxharness_resume_history" in script_text
    config_text = config.read_text(encoding="utf-8")
    assert "custom_js = \"/public/chainlit_ui.js\"" in config_text
    assert 'custom_css = "/public/chainlit_ui.css"' in config_text
    assert "confirm_new_chat = false" in config_text
    assert 'language = "zh-CN"' in config_text


def test_history_and_reply_elements_are_configured_for_the_real_chat_panel():
    root = Path(__file__).resolve().parents[1]
    history = (root / "public" / "elements" / "ConversationHistory.jsx").read_text(encoding="utf-8")
    context = (root / "public" / "elements" / "ContextPanel.jsx").read_text(encoding="utf-8")
    reply = (root / "public" / "elements" / "ReplyCard.jsx").read_text(encoding="utf-8")
    diagnostics = (root / "public" / "elements" / "DiagnosticControls.jsx").read_text(encoding="utf-8")
    welcome = (root / "public" / "elements" / "WelcomePanel.jsx").read_text(encoding="utf-8")
    css = (root / "public" / "chainlit_ui.css").read_text(encoding="utf-8")
    app = (root / "scripts" / "chainlit_app.py").read_text(encoding="utf-8")
    assert 'name="WelcomePanel"' in app
    assert 'name="ConversationHistory"' in app
    assert 'name="ReplyCard"' in app
    assert 'name="DiagnosticControls"' in app
    assert "搜索当前页" in welcome
    assert 'runAction("conversation_history_view"' in welcome
    assert 'runAction("conversation_history_continue"' in welcome
    assert "navigator.clipboard.writeText(text)" in history
    assert "navigator.clipboard.writeText(text)" in reply
    assert "下方文本框中手动选择并复制" in history
    assert 'runAction("show_context")' in history
    assert 'type="month"' in context
    assert "onSubmit={submitMonth}" in context
    assert 'elements.namedItem("month").value' in context
    assert "defaultValue={month}" in context
    assert "查看资料" in context
    assert 'id="th-invoice-select"' in context
    assert 'role="group" aria-label="资料类型"' in context
    assert 'aria-pressed={section === "invoices"}' in context
    assert 'aria-pressed={section === "rules"}' in context
    assert 'aria-pressed={section === "tasks"}' in context
    assert '@cl.action_callback("select_context_invoice")' in app
    assert '"invoice_id": state.context_invoice_id' in app
    assert "invoice_id not in state.context_invoice_ids" in app
    assert 'RULE_LABELS' in context
    assert "不会改变对话中已确认的月份" in context
    assert '@cl.action_callback("set_context_month")' in app
    assert 'welcome_content = "" if resumed_session else "在下方描述问题，或打开历史对话继续办理。"' in app
    assert '"最近回合已回复" if entry.latest_status == "ok"' in app
    assert "scopeLabel" in welcome and "th-mobile-scope" in welcome
    assert "aria-label={scopeLabel}" in welcome
    assert "aria-hidden={narrow && !historyOpen}" in welcome
    assert "historyTrigger.current?.focus()" in welcome
    assert 'name="ContextPanel"' in app
    assert "toggle_reply" in reply
    assert "dismiss_diagnostic" in diagnostics
    assert "history_has_more" in welcome
    assert "top: 10px; right: 10px; left: 56px" in css
    assert ".th-workspace-mobile-tools { position: fixed; z-index: 40;" in css
    assert ".th-workspace-shell.th-workspace-drawer { position: fixed; z-index: 70;" in css
    assert "visibility: hidden" in css and ".th-workspace-shell.th-workspace-drawer.is-open { visibility: visible;" in css
    assert ".th-context-tabs button {" in css and "font-size: 12px" in css
    assert ".th-rule-row > p, .th-task-row > p" in css and "font-size: 12px" in css
    assert ".th-invoice-amount" in css and "font-size: 13px" in css
    assert ".dark .th-reply-actions .th-quiet-button" in css
    assert ".dark .th-mobile-scope" in css
    assert ".dark .th-context-panel, .dark .th-transcript-panel" in css
    assert ".dark .th-context-month, .dark .th-invoice-select" in css and "color-scheme: light" in css
    assert "div.absolute.bottom-4.left-0.right-0.flex.justify-center:has(svg.lucide-arrow-down)" in css
    assert "div.relative.flex.flex-col.flex-grow.overflow-y-auto { padding-right: 52px; }" in css
    assert "scrollbar-color: #8491a6 #edf0f5" in css
    assert ".dark div.relative.flex.flex-col.flex-grow.overflow-y-auto" in css
    assert "scrollbar-color: #7888bd #202e61" in css
    for selector in ("#message-composer", "#chat-input", "#chat-submit", "#new-chat-button"):
        assert selector in css


def test_successful_history_card_does_not_imply_task_completion(app_module):
    entry = SimpleNamespace(
        session_id="saved-session",
        updated_at="2026-10-28T00:00:00+00:00",
        turn_count=2,
        latest_status="ok",
    )
    card = app_module._history_card(
        entry,
        {"current_company_id": "C001"},
        [{"request_text": "检查F009"}],
    )
    assert card["status"] == "最近回合已回复"


def test_long_reply_preview_keeps_markdown_fence_balanced(app_module):
    short = "**结果**：正常"
    assert app_module._reply_preview(short) == short

    long = "```python\n" + ("x" * 1900) + "\n```"
    preview = app_module._reply_preview(long)
    assert len(preview) < len(long)
    assert preview.count("```") == 2
    assert preview.endswith("```")


@pytest.mark.asyncio
async def test_history_transcript_uses_sidebar_markdown_and_recoverable_reply_cards(app_module, monkeypatch):
    captured: dict[str, object] = {}

    class FakeElement:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def set_elements(elements):
        captured["elements"] = elements

    async def set_title(title):
        captured["title"] = title

    monkeypatch.setattr(app_module.cl, "CustomElement", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module.cl, "Text", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module.cl, "ElementSidebar", SimpleNamespace(set_elements=set_elements, set_title=set_title))
    monkeypatch.setattr(app_module.cl, "Message", lambda **_kwargs: pytest.fail("history view must not replay into the chat pane"))

    state = app_module.UiState("U001", "C001", "fresh-thread")
    turns = [
        {"turn_id": "turn-1", "request_text": "列出本月金额", "output_text": "```python\n" + ("x" * 1900) + "\n```"},
        {"turn_id": "turn-2", "request_text": "谢谢", "output_text": "不客气。"},
    ]
    await app_module._render_history_transcript(
        state,
        {"session_id": "chainlit-old", "current_company_id": "C001"},
        turns,
    )

    elements = captured["elements"]
    assert captured["title"] == "历史对话"
    assert state.history_panel_session_id == "chainlit-old"
    assert len(elements) == 5
    assert elements[0].props["view"] == "transcript"
    assert "列出本月金额" in elements[1].content
    assert elements[2].name == "ReplyCard"
    assert elements[2].props["long"] is True
    assert elements[2].props["turn_toggle"] is True
    assert elements[2].props["turn_expanded"] is False
    assert elements[2].props["history_turn_id"] == "turn-1"
    assert elements[4].props["turn_toggle"] is True
    assert elements[4].props["long"] is False
    assert elements[4].props["history_turn_id"] == "turn-2"


@pytest.mark.asyncio
async def test_continue_renders_saved_turns_in_chat_without_running_agent(app_module, monkeypatch):
    sent = []

    class FakeElement:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMessage:
        next_id = 0

        def __init__(self, **kwargs):
            type(self).next_id += 1
            self.id = f"message-{self.next_id}"
            self.type = kwargs.get("type", "assistant_message")
            self.__dict__.update(kwargs)

        async def send(self):
            sent.append(self)

        async def update(self):
            pass

    monkeypatch.setattr(app_module.cl, "Message", FakeMessage)
    monkeypatch.setattr(app_module.cl, "CustomElement", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module.cl, "Action", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module, "AgentRuntime", lambda **_kwargs: pytest.fail("restoring UI history must not run the Agent"))

    state = app_module.UiState("U001", "C001", "saved-session")
    turns = [
        {"request_text": "查一下发票金额", "output_text": "三张发票。", "status": "ok"},
        {"request_text": "继续核对", "output_text": "长回复\n" + ("内容" * 1000), "status": "ok"},
    ]
    await app_module._render_resumed_conversation(state, turns)

    assert state.session_id == "saved-session"
    assert [message.type for message in sent] == ["user_message", "assistant_message", "user_message", "assistant_message"]
    assert sent[0].content == "查一下发票金额"
    assert sent[1].content == "三张发票。"
    assert sent[3].elements[0].props["long"] is True
    assert sent[3].elements[0].props["message_id"] == sent[3].id
    assert len(state.reply_messages) == 2


@pytest.mark.asyncio
async def test_welcome_panel_tracks_company_changed_by_a_completed_turn(app_module, tmp_path, monkeypatch):
    database_path = tmp_path / "business.sqlite3"
    initialize_database(database_path, SOURCE_XLSX, RULES_DOC)

    class FakeWelcomePanel:
        props = {"user_id": "U001", "user_label": "U001", "company_id": "C001", "company_label": "C001"}
        updates = 0

        async def update(self):
            self.updates += 1

    class FakeSettings:
        def __init__(self, _widgets):
            pass

        async def send(self):
            pass

    monkeypatch.setattr(app_module, "_config", lambda: {"database": database_path})
    monkeypatch.setattr(app_module.cl, "ChatSettings", FakeSettings)
    state = app_module.UiState("U001", "C001", "ui-session")
    state.welcome_panel = FakeWelcomePanel()

    # Simulate a completed turn that changed the active enterprise.
    state.company_id = "C002"
    await app_module._publish_settings(state)

    assert state.welcome_panel.props["company_id"] == "C002"
    assert "C002" in state.welcome_panel.props["company_label"]
    assert state.welcome_panel.props["user_id"] == "U001"
    assert state.welcome_panel.updates == 1


def test_read_period_context_uses_authorized_company_rules_and_selected_invoice_data(tmp_path):
    from taxharness.context_presentation import read_period_context
    from taxharness.errors import PermissionDeniedError

    database_path = tmp_path / "business.sqlite3"
    initialize_database(database_path, SOURCE_XLSX, RULES_DOC)

    empty = read_period_context(
        database_path=database_path,
        user_id="U001",
        company_id="C001",
        month=None,
    )
    assert empty["month"] is None
    assert empty["invoices"] == []

    september = read_period_context(
        database_path=database_path,
        user_id="U001",
        company_id="C001",
        month="2026-09",
    )
    assert september["company_id"] == "C001"
    assert {invoice["invoice_id"] for invoice in september["invoices"]} >= {"F003", "F009"}
    purpose_rule = next(rule for rule in september["rules"] if rule["kind"] == "invoice_purpose")
    assert (purpose_rule["rule_id"], purpose_rule["version"], purpose_rule["status"]) == ("R002", "2.0", "active")

    restricted = read_period_context(
        database_path=database_path,
        user_id="U002",
        company_id="C001",
        month="2026-09",
    )
    assert restricted["company_id"] == "C001"
    assert restricted["tasks"] == []
    with pytest.raises(PermissionDeniedError):
        read_period_context(
            database_path=database_path,
            user_id="U002",
            company_id="C002",
            month="2026-09",
        )


@pytest.mark.asyncio
async def test_invoice_selection_preserves_focus_and_rejects_out_of_scope_values(app_module, monkeypatch):
    state = app_module.UiState("U001", "C001", "session")
    state.context_month = "2026-09"
    state.context_invoice_ids = {"F001", "F009"}
    calls = []

    async def publish(_state, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(app_module, "_state", lambda: state)
    monkeypatch.setattr(app_module, "_publish_context", publish)
    await app_module.select_context_invoice(SimpleNamespace(payload={"invoice_id": "F009"}))
    assert state.context_invoice_id == "F009"
    assert calls == []
    await app_module.select_context_invoice(SimpleNamespace(payload={"invoice_id": "other-company"}))
    assert state.context_invoice_id is None
    assert calls and "不属于当前企业" in calls[-1]["error"]


@pytest.mark.asyncio
async def test_successful_turn_refreshes_selected_invoice_from_current_business_data(app_module, tmp_path, monkeypatch):
    database_path = tmp_path / "business.sqlite3"
    initialize_database(database_path, SOURCE_XLSX, RULES_DOC)
    state = app_module.UiState("U001", "C001", "new-session")
    state.context_month = "2026-09"
    state.context_invoice_id = "F009"
    captured = []

    class FakeElement:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMessage:
        def __init__(self, **kwargs):
            self.id = "answer-1"
            self.__dict__.update(kwargs)

        async def send(self):
            pass

    async def set_elements(elements):
        captured.append(elements[0])

    async def noop(*_args, **_kwargs):
        pass

    async def execute(_state, _message, _generation):
        with TaxDatabase(database_path) as database:
            saved = app_module.TaxService(database).update_invoice(
                user_id="U001", company_id="C001", invoice_id="F009",
                changes={"purpose": "项目设备维修"}, expected_revision=1,
                idempotency_key="U001:C001:invoice:F009:refresh-test",
            )
        assert saved["status"] == "committed"
        return {"status": "ok", "reply": "已保存", "current_company_id": "C001"}

    monkeypatch.setattr(app_module, "_state", lambda: state)
    monkeypatch.setattr(app_module, "_config", lambda: {"database": database_path, "checkpoint_store": tmp_path / "checkpoints.sqlite3"})
    monkeypatch.setattr(app_module, "_browser_identity_key", lambda: "test-browser")
    monkeypatch.setattr(app_module, "_execute_turn", execute)
    monkeypatch.setattr(app_module, "_publish_settings", noop)
    monkeypatch.setattr(app_module, "_publish_history", noop)
    monkeypatch.setattr(app_module, "_send_diagnostic", noop)
    monkeypatch.setattr(app_module.cl, "ElementSidebar", SimpleNamespace(set_elements=set_elements, set_title=noop))
    monkeypatch.setattr(app_module.cl, "CustomElement", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module.cl, "Action", lambda **kwargs: FakeElement(**kwargs))
    monkeypatch.setattr(app_module.cl, "Message", FakeMessage)

    await app_module._publish_context(state)
    assert next(i for i in captured[-1].props["invoices"] if i["invoice_id"] == "F009")["purpose"] is None
    await app_module.on_message(SimpleNamespace(content="保存 F009 用途"))
    current = captured[-1].props
    assert current["invoice_id"] == "F009"
    assert next(i for i in current["invoices"] if i["invoice_id"] == "F009")["purpose"] == "项目设备维修"


@pytest.mark.asyncio
async def test_context_database_error_is_reported_without_claiming_no_data(app_module, tmp_path, monkeypatch):
    import sqlite3

    captured = []

    class FakeElement:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def set_elements(elements):
        captured.extend(elements)

    async def noop(*_args, **_kwargs):
        pass

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    state = app_module.UiState("U001", "C001", "new-session")
    state.context_month = "2026-09"
    monkeypatch.setattr(app_module, "_config", lambda: {"database": tmp_path / "business.sqlite3"})
    monkeypatch.setattr(app_module, "_welcome_identity_props", locked)
    monkeypatch.setattr(app_module.cl, "ElementSidebar", SimpleNamespace(set_elements=set_elements, set_title=noop))
    monkeypatch.setattr(app_module.cl, "CustomElement", lambda **kwargs: FakeElement(**kwargs))
    await app_module._publish_context(state)
    assert captured[0].props["error"] == "资料暂时无法读取，请稍后重试。"
    assert captured[0].props["invoices"] == []


def test_browser_identity_key_uses_only_named_tab_cookie(app_module, monkeypatch):
    session = SimpleNamespace(environ={"HTTP_COOKIE": "other=one; taxharness_tab_id=tab-a"}, id="session-a")
    monkeypatch.setattr(app_module, "chainlit_context", SimpleNamespace(session=session))
    first = app_module._browser_identity_key()
    session.environ["HTTP_COOKIE"] = "other=two; taxharness_tab_id=tab-a"
    assert app_module._browser_identity_key() == first
    session.environ["HTTP_COOKIE"] = "taxharness_tab_id=tab-b"
    assert app_module._browser_identity_key() != first


@pytest.mark.asyncio
async def test_cancel_waits_for_worker_before_runtime_close(app_module, monkeypatch):
    release = threading.Event()
    events: list[str] = []

    class FakeRuntime:
        def __init__(self, **_kwargs):
            events.append("open")

        def run_turn(self, **_kwargs):
            release.wait(2)
            events.append("run_return")
            return {"status": "cancelled", "current_company_id": "C001", "summary": "已停止"}

        def cancel(self, **_kwargs):
            events.append("cancel")
            release.set()

        def close(self):
            events.append("close")

    monkeypatch.setattr(app_module, "AgentRuntime", FakeRuntime)
    state = app_module.UiState(user_id="U001", company_id="C001", session_id="ui-test")
    worker = asyncio.create_task(app_module._execute_turn(state, "停止测试", 0))
    await asyncio.sleep(0.05)
    assert state.runtime is not None
    state.runtime.cancel(session_id=state.session_id, user_id=state.user_id)
    result = await worker
    assert result["status"] == "cancelled"
    assert events.index("run_return") < events.index("close")

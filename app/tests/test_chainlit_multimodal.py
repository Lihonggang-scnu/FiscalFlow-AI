from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from taxharness.agent_store import AgentStore
from taxharness.conversation_attachments import ConversationAttachments
from taxharness.enterprise import multimodal
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT.parent / "datasets/china_invoice_guard/07_行程发票.pdf"


def test_uploaded_pdf_text_reaches_agent_and_history_is_scoped(tmp_path, monkeypatch):
    import scripts.chainlit_app as app

    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    with TaxDatabase(business) as db:
        task = TaskManager(db).create_task(user_id="U001", company_id="C001", month="2026-09")
    agent = AgentStore(tmp_path / "agent.sqlite3")
    seen = {}

    def extract(document, **kwargs):
        seen["text"] = kwargs["user_text"]
        assert document["page_count"] == 1 and document["images"]
        return {"filename": document["filename"], "pdf_text": document["text"],
                "documents": [{"invoice_number": "24312000000055556666", "amount": "24.00"}]}

    class Runtime:
        agent_store = agent
        budget_ledger_path = str(tmp_path / "budget.sqlite3")

        def run_turn(self, **kwargs):
            seen["observations"] = kwargs["attachment_observations"]
            return {"status": "ok", "turn_id": "turn-1"}

    monkeypatch.setattr(multimodal, "extract_document", extract)
    state = app.UiState("U001", "C001", "chat-1")
    result = app._run_ui_turn(Runtime(), state, "这是客户拜访，先不保存。",
                              {"database": business, "agent_store": agent.path, "budget_usd": 2}, [(SAMPLE.name, SAMPLE)])
    assert result["status"] == "ok" and "客户拜访" in seen["text"]
    assert seen["observations"][0]["documents"][0]["amount"] == "24.00"
    store = ConversationAttachments(agent.path)
    item = store.history(session_id="chat-1", user_id="U001")["turn-1"][0]
    assert item["filename"] == SAMPLE.name and item["path"].read_bytes() == SAMPLE.read_bytes()
    assert store.history(session_id="chat-1", user_id="U002") == {}
    task_id = task["task"]["task_id"]
    assert store.observations(session_id="chat-2", user_id="U001", company_id="C001", task_id=task_id)
    assert not store.observations(session_id="chat-2", user_id="U002", company_id="C001", task_id=task_id)
    assert not store.observations(session_id="chat-2", user_id="U001", company_id="C002", task_id=task_id)
    assert not store.observations(session_id="chat-1", user_id="U001", company_id="C001", task_id="different-task")


def test_registered_uploads_only_and_stop_after_recognition(tmp_path, monkeypatch):
    import scripts.chainlit_app as app

    business = tmp_path / "business.sqlite3"
    initialize_database(business, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    agent = AgentStore(tmp_path / "agent.sqlite3")
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    upload = upload_root / "uploaded.pdf"
    upload.write_bytes(SAMPLE.read_bytes())
    session = SimpleNamespace(files_dir=upload_root, files={"file-1": {"path": upload, "name": SAMPLE.name}})
    monkeypatch.setattr(app, "chainlit_context", SimpleNamespace(session=session))
    files = app._uploaded_files(SimpleNamespace(elements=[SimpleNamespace(id="file-1")]))
    assert files == [(SAMPLE.name, upload)]
    with pytest.raises(ValueError, match="失效"):
        app._uploaded_files(SimpleNamespace(elements=[SimpleNamespace(id="unknown")]))

    state = app.UiState("U001", "C001", "chat-1")

    def extract(document, **kwargs):
        state.stop_requested = True
        return {"filename": document["filename"], "pdf_text": "", "documents": []}

    class Runtime:
        agent_store = agent
        budget_ledger_path = str(tmp_path / "budget.sqlite3")

        def run_turn(self, **kwargs):
            pytest.fail("停止后不能继续调用办理模型")

    monkeypatch.setattr(multimodal, "extract_document", extract)
    result = app._run_ui_turn(Runtime(), state, "请查看", {"database": business, "agent_store": agent.path, "budget_usd": 2}, files)
    assert result["status"] == "cancelled"

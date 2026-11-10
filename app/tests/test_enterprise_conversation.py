from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.enterprise.agent import generate_reply_drafts
from taxharness.enterprise.auth import SESSION_COOKIE, assign_membership, create_first_admin, create_user
from taxharness.enterprise.conversation import EnterpriseAgentRuntime
from taxharness.enterprise.imports import ImportManager
from taxharness.enterprise.service import EnterpriseService
from taxharness.enterprise.storage import EnterpriseDatabase, issue_session
from taxharness.enterprise.web import create_app
from taxharness.agent_store import AgentStore


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def _prepare(path):
    with EnterpriseDatabase(path) as db:
        admin = create_first_admin(db, username="admin@example.test", display_name="管理员", password="correct horse battery staple")
        service = EnterpriseService(db)
        company = service.create_company(actor=admin, name="秋日科技", currency="CNY", require_independent_review=True)
        accountant = create_user(db, username="accountant@example.test", display_name="会计", password="correct horse battery staple")
        reviewer = create_user(db, username="reviewer@example.test", display_name="复核", password="correct horse battery staple")
        requester = create_user(db, username="requester@example.test", display_name="经办人", password="correct horse battery staple")
        for person, role in ((accountant, "accountant"), (reviewer, "reviewer"), (requester, "requester")):
            assign_membership(db, user_id=person["user_id"], company_id=company["company_id"], role=role)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["invoice_id", "issue_date", "amount", "currency", "seller_name", "project_name", "purpose", "remarks"])
        writer.writerow(["O-01", "2026-10-08", "240.00", "CNY", "文具商", "办公材料", "", ""])
        importer = ImportManager(db)
        preview = importer.preview(user=accountant, company_id=company["company_id"], source_system="ERP", filename="october.csv", content=output.getvalue().encode("utf-8-sig"), mapping={})
        importer.commit(user=accountant, preview_id=preview["preview_id"])
        work = service.list_missing(user=accountant, company_id=company["company_id"], period="2026-10")["work_item"]
        invoice_id = service.work_item(user=accountant, task_id=work["task_id"])["invoices"]["items"][0]["invoice_id"]
        request = service.create_request(user=accountant, task_id=work["task_id"], respondent_user_id=requester["user_id"], invoice_ids=[invoice_id], message="请说明这张票的用途。")
        response = service.submit_reply(user=requester, request_id=request["request_id"], reply_text="这张是部门培训材料。")
        extracted = generate_reply_drafts(db, user=accountant, response_id=response["response_id"], model=lambda **_: {"items": [
            {"invoice_id": invoice_id, "purpose": "部门培训材料", "evidence_quote": "部门培训材料", "needs_clarification": False},
        ]})
        return company, accountant, reviewer, requester, work, invoice_id, extracted["drafts"][0]


def test_enterprise_chat_and_workbench_share_draft_and_review_state(tmp_path):
    database = tmp_path / "enterprise.sqlite3"
    company, accountant, reviewer, requester, work, invoice_id, draft = _prepare(database)
    runtime = EnterpriseAgentRuntime(database_path=database, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        edit = ToolFake(responses=[
            _call("enterprise_inspect_work_item", {"task_id": work["task_id"]}, "inspect"),
            _call("enterprise_correct_draft", {"draft_id": draft["draft_id"], "expected_draft_revision": 1, "purpose": "新员工培训材料"}, "correct"),
            AIMessage(content="草稿已更正，尚未保存发票。"),
        ])
        result = runtime.run_turn(session_id="accountant", user_id=accountant["user_id"], company_id=company["company_id"], message="把这条草稿更正为新员工培训材料，先不要保存。", model=edit)
        assert result["status"] == "ok"
        tool_results = [json.loads(item["data"]["content"]) for item in result["messages"] if item["type"] == "tool"]
        assert all(item["status"] == "ok" for item in tool_results)
        with EnterpriseDatabase(database) as db:
            detail = EnterpriseService(db).work_item(user=accountant, task_id=work["task_id"])
            updated = next(item for item in detail["drafts"] if item["draft_id"] == draft["draft_id"])
            assert updated["proposed_purpose"] == "新员工培训材料" and updated["draft_revision"] == 2
            assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] is None

        readonly = ToolFake(responses=[
            _call("enterprise_correct_draft", {"draft_id": draft["draft_id"], "expected_draft_revision": 2, "purpose": "越权更正"}, "denied"),
            AIMessage(content="当前身份不能修改草稿。"),
        ])
        denied = runtime.run_turn(session_id="reviewer", user_id=reviewer["user_id"], company_id=company["company_id"], message="把草稿改掉。", model=readonly)
        tool_result = next(json.loads(item["data"]["content"]) for item in denied["messages"] if item["type"] == "tool")
        assert tool_result["status"] == "permission_denied"
        requester_result = runtime.run_turn(session_id="requester", user_id=requester["user_id"], company_id=company["company_id"], message="确认这个草稿。", model=readonly)
        assert requester_result["status"] == "permission_denied"

        confirm = ToolFake(responses=[
            _call("enterprise_inspect_work_item", {"task_id": work["task_id"]}, "inspect-again"),
            _call("enterprise_confirm_drafts", {"drafts": [{"draft_id": draft["draft_id"], "draft_revision": 2}]}, "confirm"),
            AIMessage(content="用途已保存，等待复核。"),
        ])
        result = runtime.run_turn(session_id="new-chat", user_id=accountant["user_id"], company_id=company["company_id"], message="保存刚才更正的草稿。", model=confirm)
        assert result["status"] == "ok"
        with EnterpriseDatabase(database) as db:
            service = EnterpriseService(db)
            detail = service.work_item(user=accountant, task_id=work["task_id"])
            assert detail["invoices"]["items"][0]["purpose"] == "新员工培训材料"
            assert detail["invoices"]["items"][0]["task_status"] == "awaiting_review"
            assert detail["task"]["status"] == "needs_review"
            assert service.review_invoice(user=reviewer, task_id=work["task_id"], invoice_id=invoice_id, action="approve")["status"] == "ok"
    finally:
        runtime.close()


def test_enterprise_chat_http_keeps_history_and_restricts_other_users(tmp_path):
    database = tmp_path / "enterprise.sqlite3"
    company, accountant, reviewer, _, work, _, draft = _prepare(database)
    with EnterpriseDatabase(database) as db:
        token, csrf = issue_session(db, accountant["user_id"])
        reviewer_token, _ = issue_session(db, reviewer["user_id"])
    model = ToolFake(responses=[
        _call("enterprise_inspect_work_item", {"task_id": work["task_id"]}, "inspect"),
        _call("enterprise_correct_draft", {"draft_id": draft["draft_id"], "expected_draft_revision": 1, "purpose": "入职培训资料"}, "edit"),
        AIMessage(content="已更正草稿，尚未保存发票。"),
    ])
    app = create_app(database_path=database, agent_store_path=tmp_path / "budget.sqlite3", assistant_model=model)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, token)
    task_id = work["task_id"]
    projection = client.get(f"/api/work-items/{task_id}/projection")
    assert projection.status_code == 200 and projection.json()["awaiting_confirmation"]
    response = client.post(f"/api/work-items/{task_id}/assistant/turn", json={"message": "把草稿更正为入职培训资料，先不要保存。"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json()["status"] == "ok"
    session_id = response.json()["session_id"]
    sessions = client.get(f"/api/work-items/{task_id}/assistant/sessions").json()["sessions"]
    assert [item["session_id"] for item in sessions] == [session_id]
    history = client.get(f"/api/work-items/{task_id}/assistant/history", params={"session_id": session_id})
    assert history.status_code == 200 and history.json()["turns"][0]["output_text"] == "已更正草稿，尚未保存发票。"
    with EnterpriseDatabase(database) as db:
        updated = EnterpriseService(db).work_item(user=accountant, task_id=task_id)["drafts"][0]
        assert updated["proposed_purpose"] == "入职培训资料" and updated["draft_revision"] == 2
    other = TestClient(app)
    other.cookies.set(SESSION_COOKIE, reviewer_token)
    assert other.get(f"/api/work-items/{task_id}/assistant/history", params={"session_id": session_id}).status_code == 403
    assert other.get(f"/api/work-items/{task_id}/assistant/sessions").json()["sessions"] == []


def test_enterprise_chat_accepts_pdf_with_text_and_preserves_attachment(tmp_path):
    class Vision:
        def __init__(self):
            self.messages = None

        def invoke(self, messages, **_kwargs):
            self.messages = messages
            return AIMessage(content=json.dumps({"documents": [{"invoice_number": "24312000000055556666", "amount": "24.00"}]}))

    database = tmp_path / "enterprise.sqlite3"
    _, accountant, reviewer, _, work, _, _ = _prepare(database)
    with EnterpriseDatabase(database) as db:
        token, csrf = issue_session(db, accountant["user_id"])
        reviewer_token, _ = issue_session(db, reviewer["user_id"])
    vision = Vision()
    assistant = ToolFake(responses=[AIMessage(content="已读取票据，尚未保存。")])
    app = create_app(database_path=database, agent_store_path=tmp_path / "budget.sqlite3", assistant_model=assistant, vision_model=vision)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, token)
    sample = Path(__file__).resolve().parents[2] / "datasets/china_invoice_guard/07_行程发票.pdf"
    content = sample.read_bytes()
    response = client.post(f"/api/work-items/{work['task_id']}/assistant/turn-with-files",
        data={"message": "这张用于客户拜访，先不要保存。", "session_id": ""}, files=[("files", ("行程发票.pdf", content, "application/pdf"))], headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert "客户拜访" in vision.messages[1].content[0]["text"]
    assert any(block["type"] == "image_url" for block in vision.messages[1].content)
    session_id, turn_id = response.json()["session_id"], response.json()["turn_id"]
    store = AgentStore(tmp_path / "assistant/agent.sqlite3")
    stored = store.get_turn(turn_id, accountant["user_id"])
    assert "24312000000055556666" in stored["input_context_json"]
    history = client.get(f"/api/work-items/{work['task_id']}/assistant/history", params={"session_id": session_id}).json()
    attachment = history["turns"][0]["attachments"][0]
    assert attachment["filename"] == "行程发票.pdf"
    url = f"/api/work-items/{work['task_id']}/assistant/attachments/{attachment['attachment_id']}"
    assert client.get(url).content == content
    other = TestClient(app)
    other.cookies.set(SESSION_COOKIE, reviewer_token)
    assert other.get(url).status_code == 403
    second = client.post(f"/api/work-items/{work['task_id']}/assistant/turn", json={"message": "刚才那张票的金额是多少？"}, headers={"X-CSRF-Token": csrf}).json()
    assert second["status"] == "ok"
    assert "24312000000055556666" in store.get_turn(second["turn_id"], accountant["user_id"])["input_context_json"]


def test_conversation_can_create_a_real_shared_draft_without_a_fake_reply(tmp_path):
    database = tmp_path / "enterprise.sqlite3"
    company, accountant, _, _, work, invoice_id, old_draft = _prepare(database)
    with EnterpriseDatabase(database) as db:
        EnterpriseService(db).reject_draft(user=accountant, draft_id=old_draft["draft_id"])
    runtime = EnterpriseAgentRuntime(database_path=database, agent_store_path=tmp_path / "agent.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3", active_task_id=work["task_id"])
    try:
        model = ToolFake(responses=[
            _call("enterprise_create_draft", {"invoice_id": invoice_id, "purpose": "客户拜访资料"}, "new-draft"),
            AIMessage(content="已整理草稿，尚未保存。"),
        ])
        result = runtime.run_turn(session_id="new", user_id=accountant["user_id"], company_id=company["company_id"],
                                  message="这张票用于客户拜访资料，先记草稿。", model=model)
        assert result["status"] == "ok"
        tool_result = next(json.loads(item["data"]["content"]) for item in result["messages"] if item["type"] == "tool")
        assert tool_result["status"] == "created" and not tool_result["invoice_written"]
        with EnterpriseDatabase(database) as db:
            detail = EnterpriseService(db).work_item(user=accountant, task_id=work["task_id"])
            current = next(item for item in detail["drafts"] if item["draft_id"] == tool_result["draft"]["draft_id"])
            assert current["origin"] == "chat" and current["response_id"] is None and current["request_id"] is None
            assert current["evidence_quote"] == "这张票用于客户拜访资料，先记草稿。"
            assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] is None
    finally:
        runtime.close()

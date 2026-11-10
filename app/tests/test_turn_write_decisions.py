import json

from langchain_core.messages import AIMessage

from taxharness.enterprise.conversation import EnterpriseAgentRuntime
from taxharness.enterprise.storage import EnterpriseDatabase
from taxharness.turn_intent import write_conditions, write_restricted
from test_enterprise_conversation import ToolFake, _call, _prepare


def test_explicit_read_only_request_is_enforced_before_a_model_tool_write(tmp_path):
    database = tmp_path / "enterprise.sqlite3"
    company, accountant, _, _, work, invoice_id, draft = _prepare(database)
    runtime = EnterpriseAgentRuntime(database_path=database, agent_store_path=tmp_path / "agent.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3", preference_store_path=tmp_path / "prefs.sqlite3",
        active_task_id=work["task_id"])
    try:
        model = ToolFake(responses=[
            _call("enterprise_confirm_drafts", {"drafts": [{"draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"]}]}, "wrong-write"),
            AIMessage(content="草稿已保存。"),
        ])
        result = runtime.run_turn(session_id="read-only", user_id=accountant["user_id"], company_id=company["company_id"],
                                  message="只看看草稿，不要保存。", model=model)
        outputs = [json.loads(item["data"]["content"]) for item in result["messages"] if item["type"] == "tool"]
        assert outputs[0]["status"] == "write_not_authorized"
        assert "已保存" not in result["reply"] and "未执行" in result["reply"]
        with EnterpriseDatabase(database) as db:
            assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] is None
            assert db.connection.execute("SELECT COUNT(*) FROM enterprise_confirmation_receipts").fetchone()[0] == 0
    finally:
        runtime.close()


def test_explicit_prohibition_respects_ticket_field_and_later_correction():
    assert write_restricted("F003 暂时不要保存，F002 保存", ["F003"], ["purpose"])
    assert not write_restricted("F003 暂时不要保存，F002 保存", ["F002"], ["purpose"])
    assert not write_restricted("不要修改用途，备注改为已核对", ["F003"], ["remarks"])
    assert not write_restricted("先不要保存，后来确认可以保存", ["F003"], ["purpose"])
    assert not write_restricted("用途不为空时保存备注", ["F003"], ["remarks"])
    assert write_restricted("只看，不保存，并解释保存规则", ["F003"], ["purpose"])
    mixed = "F002和F003用于培训资料，先保存这两张；F006是项目服务，先放草稿不要保存；F009我回头核实。"
    assert not write_restricted(mixed, ["F002"], ["purpose"])
    assert not write_restricted(mixed, ["F003"], ["purpose"])
    assert write_restricted(mixed, ["F006"], ["purpose"])
    companies = {"C001": ["C001", "甲企业"], "C002": ["C002", "乙企业"]}
    cross_company = "甲企业F002保存；乙企业F002不要保存。"
    assert not write_restricted(cross_company, ["F002"], ["purpose"], company_id="C001", companies=companies)
    assert write_restricted(cross_company, ["F002"], ["purpose"], company_id="C002", companies=companies)
    assert write_conditions("如果 F003 的用途仍为空才保存，否则不要动", ["F003"]) == (True, {"purpose"})
    assert write_conditions("保存 F002，如果门槛降到500元会影响哪些票？", ["F002"]) == (False, set())


def test_enterprise_conditional_save_does_not_overwrite_a_nonempty_purpose(tmp_path):
    database = tmp_path / "conditional.sqlite3"
    company, accountant, _, _, work, invoice_id, draft = _prepare(database)
    with EnterpriseDatabase(database) as db:
        db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE invoice_id=?", ("他人已确认的用途", invoice_id))
    runtime = EnterpriseAgentRuntime(database_path=database, agent_store_path=tmp_path / "agent.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3", preference_store_path=tmp_path / "prefs.sqlite3", active_task_id=work["task_id"])
    try:
        model = ToolFake(responses=[
            _call("enterprise_confirm_drafts", {"drafts": [{"draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"]}]}, "conditional"),
            AIMessage(content="已保存。"),
        ])
        result = runtime.run_turn(session_id="conditional", user_id=accountant["user_id"], company_id=company["company_id"],
                                  message="只有用途为空才保存，否则保持原样。", model=model)
        assert result["status"] == "ok", result
        outputs = [json.loads(item["data"]["content"]) for item in result["messages"] if item["type"] == "tool"]
        assert outputs[0]["status"] == "precondition_not_met"
        assert "未执行" in result["reply"]
        with EnterpriseDatabase(database) as db:
            assert db.connection.execute("SELECT purpose FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0] == "他人已确认的用途"
    finally:
        runtime.close()

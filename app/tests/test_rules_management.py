from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime

from taxharness.agent import ProgramContextMiddleware
from taxharness.agent_tools import AgentContext
from taxharness.errors import InvalidInputError, RuleConflictError
from taxharness.rule_manager import RuleManager
from taxharness.services import TaxService
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


SOURCE_XLSX = Path(__file__).resolve().parents[1] / "data" / "02_示例数据.xlsx"
RULES_DOC = Path(__file__).resolve().parents[1] / "data" / "03_业务规则.docx.txt"


@pytest.fixture
def database(tmp_path: Path):
    path = tmp_path / "rules.sqlite3"
    initialize_database(path, SOURCE_XLSX, RULES_DOC)
    with TaxDatabase(path) as db:
        yield path, db


def _write_rule(path: Path, *, rule_id: str = "R003", version: str = "2.0", status: str = "active", condition: dict | None = None, content: str = "金额达到500.00元时必须填写用途。") -> Path:
    path.write_text(json.dumps({
        "rules": [{
            "rule_id": rule_id,
            "version": version,
            "name": "乙企业发票用途规则",
            "kind": "invoice_purpose",
            "company_id": "C002",
            "effective_from": "2026-09-01",
            "status": status,
            "content": content,
            "condition": condition or {"type": "amount_gte", "amount_cents": 50000},
        }]
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_new_version_changes_condition_and_refreshes_active_all_missing(database, tmp_path):
    path, db = database
    tasks = TaskManager(db)
    created = tasks.create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")
    assert created["task"]["pending_invoice_ids"] == ["F002", "F005", "F006"]
    manager = RuleManager(db)
    imported = manager.import_file(_write_rule(tmp_path / "r003-2.json"))
    assert imported["status"] == "imported"
    missing = TaxService(db).list_missing_purposes(user_id="U001", company_id="C002")
    assert missing["status"] == "ok"
    assert missing["data"]["rule"]["version"] == "2.0"
    assert "F004" in {row["invoice_id"] for row in missing["data"]["missing"]}
    refreshed = tasks.resume_task(task_id=None, user_id="U001", company_id="C002")
    assert refreshed["status"] == "resumed"
    assert set(refreshed["task"]["pending_invoice_ids"]) == {"F001", "F002", "F004", "F005", "F006"}
    assert "2.0" in refreshed["task"]["message"]

    disabled = manager.set_status(rule_id="R003", version="2.0", status="inactive")
    assert disabled["status"] == "disabled"
    no_rule = TaxService(db).list_missing_purposes(user_id="U001", company_id="C002")
    assert no_rule["status"] == "rule_not_found"
    assert tasks.resume_task(task_id=None, user_id="U001", company_id="C002")["status"] == "rule_not_found"
    preserved = tasks.get_task(task_id=created["task"]["task_id"], user_id="U001", company_id="C002")["task"]
    assert preserved["status"] == "open"
    assert set(preserved["pending_invoice_ids"]) == {"F001", "F002", "F004", "F005", "F006"}

    initialize_database(path, SOURCE_XLSX, RULES_DOC)
    after_reinit = RuleManager(db).get_rule(rule_id="R003", version="2.0")
    assert after_reinit["rule"]["status"] == "inactive"
    assert after_reinit["rule"]["condition"]["amount_cents"] == 50000


def test_unknown_condition_is_explicit_and_does_not_create_or_complete_task(database, tmp_path):
    _, db = database
    manager = RuleManager(db)
    imported = manager.import_file(_write_rule(tmp_path / "unknown.json", version="3.0", condition={"type": "between", "low": 1, "high": 2}, content="存在无法自动执行的条件。"))
    assert imported["rules"][0]["rule"]["execution_status"] == "unsupported"
    result = TaxService(db).list_missing_purposes(user_id="U001", company_id="C002")
    assert result["status"] == "cannot_determine"
    created = TaskManager(db).create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")
    assert created["status"] == "cannot_determine"
    assert TaskManager(db).list_tasks(user_id="U001", company_id="C002")["tasks"] == []


def test_import_is_atomic_and_active_version_cannot_be_updated(database, tmp_path):
    _, db = database
    manager = RuleManager(db)
    invalid_batch = tmp_path / "invalid-batch.json"
    invalid_batch.write_text(json.dumps({"rules": [
        {"rule_id": "R010", "version": "1.0", "name": "测试", "kind": "invoice_purpose", "company_id": "C001", "effective_from": "2026-09-01", "status": "inactive", "content": "测试", "condition": {"type": "all"}},
        {"rule_id": "R011", "version": "1.0", "name": "缺条件", "kind": "invoice_purpose", "company_id": "C001", "effective_from": "2026-09-01", "status": "inactive", "content": "缺条件"},
    ]}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(InvalidInputError):
        manager.import_file(invalid_batch)
    assert manager.get_rule(rule_id="R010", version="1.0")["status"] == "not_found"
    with pytest.raises(RuleConflictError):
        manager.update_file(_write_rule(tmp_path / "active-update.json", rule_id="R003", version="1.0", status="inactive"))


def test_same_date_different_rule_ids_are_not_silently_selected(database, tmp_path):
    _, db = database
    manager = RuleManager(db)
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"rules": [{
        "rule_id": "R010", "version": "1.0", "name": "甲用途候选一", "kind": "invoice_purpose", "company_id": "C001",
        "effective_from": "2026-09-01", "status": "active", "content": "候选一", "condition": {"type": "all"},
    }]}, ensure_ascii=False), encoding="utf-8")
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"rules": [{
        "rule_id": "R011", "version": "1.0", "name": "甲用途候选二", "kind": "invoice_purpose", "company_id": "C001",
        "effective_from": "2026-09-01", "status": "active", "content": "候选二", "condition": {"type": "none"},
    }]}, ensure_ascii=False), encoding="utf-8")
    manager.import_file(first)
    manager.import_file(second)
    result = TaxService(db).list_missing_purposes(user_id="U001", company_id="C001")
    assert result["status"] == "rule_conflict"


def test_amount_query_survives_purpose_rule_conflict_with_warning(database, tmp_path):
    _, db = database
    manager = RuleManager(db)
    for rule_id, condition in (("R010", {"type": "all"}), ("R011", {"type": "none"})):
        path = tmp_path / f"{rule_id}.json"
        path.write_text(json.dumps({"rules": [{
            "rule_id": rule_id, "version": "1.0", "name": rule_id, "kind": "invoice_purpose", "company_id": "C001",
            "effective_from": "2026-09-01", "status": "active", "content": rule_id, "condition": condition,
        }]}, ensure_ascii=False), encoding="utf-8")
        manager.import_file(path)
    result = TaxService(db).query_all(user_id="U001", company_id="C001", month="2026-09")
    assert result["status"] == "ok"
    warning = next(rule for rule in result["data"]["rules"] if rule.get("kind") == "invoice_purpose")
    assert warning["status"] == "rule_conflict"
    assert result["data"]["invoice"]["invoice_total"] == "9622.06"


def test_empty_legacy_condition_is_readable_as_unsupported(database):
    _, db = database
    db.connection.execute("UPDATE rules SET condition_json='{}',condition_status='unsupported',condition_summary='' WHERE rule_id='R003' AND version='1.0'")
    result = RuleManager(db).get_rule(rule_id="R003", version="1.0")
    assert result["status"] == "ok"
    assert result["rule"]["execution_status"] == "unsupported"
    assert "自动判定" in result["rule"]["execution_summary"]


def test_import_rejects_unknown_company_before_writing_batch(database, tmp_path):
    _, db = database
    path = _write_rule(tmp_path / "unknown-company.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rules"][0]["company_id"] = "C999"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(InvalidInputError):
        RuleManager(db).import_file(path)
    assert RuleManager(db).get_rule(rule_id="R003", version="2.0")["status"] == "not_found"


def test_rule_refresh_warning_is_in_context_without_blocking_other_model_calls(database, tmp_path):
    path, db = database
    task = TaskManager(db).create_task(user_id="U001", company_id="C002", month="2026-09", scope="all_missing")
    RuleManager(db).set_status(rule_id="R003", version="1.0", status="inactive")
    context = AgentContext(
        user_id="U001", session_id="rule-warning", database_path=str(path), agent_store_path=str(tmp_path / "agent.sqlite3"),
        current_company_id="C002", preference_store_path=str(tmp_path / "preferences.sqlite3"),
    )
    request = ModelRequest(
        model=FakeMessagesListChatModel(responses=[]), messages=[], system_message=SystemMessage(content="base"), tools=[], runtime=Runtime(context=context)
    )
    seen = {}
    ProgramContextMiddleware().wrap_model_call(request, lambda value: seen.setdefault("request", value) or value)
    content = seen["request"].system_message.content
    assert "task_refresh_warning" in content and "已停用" in content
    assert TaskManager(db).get_task(task_id=task["task"]["task_id"], user_id="U001", company_id="C002")["task"]["status"] == "open"

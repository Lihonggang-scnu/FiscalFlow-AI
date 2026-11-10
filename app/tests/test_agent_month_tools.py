from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.agent import AgentRuntime
from taxharness.cli import main
from taxharness.storage import TaxDatabase, initialize_database


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SOURCE_XLSX = DATA_DIR / "02_示例数据.xlsx"
RULES_DOC = DATA_DIR / "03_业务规则.docx.txt"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "month-tools.sqlite3"
    initialize_database(path, SOURCE_XLSX, RULES_DOC)
    with TaxDatabase(path) as db:
        yield path, db


class ToolFake(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _run_tool(path: Path, tmp_path: Path, *, tool_name: str, args: dict, user_id: str = "U001", company_id: str = "C001", message: str = "这句话没有包含日期。"):
    runtime = AgentRuntime(
        database_path=path,
        agent_store_path=tmp_path / f"{tool_name}-agent.sqlite3",
        checkpoint_path=tmp_path / f"{tool_name}-checkpoint.sqlite3",
        preference_store_path=tmp_path / f"{tool_name}-preferences.sqlite3",
    )
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": f"call-{tool_name}", "type": "tool_call"}]),
            AIMessage(content="工具结果已返回。"),
        ])
        return runtime.run_turn(session_id=f"month-tools-{tool_name}-{user_id}", user_id=user_id, company_id=company_id, message=message, model=model)
    finally:
        runtime.close()


def _tool_payload(result: dict) -> dict:
    tool_messages = [message for message in result["messages"] if message["type"] == "tool"]
    assert tool_messages
    return json.loads(tool_messages[-1]["data"]["content"])


@pytest.mark.parametrize(
    ("tool_name", "args", "expected_status"),
    [
        ("query_finance", {"month": "2026-09", "company_id": "C001"}, "ok"),
        ("list_missing_purposes", {"month": "2026-09", "company_id": "C001"}, "ok"),
        ("task_create", {"month": "2026-09", "scope": "all_missing", "company_id": "C001"}, "created"),
    ],
)
def test_explicit_month_parameter_runs_without_matching_user_text(database, tmp_path, tool_name, args, expected_status):
    path, _ = database
    result = _run_tool(path, tmp_path, tool_name=tool_name, args=args)

    assert result["status"] == "ok"
    assert _tool_payload(result)["status"] == expected_status


def test_query_month_format_is_validated_by_business_service(database, tmp_path):
    path, _ = database
    result = _run_tool(path, tmp_path, tool_name="query_finance", args={"month": "2026/09", "company_id": "C001"})

    assert result["status"] == "ok"
    payload = _tool_payload(result)
    assert payload["status"] == "invalid_input"
    assert "YYYY-MM" in payload["summary"]


def test_month_tool_still_enforces_company_permission(database, tmp_path):
    path, _ = database
    result = _run_tool(path, tmp_path, tool_name="query_finance", args={"month": "2026-09", "company_id": "C002"}, user_id="U002")

    assert result["status"] == "ok"
    assert _tool_payload(result)["status"] == "permission_denied"


def test_missing_task_month_returns_waiting_input_for_model_to_ask(database, tmp_path):
    path, _ = database
    result = _run_tool(path, tmp_path, tool_name="task_create", args={"scope": "all_missing", "company_id": "C001"}, message="开始用途补全任务，但月份还没有提供。")

    assert result["status"] == "ok"
    payload = _tool_payload(result)
    assert payload["status"] == "created"
    assert payload["task"]["status"] == "waiting_input"
    assert payload["task"]["next_step"] == "补充月份"


def test_missing_business_database_is_not_reported_as_permission_denied(tmp_path, capsys):
    missing = tmp_path / "not-initialized.sqlite3"
    runtime = AgentRuntime(database_path=missing, agent_store_path=tmp_path / "agent.sqlite3", checkpoint_path=tmp_path / "checkpoint.sqlite3")
    try:
        result = runtime.run_turn(session_id="missing-db", user_id="U001", company_id="C001", message="查甲企业金额。", model=FakeMessagesListChatModel(responses=[]))
        assert result["status"] == "database_not_initialized"
    finally:
        runtime.close()
    code = main(["query", "--database", str(tmp_path / "cli-missing.sqlite3"), "--user-id", "U001", "--company-id", "C001", "--month", "2026-09"])
    assert code != 0 and "database_not_initialized" in capsys.readouterr().out

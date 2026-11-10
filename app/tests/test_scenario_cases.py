from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from taxharness.agent import AgentRuntime
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_manager import TaskManager


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_taskledger_scenarios.py"
spec = importlib.util.spec_from_file_location("live_taskledger_scenarios", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


def test_failure_case_keeps_pre_turn_state_and_can_be_inspected_without_a_model(tmp_path):
    business = tmp_path / "business.sqlite3"
    with closing(sqlite3.connect(business)) as database:
        database.execute("CREATE TABLE example(value TEXT)")
        database.execute("INSERT INTO example VALUES ('原始状态')")
        database.commit()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    names = scenario._snapshot_before_turn(tmp_path, snapshot)
    with closing(sqlite3.connect(business)) as database:
        database.execute("UPDATE example SET value='后来状态'")
        database.commit()
    case = {"name": "草稿更正", "request": "更正用途", "check": {"name": "corrected"}, "snapshot_files": names}
    path = scenario._save_failure_case(tmp_path, snapshot, names, case)
    assert json.loads(path.read_text(encoding="utf-8"))["check"]["name"] == "corrected"
    with closing(sqlite3.connect(path.parent / "business.sqlite3")) as database:
        assert database.execute("SELECT value FROM example").fetchone()[0] == "原始状态"
    result = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--replay-case", str(path)],
                            capture_output=True, text=True, encoding="utf-8", check=True,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert "草稿更正" in result.stdout


def test_case_snapshot_can_resume_the_same_agent_session(tmp_path):
    class ToolFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    app = SCRIPT.parents[1]
    business = tmp_path / "business.sqlite3"
    initialize_database(business, app / "data/02_示例数据.xlsx", app / "data/03_业务规则.docx.txt")
    runtime = AgentRuntime(database_path=business, agent_store_path=tmp_path / "agent.sqlite3",
        checkpoint_path=tmp_path / "checkpoint.sqlite3", preference_store_path=tmp_path / "preferences.sqlite3")
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "task_create", "args": {"month": "2026-09", "scope": "selected", "invoice_ids": ["F003"]}, "id": "create", "type": "tool_call"}]),
            AIMessage(content="任务已建立。"),
        ])
        assert runtime.run_turn(session_id="snapshot-session", user_id="U001", company_id="C001", message="跟进 F003。", model=model)["status"] == "ok"
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        names = scenario._snapshot_before_turn(tmp_path, snapshot)
        assert {"business.sqlite3", "agent.sqlite3", "checkpoint.sqlite3", "preferences.sqlite3"} <= set(names)
    finally:
        runtime.close()

    case_path = scenario._save_failure_case(tmp_path, snapshot, names, {"name": "接续", "request": "继续", "snapshot_files": names})
    case_dir = case_path.parent
    replay = AgentRuntime(database_path=case_dir / "business.sqlite3", agent_store_path=case_dir / "agent.sqlite3",
        checkpoint_path=case_dir / "checkpoint.sqlite3", preference_store_path=case_dir / "preferences.sqlite3")
    try:
        model = ToolFake(responses=[
            AIMessage(content="", tool_calls=[{"name": "inspect_task_frontier", "args": {}, "id": "frontier", "type": "tool_call"}]),
            AIMessage(content="F003 仍待补充。"),
        ])
        result = replay.run_turn(session_id="snapshot-session", user_id="U001", company_id="C001", message="继续。", model=model)
        assert result["status"] == "ok"
        with TaxDatabase(case_dir / "business.sqlite3") as database:
            task = TaskManager(database).list_tasks(user_id="U001", company_id="C001")["tasks"][0]
            assert task["pending_invoice_ids"] == ["F003"]
    finally:
        replay.close()


def test_recovery_answer_accepts_normal_success_wording_and_rejects_missing_receipt():
    recovery_spec = importlib.util.spec_from_file_location("live_recovery_scenario", SCRIPT.parent / "live_recovery_scenario.py")
    recovery = importlib.util.module_from_spec(recovery_spec)
    recovery_spec.loader.exec_module(recovery)
    assert recovery._historical_receipt_explained("那次保存确实提交成功过，当时写入购买培训资料。")
    assert not recovery._historical_receipt_explained("购买培训资料现在能查到，但没有留下可查的操作记录。")

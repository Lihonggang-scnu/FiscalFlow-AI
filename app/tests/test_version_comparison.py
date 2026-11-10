from __future__ import annotations

import os
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from taxharness.agent_store import AgentStore


APP = Path(__file__).resolve().parents[1]
BASELINE = APP.parent / "baseline/app"


@pytest.mark.parametrize("app", [BASELINE, APP], ids=["baseline", "upgrade"])
def test_each_version_runs_against_an_independent_database(app, tmp_path):
    code = r'''
import os, sys
from pathlib import Path
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
app=Path(os.environ["TASKLEDGER_COMPARE_APP"])
root=Path(os.environ["TASKLEDGER_COMPARE_STATE"])
sys.path.insert(0,str(app / "src"))
from taxharness.storage import initialize_database
from taxharness.agent import AgentRuntime
class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs): return self
business=root / "business.sqlite3"
initialize_database(business, app / "data/02_示例数据.xlsx", app / "data/03_业务规则.docx.txt")
runtime=AgentRuntime(database_path=business, agent_store_path=root/"agent.sqlite3", checkpoint_path=root/"checkpoint.sqlite3")
try:
    model=Model(responses=[AIMessage(content="",tool_calls=[{"name":"query_finance","args":{"month":"2026-09"},"id":"query","type":"tool_call"}]),AIMessage(content="完成")])
    result=runtime.run_turn(session_id="compare",user_id="U001",company_id="C001",message="查询九月金额",model=model)
    assert result["status"] == "ok"
    assert any("9622.06" in item["data"].get("content","") for item in result["messages"] if item["type"]=="tool")
finally:
    runtime.close()
'''
    env = {**os.environ, "TASKLEDGER_COMPARE_APP": str(app), "TASKLEDGER_COMPARE_STATE": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, encoding="utf-8", check=True)


def test_comparison_usage_counts_only_its_own_sessions(tmp_path):
    spec = importlib.util.spec_from_file_location("compare_taskledger_versions", APP / "scripts/compare_taskledger_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ledger = tmp_path / "budget.sqlite3"
    store = AgentStore(ledger)
    for session in ("our-run:first", "other-run:first"):
        call = store.reserve_model_call(session_id=session, user_id="U001", model="deepseek-flash",
            estimated_input_tokens=2000, estimated_output_tokens=500, budget_usd=2)
        store.settle_model_call(call, input_tokens=1000, output_tokens=100)
    usage = module._run_usage(ledger, "our-run:")
    assert usage == {"model_calls": 1, "input_tokens": 1000, "output_tokens": 100,
                     "estimated_increment_cny": 0.0014, "unknown_calls": 0}

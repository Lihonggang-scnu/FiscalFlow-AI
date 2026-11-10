"""Bounded live validation for rule-aware Agent behavior on an isolated copy."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable).resolve()
LIVE_ROOT = PROJECT_ROOT / "artifacts" / "rules" / "live_20261026"
BUSINESS = LIVE_ROOT / "business.sqlite3"
PREFERENCES = LIVE_ROOT / "preferences.sqlite3"
CHECKPOINT = LIVE_ROOT / "checkpoints.sqlite3"
LEDGER = PROJECT_ROOT / "artifacts" / "agent" / "agent.sqlite3"
SUMMARY = LIVE_ROOT / "live_summary.json"
BUDGET = 0.3491618


def ledger_usage() -> dict[str, float | int]:
    connection = sqlite3.connect(LEDGER)
    try:
        calls, settled, pending, unknown = connection.execute(
            """SELECT count(*), coalesce(sum(actual_usd),0),
                      coalesce(sum(case when status='pending' then reserved_usd else 0 end),0),
                      coalesce(sum(case when status='unknown' then reserved_usd else 0 end),0)
               FROM agent_model_calls"""
        ).fetchone()
        return {"calls": int(calls), "settled_usd": float(settled), "pending_reserved_usd": float(pending), "unknown_reserved_usd": float(unknown), "effective_spent_usd": float(settled + pending + unknown)}
    finally:
        connection.close()


def cli(environment: dict[str, str], name: str, args: list[str]) -> tuple[int, dict]:
    process = subprocess.run(
        [str(PYTHON), "-m", "taxharness.cli", *args, "--database", str(BUSINESS)],
        cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=240,
    )
    (LIVE_ROOT / f"{name}.raw.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError:
        value = {"status": "invalid_json", "stdout": process.stdout[-4000:], "stderr": process.stderr[-2000:]}
    return process.returncode, value


def chat(environment: dict[str, str], name: str, *, session_id: str, message: str) -> tuple[int, dict]:
    args = [
        "agent-chat", "--agent-store", str(LEDGER), "--preference-store", str(PREFERENCES),
        "--checkpoint-store", str(CHECKPOINT), "--user-id", "U001", "--company-id", "C002",
        "--session-id", session_id, "--message", message, "--budget-usd", str(BUDGET),
    ]
    process = subprocess.run(
        [str(PYTHON), "-m", "taxharness.cli", *args, "--database", str(BUSINESS)],
        cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=240,
    )
    (LIVE_ROOT / f"{name}.raw.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError:
        value = {"status": "invalid_json", "stdout": process.stdout[-4000:], "stderr": process.stderr[-2000:]}
    return process.returncode, value


def main() -> int:
    if LIVE_ROOT.exists():
        raise SystemExit(f"live artifact directory already exists: {LIVE_ROOT}")
    LIVE_ROOT.mkdir(parents=True)
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(PROJECT_ROOT / "src"), "TAXHARNESS_LIVE_MODEL_ENABLED": "true"}
    baseline = ledger_usage()
    if baseline["effective_spent_usd"] > BUDGET:
        raise SystemExit("shared ledger is already above the approved hard limit")

    rule_file = LIVE_ROOT / "r003-2.json"
    rule_file.write_text(json.dumps({"rules": [{
        "rule_id": "R003", "version": "2.0", "name": "乙企业发票用途规则", "kind": "invoice_purpose",
        "company_id": "C002", "effective_from": "2026-09-01", "status": "active",
        "content": "金额达到500.00元时必须填写用途。", "condition": {"type": "amount_gte", "amount_cents": 50000},
    }]}, ensure_ascii=False), encoding="utf-8")

    checks: dict[str, dict] = {}
    init_code, init = cli(environment, "00_init", ["init-db"])
    checks["init"] = {"return_code": init_code, "result": init}
    stopped_at: str | None = None

    def require(name: str, code: int, result: dict, *, ok_status: str = "ok") -> bool:
        checks[name] = {"return_code": code, "result": result, "ledger_after": ledger_usage()}
        return code == 0 and result.get("status") == ok_status and ledger_usage()["effective_spent_usd"] <= BUDGET

    if not (init_code == 0 and init.get("status") == "ok"):
        stopped_at = "00_init"
    else:
        code, result = chat(environment, "01_initial_rule_read", session_id="rules-live-initial", message="检查乙企业2026年9月哪些发票还缺用途，并说明适用规则版本和实际执行条件。")
        if not require("initial_rule_read", code, result):
            stopped_at = "01_initial_rule_read"
        else:
            code, result = cli(environment, "02_task_create_before", ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09", "--scope", "all_missing"])
            if not require("task_create_before", code, result, ok_status="created"):
                stopped_at = "02_task_create_before"
            else:
                code, result = cli(environment, "03_import_new_rule", ["rule-import", "--file", str(rule_file)])
                if not require("import_new_rule", code, result, ok_status="imported"):
                    stopped_at = "03_import_new_rule"
                else:
                    code, result = chat(environment, "04_refresh_after_import", session_id="rules-live-refresh", message="继续乙企业没有完成的用途任务，并说明当前规则版本和待办。")
                    if not require("refresh_after_import", code, result):
                        stopped_at = "04_refresh_after_import"
                    else:
                        code, result = cli(environment, "05_disable_latest", ["rule-disable", "--rule-id", "R003", "--version", "2.0"])
                        if not require("disable_latest", code, result, ok_status="disabled"):
                            stopped_at = "05_disable_latest"
                        else:
                            code, result = chat(environment, "06_amount_after_disable", session_id="rules-live-amount", message="只查询乙企业2026年9月发票总额、银行收入和银行支出；不要处理用途任务。")
                            if not require("amount_after_disable", code, result):
                                stopped_at = "06_amount_after_disable"
                            else:
                                code, result = cli(environment, "07_cancel_all_missing", ["task-cancel", "--user-id", "U001", "--company-id", "C002", "--task-id", checks["task_create_before"]["result"]["task"]["task_id"]])
                                if not require("cancel_all_missing", code, result, ok_status="cancelled"):
                                    stopped_at = "07_cancel_all_missing"
                                else:
                                    code, result = cli(environment, "08_selected_create", ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09", "--scope", "selected", "--invoice-id", "F004"])
                                    if not require("selected_create", code, result, ok_status="created"):
                                        stopped_at = "08_selected_create"
                                    else:
                                        code, result = chat(environment, "09_selected_save", session_id="rules-live-selected", message="我明确选择乙企业 F004，把用途填写为会议资料并保存。")
                                        if not require("selected_save", code, result):
                                            stopped_at = "09_selected_save"
                                        else:
                                            code, result = cli(environment, "10_reinit", ["init-db"])
                                            if not require("reinit", code, result):
                                                stopped_at = "10_reinit"

    business_probe: dict[str, object] = {}
    if BUSINESS.exists():
        connection = sqlite3.connect(BUSINESS)
        try:
            business_probe = {
                "invoice_count": connection.execute("SELECT count(*) FROM invoices").fetchone()[0],
                "bank_record_count": connection.execute("SELECT count(*) FROM bank_records").fetchone()[0],
                "f004_c002": dict(zip(("purpose", "remarks", "revision"), connection.execute(
                    "SELECT purpose,remarks,revision FROM invoices WHERE company_id='C002' AND invoice_id='F004'"
                ).fetchone() or (None, None, None))),
            }
        finally:
            connection.close()
    final_usage = ledger_usage()
    result = {
        "checked_at": datetime.now(UTC).isoformat(), "python": str(PYTHON), "model": "deepseek-flash",
        "budget_limit_usd": BUDGET, "baseline": baseline, "final_ledger": final_usage,
        "incremental_effective_spent_usd": final_usage["effective_spent_usd"] - baseline["effective_spent_usd"],
        "checks": checks, "stopped_at": stopped_at, "business_probe": business_probe,
        "raw_reply_directory": str(LIVE_ROOT), "status": "passed" if stopped_at is None else "stopped",
    }
    SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "artifact": str(SUMMARY), "incremental_effective_spent_usd": result["incremental_effective_spent_usd"], "pending_reserved_usd": final_usage["pending_reserved_usd"], "unknown_reserved_usd": final_usage["unknown_reserved_usd"]}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

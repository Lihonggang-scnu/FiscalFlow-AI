"""Run the same isolated business scenario against the retained and upgraded Agent."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


UPGRADE_APP = Path(__file__).resolve().parents[1]
UPGRADE = UPGRADE_APP.parent
PROJECT = UPGRADE.parents[1]
BASELINE_APP = PROJECT / "artifacts" / "ui_three_views_candidate_20261028" / "app"


def _invoice(database: Path, company: str, invoice: str) -> tuple:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT purpose,remarks,revision FROM invoices WHERE company_id=? AND invoice_id=?", (company, invoice)).fetchone()


def _budget_total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _run_usage(path: Path, prefix: str) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("""SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),
            COALESCE(SUM(CASE WHEN status IN ('pending','unknown') THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0),
            SUM(CASE WHEN status IN ('pending','unknown') THEN 1 ELSE 0 END)
            FROM agent_model_calls WHERE substr(session_id,1,?)=?""", (len(prefix), prefix)).fetchone()
    return {"model_calls": row[0], "input_tokens": row[1], "output_tokens": row[2],
            "estimated_increment_cny": round(row[3] / 0.3, 6), "unknown_calls": row[4] or 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", choices=("baseline", "upgrade"), required=True)
    parser.add_argument("--case", choices=("cross_session", "mixed_companies", "switch_order", "long_correction"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    app = BASELINE_APP if args.version == "baseline" else UPGRADE_APP
    if args.dry_run:
        print(f"配置：{args.version}；场景：{args.case}；代码：{app}")
        return 0
    sys.path.insert(0, str(app / "src"))
    from taxharness.agent import AgentRuntime
    from taxharness.provider import _peak_window
    from taxharness.storage import initialize_database

    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行比较实验。")
    budget = json.loads((UPGRADE / "budget.json").read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _budget_total(ledger)
    run_root = UPGRADE / "experiments" / "comparison" / (args.version + "-" + args.case + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    database = run_root / "business.sqlite3"
    initialize_database(database, app / "data/02_示例数据.xlsx", app / "data/03_业务规则.docx.txt")
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)
    runtime = AgentRuntime(database_path=database, agent_store_path=run_root / "agent.sqlite3",
                           checkpoint_path=run_root / "checkpoint.sqlite3", preference_store_path=run_root / "preferences.sqlite3",
                           budget_ledger_path=ledger)
    report = {"version": args.version, "case": args.case, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "steps": []}
    session_prefix = run_root.name + ":"

    def turn(session: str, message: str) -> dict:
        started = time.perf_counter()
        result = runtime.run_turn(session_id=session_prefix + session, user_id="U001", company_id="C001", message=message,
                                  live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
        messages = result.get("messages", [])
        current_start = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("type") == "human"), len(messages))
        tools = [item.get("data", {}).get("name") for item in messages[current_start:] if item.get("type") == "tool"]
        outcomes = []
        for item in messages[current_start:]:
            if item.get("type") == "tool":
                data = item.get("data", {})
                try:
                    status = json.loads(data.get("content", "{}")).get("status")
                except (TypeError, json.JSONDecodeError):
                    status = None
                outcomes.append({"tool": data.get("name"), "status": status})
        batches = [[call.get("name") for call in item.get("data", {}).get("tool_calls", [])]
                   for item in messages[current_start:] if item.get("type") == "ai" and item.get("data", {}).get("tool_calls")]
        report["steps"].append({"message": message, "reply": result.get("reply", ""), "status": result.get("status"),
                                "elapsed_seconds": round(time.perf_counter() - started, 2), "tools": tools, "tool_batches": batches, "tool_outcomes": outcomes,
                                "f002": _invoice(database, "C001", "F002"), "f003_a": _invoice(database, "C001", "F003"),
                                "f003_b": _invoice(database, "C002", "F003")})
        print(f"第 {len(report['steps'])} 轮：{result.get('status')}", flush=True)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.get("status") != "ok":
            raise RuntimeError(f"当前轮未完成：{result.get('status')}")
        return result

    try:
        if args.case == "cross_session":
            first = turn("first", "为甲企业2026年9月建立只跟进F002和F003的用途任务。F002的实际用途是购买培训资料，先记为草稿，暂时不要保存。")
            unchanged_after_first = _invoice(database, "C001", "F002")[0] is None
            second = turn("second", "继续甲企业上次9月的任务，告诉我F002草稿的用途，先别改数据。")
            unchanged_after_second = _invoice(database, "C001", "F002")[0] is None
            third = turn("second", "把刚才展示的F002用途草稿保存。")
            value = _invoice(database, "C001", "F002")[0]
            report["checks"] = {"first_no_write": unchanged_after_first, "second_no_write": unchanged_after_second,
                                "resumed_draft_visible": "购买培训资料" in second.get("reply", ""),
                                "correct_final_value": value == "购买培训资料", "runtime_ok": all(x.get("status") == "ok" for x in (first, second, third))}
        elif args.case == "mixed_companies":
            before_other = _invoice(database, "C002", "F003")
            result = turn("mixed", "先把甲企业2026年9月F003用途保存为寄送客户验收文件，再临时查询乙企业2026年9月发票总额；当前办理企业保持甲企业。")
            reply = result.get("reply", "").replace(",", "").replace(" ", "")
            report["checks"] = {"correct_company_saved": _invoice(database, "C001", "F003")[0] == "寄送客户验收文件",
                                "other_company_untouched": _invoice(database, "C002", "F003") == before_other,
                                "correct_other_total": "12667.24" in reply, "company_kept": result.get("current_company_id") == "C001"}
        elif args.case == "switch_order":
            before_other = _invoice(database, "C002", "F003")
            result = turn("switch", "先把甲企业2026年9月F003用途保存为寄送客户验收文件，再将当前办理企业切换到乙企业。请分别说明保存结果和当前企业。")
            outcomes = report["steps"][-1]["tool_outcomes"]
            saves = [index for index, item in enumerate(outcomes) if item["tool"] == "save_invoice" and item["status"] == "committed"]
            switches = [index for index, item in enumerate(outcomes) if item["tool"] == "switch_company" and item["status"] == "ok"]
            report["checks"] = {"correct_company_saved": _invoice(database, "C001", "F003")[0] == "寄送客户验收文件",
                                "other_company_untouched": _invoice(database, "C002", "F003") == before_other,
                                "switched_company": result.get("current_company_id") == "C002",
                                "save_before_switch": bool(saves and switches and saves[0] < switches[0])}
        else:
            turn("long", "建立甲企业2026年9月F002的用途任务。F002用途是购买旧培训资料，只记草稿，暂时不保存。")
            turn("long", "更正刚才F002的用途，是购买新员工培训资料；企业和月份都不变，还是先别保存。")
            for index in range(9):
                month = "2026年8月" if index % 2 == 0 else "2026年9月"
                turn("long", f"只查询甲企业{month}的发票总额，简短回答。")
            unchanged_before_final = _invoice(database, "C001", "F002")[0] is None
            result = turn("long", "把最早那项任务里我更正后的F002用途保存，其他票不要修改。")
            report["checks"] = {"no_early_write": unchanged_before_final,
                                "correct_final_value": _invoice(database, "C001", "F002")[0] == "购买新员工培训资料",
                                "other_invoice_unchanged": _invoice(database, "C001", "F003")[0] is None,
                                "runtime_ok": result.get("status") == "ok"}
        report["passed"] = all(report["checks"].values())
    finally:
        runtime.close()
        after_count, after_total = _budget_total(ledger)
        report.update(_run_usage(ledger, session_prefix))
        report["shared_ledger_new_calls"] = after_count - before_count
        report["shared_ledger_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        report["completed_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

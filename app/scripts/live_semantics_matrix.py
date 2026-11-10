"""Run varied natural-language business requests on isolated official data."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


APP = Path(__file__).resolve().parents[1]
UPGRADE = APP.parent
PROJECT = UPGRADE.parents[1]
sys.path.insert(0, str(APP / "src"))

from taxharness.agent import AgentRuntime
from taxharness.provider import _peak_window
from taxharness.storage import TaxDatabase, initialize_database
from taxharness.task_ledger import TaskLedger


CASES = [
    {"id": "missing_month", "user": "U001", "messages": ["查一下甲企业的发票总额，月份我还没说。", "查2026年9月。"]},
    {"id": "correct_invoice", "user": "U001", "messages": ["甲企业2026年9月，我刚才说错票号了：先说F002，实际是F003用于寄送客户验收文件；只保存F003的用途，F002不要更改。"]},
    {"id": "shared_purpose", "user": "U001", "messages": ["甲企业2026年9月的F002和F003都用于客户培训资料印制，请分别保存两张发票的用途；F004先不动。"]},
    {"id": "mixed_multi", "user": "U001", "messages": ["甲企业2026年9月办用途补全。F002和F003都用于客户培训资料印制，先保存这两张；F006是项目管理服务，先放草稿不要保存；F009我回头核实，别催。只做这些。"]},
    {"id": "same_id_two_companies", "user": "U001", "messages": ["只读核对2026年9月甲乙两家企业的同号F002：各自金额是多少？当前办理企业保持甲企业。"]},
    {"id": "readonly_query", "user": "U002", "messages": ["我只需要查看甲企业2026年9月发票总额和缺用途发票，不要修改任何内容。"]},
    {"id": "out_of_scope", "user": "U001", "messages": ["请检查甲企业2026年8月哪些票不符合用途必填要求；不要更改任何发票或任务。"]},
    {"id": "repeat_save", "user": "U001", "messages": ["把甲企业2026年9月F003用途保存为寄送客户验收文件。", "刚才那条完全一样的保存请求再发一次。"]},
    {"id": "no_payment_inference", "user": "U001", "messages": ["甲企业2026年9月发票总额和银行支出差多少？老板认为差额就是未付款，请核实这个推断。"]},
]


def _ledger_total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _check(case_id: str, path: Path, turns: list[dict]) -> dict:
    replies = [turn.get("reply", "") for turn in turns]
    normalized = [text.replace(",", "").replace(" ", "") for text in replies]
    with TaxDatabase(path) as db:
        rows = {row["invoice_id"]: dict(row) for row in db.connection.execute(
            "SELECT invoice_id,purpose,remarks,revision FROM invoices WHERE company_id='C001' AND invoice_id IN ('F002','F003','F004','F006','F009')")}
        writes = db.connection.execute("SELECT COUNT(*) FROM operations WHERE status='committed'").fetchone()[0]
        memory = TaskLedger(db).memory(user_id="U001", company_id="C001")
    checks = {"runtime_ok": all(turn.get("status") == "ok" for turn in turns)}
    if case_id == "missing_month":
        checks.update({"asked_month": "月" in replies[0] and "9622.06" not in normalized[0], "correct_after_answer": "9622.06" in normalized[1], "no_write": writes == 0})
    elif case_id == "correct_invoice":
        checks.update({"correct_invoice_saved": rows["F003"]["purpose"] == "寄送客户验收文件", "original_invoice_untouched": rows["F002"]["purpose"] is None, "one_write": writes == 1})
    elif case_id == "shared_purpose":
        checks.update({"both_saved": all(rows[item]["purpose"] == "客户培训资料印制" for item in ("F002", "F003")),
                       "other_untouched": rows["F004"]["purpose"] is None, "two_writes": writes == 2})
    elif case_id == "mixed_multi":
        checks.update({"two_saved": all(rows[item]["purpose"] == "客户培训资料印制" for item in ("F002", "F003")),
                       "draft_not_written": rows["F006"]["purpose"] is None and memory["drafts"].get("F006:purpose", {}).get("value") == "项目管理服务",
                       "waiting_preserved": "F009" in memory["deferred"], "two_writes": writes == 2})
    elif case_id == "same_id_two_companies":
        checks.update({"both_amounts": "800.00" in normalized[-1] and "1000.00" in normalized[-1],
                       "company_kept": turns[-1].get("current_company_id") == "C001", "no_write": writes == 0})
    elif case_id == "readonly_query":
        checks.update({"amount_and_missing": "9622.06" in normalized[-1] and all(item in replies[-1] for item in ("F002", "F003", "F004", "F006", "F009")), "no_write": writes == 0})
    elif case_id == "out_of_scope":
        checks.update({"explained_scope": any(word in replies[-1] for word in ("范围", "不支持", "无法", "不能", "仅限")), "no_write": writes == 0})
    elif case_id == "repeat_save":
        checks.update({"value_preserved": rows["F003"]["purpose"] == "寄送客户验收文件", "one_commit": writes == 1, "one_revision": rows["F003"]["revision"] == 2})
    elif case_id == "no_payment_inference":
        checks.update({"rejects_inference": any(word in replies[-1] for word in ("不能", "无法", "不足以", "不代表")), "no_write": writes == 0})
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[case["id"] for case in CASES])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    chosen = [case for case in CASES if args.case is None or case["id"] == args.case]
    if args.dry_run:
        print("、".join(case["id"] for case in chosen))
        return 0
    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行语义场景。")
    budget = json.loads((UPGRADE / "budget.json").read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _ledger_total(ledger)
    run_root = UPGRADE / "experiments" / ("semantics-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)
    report = {"cases": []}
    try:
        for case in chosen:
            root = run_root / case["id"]
            root.mkdir()
            database = root / "business.sqlite3"
            initialize_database(database, APP / "data/02_示例数据.xlsx", APP / "data/03_业务规则.docx.txt")
            runtime = AgentRuntime(database_path=database, agent_store_path=root / "agent.sqlite3",
                                   checkpoint_path=root / "checkpoint.sqlite3", preference_store_path=root / "preferences.sqlite3",
                                   budget_ledger_path=ledger)
            try:
                turns = []
                for message in case["messages"]:
                    result = runtime.run_turn(session_id=run_root.name + ":" + case["id"], user_id=case["user"], company_id="C001",
                                              message=message, live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
                    turns.append(result)
                    if result.get("status") != "ok":
                        break
                checks = _check(case["id"], database, turns) if len(turns) == len(case["messages"]) else {"runtime_ok": False}
                item = {"id": case["id"], "checks": checks, "passed": all(checks.values()),
                        "turns": [{"request": message, "reply": result.get("reply", ""), "status": result.get("status")}
                                  for message, result in zip(case["messages"], turns)]}
                report["cases"].append(item)
                (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{case['id']}：{'通过' if item['passed'] else '需处理'} {checks}", flush=True)
                if not item["passed"]:
                    break
            finally:
                runtime.close()
        report["passed"] = len(report["cases"]) == len(chosen) and all(item["passed"] for item in report["cases"])
        return 0 if report["passed"] else 1
    finally:
        after_count, after_total = _ledger_total(ledger)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

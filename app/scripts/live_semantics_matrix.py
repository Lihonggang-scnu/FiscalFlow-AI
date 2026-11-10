"""Run varied natural-language business requests on isolated official data."""

from __future__ import annotations

import argparse
import json
import os
import re
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
    {"id": "conditional_preserve", "user": "U001", "initial_purpose": "外部已核实用途", "messages": ["只有甲企业2026年9月F003的用途仍为空才保存为寄送客户验收文件，否则保留原用途并说明结果。"]},
    {"id": "conditional_empty", "user": "U001", "messages": ["如果甲企业2026年9月F003的用途还是空白，就保存为寄送客户验收文件；否则保留原用途。"]},
]


def _ledger_total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _tool_calls(turn: dict) -> list[dict]:
    return [call for message in turn.get("messages", []) if message.get("type") == "ai"
            for call in message.get("data", {}).get("tool_calls", [])]


def _company_amounts(reply: str) -> bool | None:
    associations: dict[str, set[str]] = {"C001": set(), "C002": set()}
    names = {"甲企业": "C001", "C001": "C001", "乙企业": "C002", "C002": "C002"}
    text = reply.replace(",", "").replace("，", "\n").replace("；", "\n").replace("。", "\n")
    for line in text.splitlines():
        matches = list(re.finditer(r"甲企业|乙企业|C001|C002", line, flags=re.I))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            amounts = re.findall(r"(?<![\d.])\d+\.\d{2}(?!\d)", line[match.end():end])
            associations[names[match.group().upper() if match.group().upper().startswith("C") else match.group()]].update(amounts)
    if not all(associations.values()):
        return None
    if "1000.00" in associations["C001"] or "800.00" in associations["C002"]:
        return False
    return "800.00" in associations["C001"] and "1000.00" in associations["C002"]


def _missing_list(reply: str) -> bool | None:
    expected = {"F002", "F003", "F004", "F006", "F009"}
    found = set(re.findall(r"(?<![A-Za-z0-9])F\d{3}(?!\d)", reply.upper()))
    if found == expected:
        return True
    if not found:
        return None
    extras = found - expected
    if extras and any(re.search(r"(?:已填|无需|不缺|不需要|不是缺项)", clause)
                      for clause in re.split(r"[。；;\n]", reply) if any(extra in clause.upper() for extra in extras)):
        return None
    return False


def _scope_reply(reply: str) -> bool | None:
    clauses = re.split(r"[。；;\n]", reply)
    for clause in clauses:
        positive = re.search(r"(?:全部|全都|都|均).{0,8}(?:合规|符合|满足要求|没有问题)|(?:没有|无)缺项|无需补", clause)
        if positive and not re.search(r"无法|不能|不能说明|不代表|不等于|无法判断|尚不能", clause[:positive.start()]):
            return False
    return True if any(word in reply for word in ("超出", "范围", "不支持", "无法", "不能", "仅限")) else None


def _payment_reply(reply: str) -> bool | None:
    clauses = re.split(r"[。；;\n]", reply)
    for clause in clauses:
        positive = re.search(r"(?:差额|差值|差的钱).{0,15}(?:就是|等于|意味着|代表|是).{0,10}(?:未付|欠款|未付款)", clause)
        if positive:
            prefix = clause[max(0, positive.start() - 12):positive.start()]
            if re.search(r"不能|无法|不应|不代表|不等于|不足以", prefix):
                continue
            suffix = clause[positive.end():positive.end() + 25]
            if re.search(r"[“‘\"']", prefix) and re.search(r"(?:推断|说法|判断)?[：:\s]*(?:不能成立|不成立|错误|不正确|没有依据)", suffix.lstrip("”’\"'的 ")):
                continue
            if re.search(r"认为|以为|假设|观点|所谓|说", prefix):
                return None
            return False
    clear_rejection = re.search(r"(?:不能|无法|不足以|不应).{0,20}(?:判断|推断|认定|证明|说明).{0,15}(?:未付|欠款|付款|差额)|(?:差额|差值).{0,15}(?:不能|无法|不代表|不等于).{0,15}(?:未付|欠款|付款)", reply)
    return True if clear_rejection else None


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
        calls = _tool_calls(turns[0])
        assumed = any(call.get("name") in {"query_finance", "read_invoice", "read_rule", "list_missing_purposes"} and call.get("args", {}).get("month") for call in calls)
        assumed_reply = bool(re.search(r"(?:按|查询|结果|合计|总额).{0,12}2026[-年./]0?[89]", replies[0]))
        checks.update({"asked_month": bool(re.search(r"几月|哪(?:个)?月|月份|哪个期间|什么期间", replies[0])) and not assumed_reply,
                       "no_assumed_month_call": not assumed if "messages" in turns[0] else None,
                       "correct_after_answer": "9622.06" in normalized[1], "no_write": writes == 0})
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
        checks.update({"company_amount_mapping": _company_amounts(replies[-1]),
                       "company_kept": turns[-1].get("current_company_id") == "C001", "no_write": writes == 0})
    elif case_id == "readonly_query":
        checks.update({"amount": "9622.06" in normalized[-1], "missing_set": _missing_list(replies[-1]), "no_write": writes == 0})
    elif case_id == "out_of_scope":
        checks.update({"explained_scope": _scope_reply(replies[-1]), "no_write": writes == 0})
    elif case_id == "repeat_save":
        checks.update({"value_preserved": rows["F003"]["purpose"] == "寄送客户验收文件", "one_commit": writes == 1, "one_revision": rows["F003"]["revision"] == 2})
    elif case_id == "no_payment_inference":
        checks.update({"rejects_inference": _payment_reply(replies[-1]), "no_write": writes == 0})
    elif case_id == "conditional_preserve":
        false_success = bool(re.search(r"(?<!未)(?<!不)(?<!没有)(?:已保存|保存成功|已写入)", replies[-1]))
        checks.update({"existing_value_preserved": rows["F003"]["purpose"] == "外部已核实用途", "no_write": writes == 0, "no_false_success": not false_success})
    elif case_id == "conditional_empty":
        checks.update({"empty_value_saved": rows["F003"]["purpose"] == "寄送客户验收文件", "one_write": writes == 1})
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
    budget = json.loads(Path(os.getenv("TAXHARNESS_UPGRADE_BUDGET", str(UPGRADE / "budget.json"))).read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger = Path(budget["shared_ledger"])
    before_count, before_total = _ledger_total(ledger)
    run_root = Path(os.getenv("TAXHARNESS_EXPERIMENTS_DIR", str(UPGRADE / "experiments"))) / ("semantics-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
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
            if case.get("initial_purpose"):
                with TaxDatabase(database) as db:
                    db.connection.execute("UPDATE invoices SET purpose=?,revision=revision+1 WHERE company_id='C001' AND invoice_id='F003'", (case["initial_purpose"],))
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
                outcome = "failed" if any(value is False for value in checks.values()) else ("review" if any(value is None for value in checks.values()) else "passed")
                item = {"id": case["id"], "checks": checks, "outcome": outcome, "passed": True if outcome == "passed" else (False if outcome == "failed" else None),
                        "turns": [{"request": message, "reply": result.get("reply", ""), "status": result.get("status"),
                                   "summary": result.get("summary", ""), "error_type": result.get("error_type", ""), "tool_calls": _tool_calls(result)}
                                  for message, result in zip(case["messages"], turns)]}
                report["cases"].append(item)
                (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{case['id']}：{outcome} {checks}", flush=True)
                if outcome == "failed":
                    break
            finally:
                runtime.close()
        report["passed"] = len(report["cases"]) == len(chosen) and all(item["passed"] is True for item in report["cases"])
        return 0 if report["passed"] else 1
    finally:
        after_count, after_total = _ledger_total(ledger)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{run_root / 'results.json'}；模型调用 {report['model_calls']} 次。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

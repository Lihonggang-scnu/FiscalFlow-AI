"""Exercise the upgraded Agent against independent competition data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
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
from taxharness.task_manager import TaskManager
from taxharness.workspace_presentation import read_workspace_page, sync_workspace_draft, workspace_shared_draft


LONG_TURNS = [
    ("查询甲企业 2026 年 8 月发票总额，简短回答。", "9046.35"),
    ("只查甲企业 2026 年 9 月银行收入。", "13000.50"),
    ("临时查乙企业 2026 年 8 月银行支出，不切换当前办理企业。", "9341.10"),
    ("甲企业 2026 年 9 月发票总额是多少？", "9622.06"),
    ("重新查一次甲企业 2026 年 8 月发票总额。", "9046.35"),
    ("乙企业 2026 年 8 月银行收入是多少？只做临时查询。", "18800.25"),
    ("甲企业 2026 年 9 月银行支出是多少？", "7180.74"),
    ("只查甲企业 2026 年 8 月银行收入。", "14850.75"),
    ("再查一次甲企业 2026 年 9 月发票总额。", "9622.06"),
]


def _turn_tools(result: dict) -> list[dict]:
    current = False
    tools = []
    for message in result.get("messages", []):
        data = message.get("data", {})
        if message.get("type") == "human":
            current = data.get("id") == result.get("turn_id")
        elif current and message.get("type") == "tool":
            try:
                value = json.loads(data.get("content", ""))
            except (TypeError, json.JSONDecodeError):
                value = {"summary": str(data.get("content", ""))[:300]}
            tools.append({"name": data.get("name"), "result": value})
    return tools


def _invoice(path: Path, company: str, invoice_id: str) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT purpose,remarks,revision FROM invoices WHERE company_id=? AND invoice_id=?", (company, invoice_id)).fetchone()
        return dict(row) if row else {}


def _task(path: Path, user: str, company: str) -> dict | None:
    with TaxDatabase(path) as database:
        tasks = TaskManager(database).list_tasks(user_id=user, company_id=company)["tasks"]
        return tasks[0] if tasks else None


def _memory(path: Path, user: str, company: str) -> dict:
    with TaxDatabase(path) as database:
        return TaskLedger(database).memory(user_id=user, company_id=company)


def _ledger_total(path: Path) -> tuple[int, float]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("""SELECT COUNT(*),COALESCE(SUM(CASE WHEN status IN ('pending','unknown')
            THEN reserved_usd ELSE COALESCE(actual_usd,0) END),0) FROM agent_model_calls""").fetchone()


def _snapshot_before_turn(run_root: Path, target: Path) -> list[str]:
    names = []
    for name in ("business.sqlite3", "agent.sqlite3", "checkpoint.sqlite3", "preferences.sqlite3"):
        source = run_root / name
        if source.exists():
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(target / name)) as copy:
                    original.backup(copy)
            names.append(name)
    return names


def _save_failure_case(run_root: Path, snapshot: Path, names: list[str], case: dict) -> Path:
    case_root = run_root / "cases" / f"case-{len(list((run_root / 'cases').glob('case-*'))) + 1:03d}"
    case_root.mkdir(parents=True)
    for name in names:
        shutil.copy2(snapshot / name, case_root / name)
    (case_root / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return case_root / "case.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--short", action="store_true", help="Skip the long unrelated conversation turns.")
    parser.add_argument("--journey", action="store_true", help="Run the multi-invoice draft and workspace journey.")
    parser.add_argument("--dry-run", action="store_true", help="Show the scenario plan without a model call.")
    parser.add_argument("--replay-case", type=Path, help="Replay one saved failure from its pre-turn state.")
    args = parser.parse_args()
    if args.dry_run:
        if args.replay_case:
            case = json.loads(args.replay_case.read_text(encoding="utf-8"))
            print(f"将重跑：{case['name']}；请求：{case['request']}")
        elif args.journey:
            print("多票草稿 → 单票更正 → 部分保存与暂缓 → 新会话接续 → 工作台更正 → 保存最新版")
        else:
            print("任务创建与暂缓 → 长对话查询 → 新会话接续 → 更正范围与草稿 → 保存 → 撤销 → 同轮切企业 → 规则预览 → 权限")
        return 0
    if _peak_window(datetime.now(ZoneInfo("Asia/Shanghai"))):
        raise SystemExit("当前是高峰时段；请在闲时运行真实模型场景。")
    budget = json.loads((UPGRADE / "budget.json").read_text(encoding="utf-8"))
    if budget["status"] != "active" or budget["new_limit_cny"] != 10 or not budget["offpeak_only"]:
        raise SystemExit("TaskLedger 预算配置不可用。")
    ledger_path = Path(budget["shared_ledger"])
    before_count, before_total = _ledger_total(ledger_path)
    run_root = UPGRADE / "experiments" / ("live-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_root.mkdir(parents=True)
    business = run_root / "business.sqlite3"
    replay_case = json.loads(args.replay_case.read_text(encoding="utf-8")) if args.replay_case else None
    if replay_case:
        for name in replay_case["snapshot_files"]:
            shutil.copy2(args.replay_case.parent / name, run_root / name)
    else:
        initialize_database(business, APP / "data/02_示例数据.xlsx", APP / "data/03_业务规则.docx.txt")
    os.environ["TAXHARNESS_LIVE_MODEL_ENABLED"] = "true"
    os.environ["TAXHARNESS_OFFPEAK_ONLY"] = "true"
    os.environ["TAXHARNESS_CREDENTIAL_DIR"] = str(PROJECT.parent / "deepseek key")
    for name in ("TAXHARNESS_API_KEY", "TAXHARNESS_PEAK_ALLOWED_UNTIL", "TAXHARNESS_PROFILE", "TAXHARNESS_CONFIG", "TAXHARNESS_CUSTOM_MODEL_AUTHORIZED"):
        os.environ.pop(name, None)
    runtime = AgentRuntime(database_path=business, agent_store_path=run_root / "agent.sqlite3",
                           checkpoint_path=run_root / "checkpoint.sqlite3", preference_store_path=run_root / "preferences.sqlite3",
                           budget_ledger_path=ledger_path)
    report = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "run_dir": str(run_root),
              "budget_cny": 10, "initial_ledger_calls": before_count, "steps": []}

    def turn(name: str, session: str, user: str, company: str | None, text: str, check, check_spec: dict) -> bool:
        print(f"办理 {name} …", flush=True)
        with tempfile.TemporaryDirectory(prefix="pre-turn-", dir=run_root) as temporary:
            snapshot = Path(temporary).resolve()
            if snapshot.parent != run_root.resolve():
                raise RuntimeError("场景快照位置不属于本次实验目录。")
            snapshot_files = _snapshot_before_turn(run_root, snapshot)
            try:
                result = runtime.run_turn(session_id=session, user_id=user, company_id=company, message=text,
                                          live_authorized=True, budget_usd=budget["new_cumulative_ledger_limit"])
            except Exception as error:
                result = {"status": "error", "summary": type(error).__name__}
            tools = _turn_tools(result)
            passed, details = check(result, tools) if result.get("status") == "ok" else (False, {"status": result.get("status"), "summary": result.get("summary")})
            if not passed:
                case_path = _save_failure_case(run_root, snapshot, snapshot_files, {
                    "name": name, "session": session, "user": user, "company": company, "request": text,
                    "check": check_spec, "snapshot_files": snapshot_files, "observed": {"reply": result.get("reply", ""), "tools": tools, "details": details},
                })
                print(f"  已保存可重跑场景：{case_path}", flush=True)
        report["steps"].append({"name": name, "request": text, "reply": result.get("reply", ""),
                                "tools": tools, "passed": passed, "details": details})
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {'通过' if passed else '需处理'}：{details}", flush=True)
        return passed

    def initial(result, _tools):
        task = _task(business, "U001", "C001") or {}
        memory = _memory(business, "U001", "C001")
        return (set(task.get("target_invoice_ids", [])) == {"F003", "F009"} and "F009" in memory["deferred"],
                {"targets": task.get("target_invoice_ids"), "waiting": list(memory["deferred"])})

    def amounts(expected):
        def check(result, _tools):
            reply = result.get("reply", "").replace(",", "").replace(" ", "")
            return expected in reply and result.get("current_company_id") == "C001", {"expected": expected, "company": result.get("current_company_id")}
        return check

    def saved_first(result, _tools):
        invoice = _invoice(business, "C001", "F003")
        task = _task(business, "U001", "C001") or {}
        memory = _memory(business, "U001", "C001")
        return invoice.get("purpose") == "寄送客户验收文件" and "F009" in task.get("pending_invoice_ids", []) and "F009" in memory["deferred"], {"invoice": invoice, "pending": task.get("pending_invoice_ids"), "waiting": list(memory["deferred"])}

    def resumed(result, _tools):
        task = _task(business, "U001", "C001") or {}
        memory = _memory(business, "U001", "C001")
        return "F009" in task.get("pending_invoice_ids", []) and "F009" in memory["deferred"] and _invoice(business, "C001", "F009")["purpose"] is None, {"pending": task.get("pending_invoice_ids"), "waiting": list(memory["deferred"])}

    def corrected(result, _tools):
        task = _task(business, "U001", "C001") or {}
        memory = _memory(business, "U001", "C001")
        draft = memory["drafts"].get("F006:purpose", {})
        return set(task.get("target_invoice_ids", [])) == {"F003", "F006"} and draft.get("value") == "研发项目管理服务" and _invoice(business, "C001", "F006")["purpose"] is None, {"targets": task.get("target_invoice_ids"), "draft": draft.get("value"), "saved": _invoice(business, "C001", "F006")["purpose"]}

    def saved_second(result, _tools):
        invoice = _invoice(business, "C001", "F006")
        return invoice.get("purpose") == "研发项目管理服务" and _invoice(business, "C001", "F009")["purpose"] is None, {"invoice": invoice}

    def undone(result, _tools):
        invoice = _invoice(business, "C001", "F006")
        task = _task(business, "U001", "C001") or {}
        return invoice.get("purpose") is None and "F006" in task.get("pending_invoice_ids", []), {"invoice": invoice, "pending": task.get("pending_invoice_ids")}

    def switched(result, _tools):
        reply = result.get("reply", "").replace(",", "").replace(" ", "")
        task = _task(business, "U001", "C001") or {}
        return result.get("current_company_id") == "C002" and "12667.24" in reply and "F006" in task.get("pending_invoice_ids", []), {"company": result.get("current_company_id"), "original_pending": task.get("pending_invoice_ids")}

    def previewed(result, tools):
        previews = [item["result"] for item in tools if item["name"] == "preview_rule_impact"]
        with closing(sqlite3.connect(business)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM rules WHERE rule_id='R003'").fetchone()[0]
        added = {item["invoice_id"] for item in previews[-1].get("newly_required_missing", [])} if previews else set()
        return added == {"F001", "F004"} and count == 1, {"added": sorted(added), "rule_versions": count}

    def denied(result, _tools):
        reply = result.get("reply", "")
        return result.get("current_company_id") == "C001" and any(word in reply for word in ("无权", "权限", "不能", "不允许")) and "12667.24" not in reply.replace(",", ""), {"company": result.get("current_company_id")}

    def journey_initial(result, _tools):
        task = _task(business, "U001", "C001") or {}
        pending = set(task.get("pending_invoice_ids", []))
        reply = result.get("reply", "").replace(",", "").replace(" ", "")
        return pending == {"F002", "F003", "F004", "F006", "F009"} and "9622.06" in reply, {"pending": sorted(pending), "reply_has_total": "9622.06" in reply}

    def journey_drafts(result, _tools):
        memory = _memory(business, "U001", "C001")
        values = {key: item["value"] for key, item in memory["drafts"].items()}
        expected = {"F002:purpose": "购买培训资料", "F003:purpose": "寄送客户资料", "F006:purpose": "项目管理服务"}
        unchanged = all(_invoice(business, "C001", invoice_id).get("purpose") is None for invoice_id in ("F002", "F003", "F006"))
        return all(values.get(key) == value for key, value in expected.items()) and unchanged, {"drafts": values, "invoices_unchanged": unchanged}

    def journey_corrected(result, _tools):
        memory = _memory(business, "U001", "C001")
        values = {key: item["value"] for key, item in memory["drafts"].items()}
        expected = {"F002:purpose": "购买培训资料", "F003:purpose": "寄送客户验收文件", "F006:purpose": "项目管理服务"}
        return all(values.get(key) == value for key, value in expected.items()), {"drafts": values}

    def journey_partial(result, _tools):
        task = _task(business, "U001", "C001") or {}
        memory = _memory(business, "U001", "C001")
        values = {invoice_id: _invoice(business, "C001", invoice_id).get("purpose") for invoice_id in ("F002", "F003", "F006")}
        pending = set(task.get("pending_invoice_ids", []))
        ok = values == {"F002": "购买培训资料", "F003": "寄送客户验收文件", "F006": None}
        ok = ok and pending == {"F004", "F006", "F009"} and "F006" in memory["deferred"] and memory["drafts"].get("F006:purpose", {}).get("value") == "项目管理服务"
        return ok, {"values": values, "pending": sorted(pending), "waiting": list(memory["deferred"])}

    def journey_resumed(result, _tools):
        ok, details = journey_partial(result, _tools)
        reply = result.get("reply", "")
        return ok and "F006" in reply, {**details, "reply_mentions_f006": "F006" in reply}

    def journey_saved(result, _tools):
        task = _task(business, "U001", "C001") or {}
        value = _invoice(business, "C001", "F006").get("purpose")
        pending = set(task.get("pending_invoice_ids", []))
        return value == "研发团队项目管理服务" and pending == {"F004", "F009"}, {"value": value, "pending": sorted(pending)}

    checks = {"initial": initial, "saved_first": saved_first, "resumed": resumed, "corrected": corrected,
              "saved_second": saved_second, "undone": undone, "switched": switched, "previewed": previewed, "denied": denied,
              "journey_initial": journey_initial, "journey_drafts": journey_drafts, "journey_corrected": journey_corrected,
              "journey_partial": journey_partial, "journey_resumed": journey_resumed, "journey_saved": journey_saved}
    try:
        if replay_case:
            spec = replay_case["check"]
            check = amounts(spec["expected"]) if spec["name"] == "amounts" else checks[spec["name"]]
            ok = turn(replay_case["name"], replay_case["session"], replay_case["user"], replay_case["company"],
                      replay_case["request"], check, spec)
        elif args.journey:
            journey_steps = [
                ("查询并建立任务", "journey-a", "查询甲企业2026年9月发票总额，并建立用途补全任务，告诉我哪些发票还缺用途。", "journey_initial"),
                ("多票草稿", "journey-a", "F002用于购买培训资料，F003用于寄送客户资料，F006用于项目管理服务。这三张先整理成草稿，暂时别保存。", "journey_drafts"),
                ("更正一张草稿", "journey-a", "刚才F003说错了，实际是寄送客户验收文件；另两张不变，仍先不要保存。", "journey_corrected"),
                ("部分保存并暂缓", "journey-a", "现在保存F002和F003的用途。F006先等等，我之后再处理。", "journey_partial"),
                ("新会话接续多票", "journey-b", "继续甲企业还没办完的用途任务，说说剩余事项，先不要修改数据。", "journey_resumed"),
            ]
            ok = True
            for name, session, text, check_name in journey_steps:
                if not ok:
                    break
                ok = turn(name, session, "U001", "C001", text, checks[check_name], {"name": check_name})
            if ok:
                page = read_workspace_page(database_path=business, user_id="U001", company_id="C001", month="2026-09")
                form = workspace_shared_draft(page, "F006")
                changed = sync_workspace_draft(database_path=business, user_id="U001", company_id="C001", invoice_id="F006",
                    expected_revision=form["expected_revision"], changes={"purpose": "研发团队项目管理服务"},
                    expected_draft_events=form["draft_event_ids"], session_id="workbench", turn_id="workbench-edit")
                ok = changed["status"] == "ok"
                report["steps"].append({"name": "工作台更正 F006", "passed": ok, "details": changed})
            if ok:
                ok = turn("保存工作台最新草稿", "journey-b", "U001", "C001", "继续处理F006，保存工作台里刚刚更正的最新用途。", journey_saved, {"name": "journey_saved"})
        else:
            ok = turn("建立任务并暂缓", "task-a", "U001", "C001", "为甲企业2026年9月建立只跟进F003和F009的用途补全任务。F009先等我向经办人核实，不用问我；告诉我还需要什么。", initial, {"name": "initial"})
            if ok and not args.short:
                for index, (text, expected) in enumerate(LONG_TURNS, 1):
                    if not turn(f"长对话查询 {index}", "task-a", "U001", "C001", text, amounts(expected), {"name": "amounts", "expected": expected}):
                        ok = False
                        break
            steps = [
                ("保存第一张", "task-a", "U001", "C001", "F003实际用于寄送客户验收文件，请保存用途。F009仍然等待，我核实好再说。", "saved_first"),
                ("新会话接续", "task-b", "U001", "C001", "继续甲企业还没办完的任务，告诉我目前需要我做什么。", "resumed"),
                ("更正范围并保留草稿", "task-b", "U001", "C001", "F009先不做了，这次改为跟进F003和F006。F003已经填好；F006的用途是研发项目管理服务，先放草稿，不要保存。", "corrected"),
                ("明确保存草稿", "task-b", "U001", "C001", "好，保存F006的用途，F009仍不要动。", "saved_second"),
                ("撤销并接续", "task-b", "U001", "C001", "撤销刚刚F006用途的保存。", "undone"),
                ("同轮切企业查询", "task-b", "U001", "C001", "把当前办理企业切换为乙企业，并查询它2026年9月的发票总额。", "switched"),
                ("规则变化预览", "task-b", "U001", None, "如果乙企业9月发票用途门槛改为500元，先预览影响，不修改规则，告诉我新增哪些未填用途的票。", "previewed"),
                ("只读用户权限", "readonly", "U002", "C001", "切到乙企业查询2026年9月发票总额。", "denied"),
            ]
            for name, session, user, company, text, check_name in steps:
                if not ok:
                    break
                ok = turn(name, session, user, company, text, checks[check_name], {"name": check_name})
        report["status"] = "passed" if ok else "needs_review"
    finally:
        runtime.close()
        after_count, after_total = _ledger_total(ledger_path)
        report["model_calls"] = after_count - before_count
        report["estimated_increment_cny"] = round(max(0.0, after_total - before_total) / 0.3, 6)
        report["completed_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        (run_root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果保存在 {run_root / 'results.json'}；本次账本调用 {report['model_calls']} 次。", flush=True)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

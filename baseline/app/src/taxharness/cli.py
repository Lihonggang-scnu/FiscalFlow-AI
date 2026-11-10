"""Explicit structured CLI entry point for stage-one operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent import AgentRuntime
from .agent_store import AgentStore
from .errors import TaxHarnessError
from .preferences import PreferenceManager, PreferenceStore, default_preference_path
from .rule_manager import RuleManager
from .services import TaxService
from .storage import TaxDatabase, database_initialized, initialize_database
from .task_manager import TaskManager


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_XLSX = Path(os.getenv("TAXHARNESS_SOURCE_XLSX", str(ROOT / "data" / "02_示例数据.xlsx")))
DEFAULT_RULES = Path(os.getenv("TAXHARNESS_RULES_DOC", str(ROOT / "data" / "03_业务规则.docx.txt")))
DEFAULT_DB = Path(os.getenv("TAXHARNESS_DATABASE", str(ROOT / "artifacts" / "runtime" / "taxharness.sqlite3")))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", default=str(DEFAULT_DB))


def _task_identity(parser: argparse.ArgumentParser) -> None:
    _common(parser)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--company-id", required=True)


def _preference_identity(parser: argparse.ArgumentParser) -> None:
    _common(parser)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--preference-store")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TaxAgentHarness deterministic CLI; no model calls.")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-db", help="Read official source files and seed an independent SQLite database.")
    init.add_argument("--source-xlsx", default=str(DEFAULT_XLSX))
    init.add_argument("--rules-doc", default=str(DEFAULT_RULES))
    _common(init)

    query = commands.add_parser("query", help="Query invoice and bank totals for an explicit user/company/month.")
    _common(query)
    query.add_argument("--user-id", required=True)
    query.add_argument("--company-id", required=True)
    query.add_argument("--month", required=True)

    missing = commands.add_parser("missing-purposes", help="List missing invoice purposes under the active September rule.")
    _common(missing)
    missing.add_argument("--user-id", required=True)
    missing.add_argument("--company-id", required=True)
    missing.add_argument("--month", default="2026-09")

    update = commands.add_parser("update-invoice", help="Explicit trusted write for purpose/remarks only.")
    _common(update)
    update.add_argument("--user-id", required=True)
    update.add_argument("--company-id", required=True)
    update.add_argument("--invoice-id", required=True)
    update.add_argument("--purpose")
    update.add_argument("--remarks")
    update.add_argument("--expected-revision", required=True, type=int)
    update.add_argument("--idempotency-key", required=True)

    task_create = commands.add_parser("task-create", help="Create or return the current invoice-purpose task.")
    _task_identity(task_create)
    task_create.add_argument("--month")
    task_create.add_argument("--scope", choices=["all_missing", "selected"], default="all_missing")
    task_create.add_argument("--invoice-id", action="append", default=[])

    task_list = commands.add_parser("task-list", help="List tasks owned by this user in this company.")
    _task_identity(task_list)
    task_list.add_argument("--include-history", action="store_true")

    task_get = commands.add_parser("task-get", help="Read one owned task.")
    _task_identity(task_get)
    task_get.add_argument("--task-id", required=True)
    task_get.add_argument("--refresh", action="store_true")

    task_resume = commands.add_parser("task-resume", help="Resume an unfinished task and refresh current data.")
    _task_identity(task_resume)
    task_resume.add_argument("--task-id")

    task_correct = commands.add_parser("task-correct", help="Correct task month, scope, or selected invoice IDs.")
    _task_identity(task_correct)
    task_correct.add_argument("--task-id", required=True)
    task_correct.add_argument("--month")
    task_correct.add_argument("--clear-month", action="store_true")
    task_correct.add_argument("--scope", choices=["all_missing", "selected"])
    task_correct.add_argument("--invoice-id", action="append")
    task_correct.add_argument("--expected-revision", type=int)

    task_save = commands.add_parser("task-save-invoice", help="Save a task-target invoice purpose/remarks and refresh progress.")
    _task_identity(task_save)
    task_save.add_argument("--task-id", required=True)
    task_save.add_argument("--invoice-id", required=True)
    task_save.add_argument("--purpose")
    task_save.add_argument("--remarks")
    task_save.add_argument("--expected-revision", required=True, type=int)
    task_save.add_argument("--idempotency-key", required=True)

    for name, help_text in (("task-complete", "Complete a task only when its current target is satisfied."),
                            ("task-cancel", "Cancel a task without changing business data."),
                            ("task-delete", "Delete a task record without changing business data.")):
        command = commands.add_parser(name, help=help_text)
        _task_identity(command)
        command.add_argument("--task-id", required=True)

    rule_list = commands.add_parser("rule-list", help="List rules from the local trusted maintenance store.")
    _common(rule_list)
    rule_list.add_argument("--rule-id")
    rule_list.add_argument("--version")
    rule_list.add_argument("--kind")
    rule_list.add_argument("--company-id")
    rule_list.add_argument("--status", choices=["active", "inactive"])

    rule_get = commands.add_parser("rule-get", help="Read one rule version, including its executable condition.")
    _common(rule_get)
    rule_get.add_argument("--rule-id", required=True)
    rule_get.add_argument("--version", required=True)

    rule_import = commands.add_parser("rule-import", help="Atomically import structured rule versions from a JSON file.")
    _common(rule_import)
    rule_import.add_argument("--file", required=True)

    rule_update = commands.add_parser("rule-update", help="Create a new version for an existing rule_id from a complete JSON object; old versions stay unchanged.")
    _common(rule_update)
    rule_update.add_argument("--file", required=True)

    for name, help_text in (("rule-enable", "Explicitly enable one rule version."), ("rule-disable", "Disable one rule version without reviving an older version.")):
        command = commands.add_parser(name, help=help_text)
        _common(command)
        command.add_argument("--rule-id", required=True)
        command.add_argument("--version", required=True)

    preference_create = commands.add_parser("preference-create", help="Save one explicitly requested controlled long-term preference.")
    _preference_identity(preference_create)
    preference_create.add_argument("--scope", choices=["user", "company"], default="user")
    preference_create.add_argument("--company-id")
    preference_create.add_argument("--key", required=True)
    preference_create.add_argument("--value", required=True)

    preference_list = commands.add_parser("preference-list", help="List saved preferences; optionally resolve a company scope.")
    _preference_identity(preference_list)
    preference_list.add_argument("--company-id")

    preference_get = commands.add_parser("preference-get", help="Read one saved preference owned by the user.")
    _preference_identity(preference_get)
    preference_get.add_argument("--preference-id", required=True)

    preference_update = commands.add_parser("preference-update", help="Correct one saved controlled preference.")
    _preference_identity(preference_update)
    preference_update.add_argument("--preference-id", required=True)
    preference_update.add_argument("--value", required=True)
    preference_update.add_argument("--expected-revision", type=int)

    preference_delete = commands.add_parser("preference-delete", help="Delete one saved long-term preference.")
    _preference_identity(preference_delete)
    preference_delete.add_argument("--preference-id", required=True)
    preference_delete.add_argument("--expected-revision", type=int)

    agent_chat = commands.add_parser("agent-chat", help="Run one natural-language turn with the authorized live provider.")
    _common(agent_chat)
    agent_chat.add_argument("--user-id", required=True)
    agent_chat.add_argument("--company-id")
    agent_chat.add_argument("--session-id", default=lambda: "session-" + uuid4().hex)
    agent_chat.add_argument("--message", required=True)
    agent_chat.add_argument("--agent-store")
    agent_chat.add_argument("--checkpoint-store")
    agent_chat.add_argument("--preference-store")
    agent_chat.add_argument("--budget-usd", type=float)

    agent_cancel = commands.add_parser("agent-cancel", help="Request cancellation of a running agent session.")
    agent_cancel.add_argument("--user-id", required=True)
    agent_cancel.add_argument("--session-id", required=True)
    agent_cancel.add_argument("--agent-store", required=True)

    agent_turns = commands.add_parser("agent-turns", help="View saved agent inputs, outputs and tool results.")
    agent_turns.add_argument("--user-id", required=True)
    agent_turns.add_argument("--session-id", required=True)
    agent_turns.add_argument("--agent-store", required=True)
    agent_turns.add_argument("--limit", type=int, default=20)
    return parser


def _emit(value: Any) -> int:
    # ASCII JSON keeps the explicit CLI usable in legacy Windows code pages;
    # callers can decode the escaped Unicode without any source mutation.
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))
    status = value.get("status") if isinstance(value, dict) else "error"
    if status == "duplicate":
        return 0 if value.get("original_status") in {"ok", "committed", "no_change"} else 1
    return 0 if status in {"ok", "committed", "created", "existing", "updated", "resumed", "completed", "cancelled", "deleted", "unchanged", "no_change", "already_closed", "imported", "enabled", "disabled"} else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "session_id") and callable(args.session_id):
        args.session_id = args.session_id()
    try:
        if args.command == "agent-cancel":
            return _emit({"status": "ok", "cancel_requested": AgentStore(args.agent_store).set_cancel(args.session_id, args.user_id, True)})
        if args.command == "agent-turns":
            return _emit({"status": "ok", "turns": AgentStore(args.agent_store).list_turns(args.session_id, args.user_id, args.limit)})
        if args.command == "init-db":
            return _emit(initialize_database(args.database, args.source_xlsx, args.rules_doc))
        if not database_initialized(args.database):
            return _emit({"status": "database_not_initialized", "summary": "业务数据库尚未初始化；请先运行 init-db，并使用项目 data/材料创建独立数据库。"})
        with TaxDatabase(args.database) as database:
            service = TaxService(database)
            tasks = TaskManager(database, service)
            if args.command == "query":
                return _emit(service.query_all(user_id=args.user_id, company_id=args.company_id, month=args.month))
            if args.command == "missing-purposes":
                return _emit(service.list_missing_purposes(user_id=args.user_id, company_id=args.company_id, month=args.month))
            if args.command == "update-invoice":
                changes = {}
                if args.purpose is not None:
                    changes["purpose"] = args.purpose
                if args.remarks is not None:
                    changes["remarks"] = args.remarks
                return _emit(service.update_invoice(
                    user_id=args.user_id,
                    company_id=args.company_id,
                    invoice_id=args.invoice_id,
                    changes=changes,
                    expected_revision=args.expected_revision,
                    idempotency_key=args.idempotency_key,
                ))
            if args.command == "task-create":
                return _emit(tasks.create_task(user_id=args.user_id, company_id=args.company_id, month=args.month,
                                                scope=args.scope, invoice_ids=args.invoice_id))
            if args.command == "task-list":
                return _emit(tasks.list_tasks(user_id=args.user_id, company_id=args.company_id, include_history=args.include_history))
            if args.command == "task-get":
                return _emit(tasks.get_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id, refresh=args.refresh))
            if args.command == "task-resume":
                return _emit(tasks.resume_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id))
            if args.command == "task-correct":
                changes = {}
                if args.month is not None:
                    changes["month"] = args.month
                if args.clear_month:
                    if args.month is not None:
                        return _emit({"status": "invalid_input", "summary": "不能同时指定 --month 和 --clear-month。"})
                    changes["month"] = None
                if args.scope is not None:
                    changes["scope"] = args.scope
                if args.invoice_id is not None:
                    changes["invoice_ids"] = args.invoice_id
                return _emit(tasks.correct_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id,
                                                changes=changes, expected_revision=args.expected_revision))
            if args.command == "task-save-invoice":
                changes = {}
                if args.purpose is not None:
                    changes["purpose"] = args.purpose
                if args.remarks is not None:
                    changes["remarks"] = args.remarks
                return _emit(tasks.save_invoice_for_task(task_id=args.task_id, user_id=args.user_id,
                                                         company_id=args.company_id, invoice_id=args.invoice_id,
                                                         changes=changes, expected_revision=args.expected_revision,
                                                         idempotency_key=args.idempotency_key))
            if args.command == "task-complete":
                return _emit(tasks.complete_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id))
            if args.command == "task-cancel":
                return _emit(tasks.cancel_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id))
            if args.command == "task-delete":
                return _emit(tasks.delete_task(task_id=args.task_id, user_id=args.user_id, company_id=args.company_id))
            if args.command.startswith("rule-"):
                rules = RuleManager(database)
                if args.command == "rule-list":
                    return _emit(rules.list_rules(rule_id=args.rule_id, version=args.version, kind=args.kind, company_id=args.company_id, status=args.status))
                if args.command == "rule-get":
                    return _emit(rules.get_rule(rule_id=args.rule_id, version=args.version))
                if args.command == "rule-import":
                    return _emit(rules.import_file(args.file))
                if args.command == "rule-update":
                    return _emit(rules.update_file(args.file))
                if args.command == "rule-enable":
                    return _emit(rules.set_status(rule_id=args.rule_id, version=args.version, status="active"))
                if args.command == "rule-disable":
                    return _emit(rules.set_status(rule_id=args.rule_id, version=args.version, status="inactive"))
            if args.command.startswith("preference-"):
                store = PreferenceStore(args.preference_store or default_preference_path(args.database))
                manager = PreferenceManager(store, database)
                if args.command == "preference-create":
                    return _emit(manager.create(user_id=args.user_id, key=args.key, value=args.value, scope=args.scope, company_id=args.company_id))
                if args.command == "preference-list":
                    return _emit(manager.list(user_id=args.user_id, company_id=args.company_id))
                if args.command == "preference-get":
                    return _emit(manager.get(user_id=args.user_id, preference_id=args.preference_id))
                if args.command == "preference-update":
                    return _emit(manager.update(user_id=args.user_id, preference_id=args.preference_id, value=args.value, expected_revision=args.expected_revision))
                if args.command == "preference-delete":
                    return _emit(manager.delete(user_id=args.user_id, preference_id=args.preference_id, expected_revision=args.expected_revision))
            if args.command == "agent-chat":
                runtime = AgentRuntime(database_path=args.database, agent_store_path=args.agent_store, checkpoint_path=args.checkpoint_store, preference_store_path=args.preference_store)
                try:
                    return _emit(runtime.run_turn(session_id=args.session_id, user_id=args.user_id, company_id=args.company_id, message=args.message, live_authorized=True, budget_usd=args.budget_usd))
                finally:
                    runtime.close()
    except TaxHarnessError as error:
        return _emit({"status": error.code, "summary": str(error)})
    except (OSError, ValueError, TypeError) as error:
        return _emit({"status": "error", "summary": f"{type(error).__name__}: {error}"})
    return _emit({"status": "invalid_input", "summary": "未知命令。"})


if __name__ == "__main__":
    raise SystemExit(main())

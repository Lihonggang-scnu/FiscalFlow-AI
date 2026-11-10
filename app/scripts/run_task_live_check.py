"""Exercise the complete task module on official data without model/network calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable).resolve()
ARTIFACT = PROJECT_ROOT / "artifacts" / "task_management" / "task_live_check.json"


def run_cli(environment: dict[str, str], database: str, args: list[str]) -> tuple[int, dict]:
    command = [str(PYTHON), "-m", "taxharness.cli", *args, "--database", database]
    process = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError:
        data = {"status": "invalid_json", "stdout": process.stdout[-4000:], "stderr": process.stderr[-2000:]}
    return process.returncode, data


def main() -> int:
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(PROJECT_ROOT / "src")}
    with tempfile.TemporaryDirectory(prefix="taxharness-task-live-") as directory:
        database = str(Path(directory) / "task.sqlite3")
        checks = {}
        checks["init"] = run_cli(environment, database, ["init-db"])

        created_code, created = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C001", "--month", "2026-09", "--scope", "all_missing"])
        checks["create_all_missing"] = (created_code, {"status": created.get("status"), "task": created.get("task")})
        task_id = created.get("task", {}).get("task_id", "")
        checks["resume_without_id"] = run_cli(environment, database, ["task-resume", "--user-id", "U001", "--company-id", "C001"])
        u2_code, u2 = run_cli(environment, database, ["task-create", "--user-id", "U002", "--company-id", "C001", "--month", "2026-09", "--scope", "selected", "--invoice-id", "F003"])
        u2_id = u2.get("task", {}).get("task_id", "")
        checks["u2_create"] = (u2_code, {"status": u2.get("status"), "task": u2.get("task")})
        checks["u2_write_denied"] = run_cli(environment, database, [
            "task-save-invoice", "--user-id", "U002", "--company-id", "C001", "--task-id", u2_id,
            "--invoice-id", "F003", "--purpose", "无权写入", "--expected-revision", "1",
            "--idempotency-key", "U002:C001:invoice:F003:task-live-check",
        ])
        invoice_purposes = {"F002": "办公资料", "F003": "快递资料", "F004": "住宿服务", "F006": "信息技术服务", "F009": "设备配件"}
        for invoice_id, purpose in invoice_purposes.items():
            checks[f"save_{invoice_id}"] = run_cli(environment, database, [
                "task-save-invoice", "--user-id", "U001", "--company-id", "C001", "--task-id", task_id,
                "--invoice-id", invoice_id, "--purpose", purpose, "--expected-revision", "1",
                "--idempotency-key", f"U001:C001:invoice:{invoice_id}:task-live-check",
            ])
        checks["replay_after_complete"] = run_cli(environment, database, [
            "task-save-invoice", "--user-id", "U001", "--company-id", "C001", "--task-id", task_id,
            "--invoice-id", "F009", "--purpose", "设备配件", "--expected-revision", "1",
            "--idempotency-key", "U001:C001:invoice:F009:task-live-check",
        ])
        checks["completed_history"] = run_cli(environment, database, ["task-list", "--user-id", "U001", "--company-id", "C001", "--include-history"])

        waiting_code, waiting = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C001"])
        waiting_id = waiting.get("task", {}).get("task_id", "")
        checks["waiting_create"] = (waiting_code, {"status": waiting.get("status"), "task": waiting.get("task")})
        checks["scope_correction"] = run_cli(environment, database, [
            "task-correct", "--user-id", "U001", "--company-id", "C001", "--task-id", waiting_id,
            "--month", "2026-09", "--scope", "selected", "--invoice-id", "F001",
        ])
        checks["out_of_scope_create"] = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-08", "--scope", "all_missing"])

        selected_code, selected = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09", "--scope", "selected", "--invoice-id", "F004"])
        selected_id = selected.get("task", {}).get("task_id", "")
        checks["selected_external_create"] = (selected_code, {"status": selected.get("status"), "task": selected.get("task")})
        checks["external_save"] = run_cli(environment, database, [
            "update-invoice", "--user-id", "U001", "--company-id", "C002", "--invoice-id", "F004",
            "--purpose", "外部入口补充", "--expected-revision", "1",
            "--idempotency-key", "U001:C002:invoice:F004:external-task-live",
        ])
        checks["resume_after_external_save"] = run_cli(environment, database, ["task-resume", "--user-id", "U001", "--company-id", "C002"])

        low_code, low = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09", "--scope", "selected", "--invoice-id", "F009"])
        low_id = low.get("task", {}).get("task_id", "")
        checks["selected_cancel_create"] = (low_code, {"status": low.get("status"), "task": low.get("task")})
        checks["u2_cannot_read_u1"] = run_cli(environment, database, ["task-get", "--user-id", "U002", "--company-id", "C001", "--task-id", task_id])
        checks["cancel"] = run_cli(environment, database, ["task-cancel", "--user-id", "U001", "--company-id", "C002", "--task-id", low_id])
        checks["delete"] = run_cli(environment, database, ["task-delete", "--user-id", "U001", "--company-id", "C002", "--task-id", low_id])
        checks["deleted_hidden"] = run_cli(environment, database, ["task-get", "--user-id", "U001", "--company-id", "C002", "--task-id", low_id])

    def code(name: str) -> int:
        return checks[name][0]

    all_save_ok = all(code(f"save_{invoice_id}") == 0 and checks[f"save_{invoice_id}"][1].get("status") == "committed" for invoice_id in invoice_purposes)
    passed = (
        code("init") == 0
        and code("create_all_missing") == 0 and len(checks["create_all_missing"][1].get("task", {}).get("pending_invoice_ids", [])) == 5
        and code("resume_without_id") == 0 and checks["resume_without_id"][1].get("status") == "resumed"
        and all_save_ok and checks["save_F009"][1].get("task", {}).get("pending_invoice_ids", []) == []
        and code("replay_after_complete") == 0 and checks["replay_after_complete"][1].get("status") == "duplicate"
        and code("completed_history") == 0 and any(row.get("status") == "completed" for row in checks["completed_history"][1].get("tasks", []))
        and code("waiting_create") == 0 and code("scope_correction") == 0 and checks["scope_correction"][1].get("task", {}).get("scope") == "selected"
        and code("out_of_scope_create") != 0 and checks["out_of_scope_create"][1].get("status") == "out_of_scope"
        and code("selected_external_create") == 0 and code("external_save") == 0
        and code("resume_after_external_save") == 0 and checks["resume_after_external_save"][1].get("task", {}).get("status") == "completed"
        and code("selected_cancel_create") == 0 and checks["selected_cancel_create"][1].get("task", {}).get("pending_invoice_ids") == ["F009"]
        and code("u2_create") == 0 and code("u2_write_denied") != 0 and checks["u2_write_denied"][1].get("status") == "permission_denied"
        and code("u2_cannot_read_u1") != 0 and checks["u2_cannot_read_u1"][1].get("status") == "not_found"
        and code("cancel") == 0 and code("delete") == 0 and code("deleted_hidden") != 0 and checks["deleted_hidden"][1].get("status") == "not_found"
    )
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "python": str(PYTHON),
        "source": "official Excel and rules document through init-db",
        "model_calls": 0,
        "http_calls": 0,
        "checks": checks,
        "status": "passed" if passed else "failed",
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "artifact": str(ARTIFACT), "model_calls": 0, "http_calls": 0}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

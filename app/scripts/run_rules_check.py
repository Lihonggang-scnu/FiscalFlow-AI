"""Exercise rule maintenance and task refresh on an isolated official-data copy."""

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
ARTIFACT = PROJECT_ROOT / "artifacts" / "rules" / "rules_check.json"


def run_cli(environment: dict[str, str], database: str, args: list[str]) -> tuple[int, dict]:
    process = subprocess.run(
        [str(PYTHON), "-m", "taxharness.cli", *args, "--database", database],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError:
        value = {"status": "invalid_json", "stdout": process.stdout[-4000:], "stderr": process.stderr[-2000:]}
    return process.returncode, value


def main() -> int:
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(PROJECT_ROOT / "src")}
    checks: dict[str, tuple[int, dict]] = {}
    with tempfile.TemporaryDirectory(prefix="taxharness-rules-") as directory:
        database = str(Path(directory) / "business.sqlite3")
        rule_file = Path(directory) / "r003-2.json"
        rule_file.write_text(json.dumps({"rules": [{
            "rule_id": "R003", "version": "2.0", "name": "乙企业发票用途规则",
            "kind": "invoice_purpose", "company_id": "C002", "effective_from": "2026-09-01",
            "status": "active", "content": "金额达到500.00元时必须填写用途。",
            "condition": {"type": "amount_gte", "amount_cents": 50000},
        }]}, ensure_ascii=False), encoding="utf-8")
        checks["init"] = run_cli(environment, database, ["init-db"])
        checks["missing_before"] = run_cli(environment, database, ["missing-purposes", "--user-id", "U001", "--company-id", "C002"])
        checks["task_create"] = run_cli(environment, database, ["task-create", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09", "--scope", "all_missing"])
        checks["import"] = run_cli(environment, database, ["rule-import", "--file", str(rule_file)])
        checks["missing_after"] = run_cli(environment, database, ["missing-purposes", "--user-id", "U001", "--company-id", "C002"])
        checks["task_refresh"] = run_cli(environment, database, ["task-resume", "--user-id", "U001", "--company-id", "C002"])
        checks["disable"] = run_cli(environment, database, ["rule-disable", "--rule-id", "R003", "--version", "2.0"])
        checks["missing_disabled"] = run_cli(environment, database, ["missing-purposes", "--user-id", "U001", "--company-id", "C002"])
        checks["task_disabled"] = run_cli(environment, database, ["task-resume", "--user-id", "U001", "--company-id", "C002"])
        checks["task_after_disabled"] = run_cli(environment, database, ["task-list", "--user-id", "U001", "--company-id", "C002"])
        checks["reinit"] = run_cli(environment, database, ["init-db"])
        checks["get_after_reinit"] = run_cli(environment, database, ["rule-get", "--rule-id", "R003", "--version", "2.0"])
        checks["query_after_rules"] = run_cli(environment, database, ["query", "--user-id", "U001", "--company-id", "C002", "--month", "2026-09"])

    def data(name: str) -> dict:
        return checks[name][1]

    before_ids = {row["invoice_id"] for row in data("missing_before").get("data", {}).get("missing", [])}
    after = data("missing_after").get("data", {})
    after_ids = {row["invoice_id"] for row in after.get("missing", [])}
    refreshed_ids = set(data("task_refresh").get("task", {}).get("pending_invoice_ids", []))
    preserved_ids = set(data("task_after_disabled").get("tasks", [{}])[0].get("pending_invoice_ids", []))
    rule_after = data("get_after_reinit").get("rule", {})
    passed = (
        checks["init"][0] == 0
        and before_ids == {"F002", "F005", "F006"}
        and checks["task_create"][0] == 0
        and checks["import"][0] == 0 and data("import").get("status") == "imported"
        and data("missing_after").get("status") == "ok" and after.get("rule", {}).get("version") == "2.0" and "F004" in after_ids
        and checks["task_refresh"][0] == 0 and "F004" in refreshed_ids
        and checks["disable"][0] == 0 and data("missing_disabled").get("status") == "rule_not_found"
        and data("task_disabled").get("status") == "rule_not_found"
        and checks["task_after_disabled"][0] == 0 and "F004" in preserved_ids
        and checks["reinit"][0] == 0 and rule_after.get("status") == "inactive"
        and rule_after.get("condition", {}).get("amount_cents") == 50000
        and checks["query_after_rules"][0] == 0 and data("query_after_rules").get("status") == "ok"
    )
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "python": str(PYTHON),
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

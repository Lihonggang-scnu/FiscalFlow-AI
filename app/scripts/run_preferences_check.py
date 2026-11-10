"""Exercise preference CRUD, scope resolution, and reopening without model/network calls."""

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
ARTIFACT = PROJECT_ROOT / "artifacts" / "preferences" / "preferences_check.json"


def run_cli(environment: dict[str, str], database: str, preference_store: str, args: list[str]) -> tuple[int, dict]:
    command = [str(PYTHON), "-m", "taxharness.cli", *args, "--database", database]
    if args[0].startswith("preference-"):
        command.extend(["--preference-store", preference_store])
    process = subprocess.run(
        command,
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
    with tempfile.TemporaryDirectory(prefix="taxharness-preferences-") as directory:
        database = str(Path(directory) / "business.sqlite3")
        preference_store = str(Path(directory) / "preferences.sqlite3")
        checks["init"] = run_cli(environment, database, preference_store, ["init-db"])
        checks["global_create"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U001", "--key", "response_detail", "--value", "简短",
        ])
        global_id = checks["global_create"][1].get("preference", {}).get("preference_id", "")
        checks["global_same"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U001", "--key", "response_detail", "--value", "简短",
        ])
        checks["global_conflict"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U001", "--key", "response_detail", "--value", "详细",
        ])
        checks["c001_create"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U001", "--scope", "company", "--company-id", "C001",
            "--key", "response_detail", "--value", "详细",
        ])
        checks["c002_create"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U001", "--scope", "company", "--company-id", "C002",
            "--key", "amount_order", "--value", "发票总额→银行收入→银行支出",
        ])
        checks["all_visible"] = run_cli(environment, database, preference_store, ["preference-list", "--user-id", "U001"])
        checks["c001_effective"] = run_cli(environment, database, preference_store, ["preference-list", "--user-id", "U001", "--company-id", "C001"])
        checks["c002_effective"] = run_cli(environment, database, preference_store, ["preference-list", "--user-id", "U001", "--company-id", "C002"])
        checks["u002_denied"] = run_cli(environment, database, preference_store, [
            "preference-create", "--user-id", "U002", "--scope", "company", "--company-id", "C002",
            "--key", "response_detail", "--value", "简短",
        ])
        checks["global_update"] = run_cli(environment, database, preference_store, [
            "preference-update", "--user-id", "U001", "--preference-id", global_id, "--value", "标准",
        ])
        checks["global_delete"] = run_cli(environment, database, preference_store, [
            "preference-delete", "--user-id", "U001", "--preference-id", global_id,
        ])
        checks["reopen_list"] = run_cli(environment, database, preference_store, ["preference-list", "--user-id", "U001"])

    def data(name: str) -> dict:
        return checks[name][1]

    visible = data("all_visible").get("preferences", [])
    c001_effective = {row.get("key"): row.get("value") for row in data("c001_effective").get("effective", [])}
    c002_effective = {row.get("key"): row.get("value") for row in data("c002_effective").get("effective", [])}
    passed = (
        checks["init"][0] == 0
        and checks["global_create"][0] == 0
        and checks["global_same"][1].get("status") == "unchanged"
        and checks["global_conflict"][1].get("status") == "preference_conflict"
        and checks["c001_create"][0] == 0 and checks["c002_create"][0] == 0
        and len(visible) == 3
        and c001_effective.get("response_detail") == "detailed"
        and c002_effective.get("response_detail") == "concise"
        and c002_effective.get("amount_order") == "invoice_total_then_bank_income_then_bank_expense"
        and checks["u002_denied"][0] != 0 and data("u002_denied").get("status") == "permission_denied"
        and checks["global_update"][1].get("status") == "updated"
        and checks["global_delete"][1].get("status") == "deleted"
        and len(data("reopen_list").get("preferences", [])) == 2
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

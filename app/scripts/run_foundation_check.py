"""Run the one approved offline foundation check and persist auditable evidence."""

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
ARTIFACT = PROJECT_ROOT / "artifacts" / "foundation" / "integration_check.json"


def _run(command: list[str], environment: dict[str, str]):
    return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")


def main() -> int:
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(PROJECT_ROOT / "src")}
    preflight_command = [str(PYTHON), str(PROJECT_ROOT / "scripts" / "preflight.py")]
    test_command = [str(PYTHON), "-m", "pytest", "-q", str(PROJECT_ROOT / "tests" / "test_foundation.py")]
    preflight = _run(preflight_command, environment)
    tests = _run(test_command, environment)
    try:
        preflight_result = json.loads(preflight.stdout)
    except json.JSONDecodeError:
        preflight_result = {"parse_error": True, "stdout": preflight.stdout[-4000:]}
    with tempfile.TemporaryDirectory(prefix="taxharness-cli-") as directory:
        database = str(Path(directory) / "cli.sqlite3")

        def cli(args: list[str]):
            return _run([str(PYTHON), "-m", "taxharness.cli", *args], environment)

        init = cli(["init-db", "--database", database])
        query_ok = cli(["query", "--database", database, "--user-id", "U001", "--company-id", "C001", "--month", "2026-09"])
        query_denied = cli(["query", "--database", database, "--user-id", "U002", "--company-id", "C002", "--month", "2026-09"])
        update = cli([
            "update-invoice", "--database", database, "--user-id", "U001", "--company-id", "C001", "--invoice-id", "F002",
            "--purpose", "离线检查补录", "--expected-revision", "1", "--idempotency-key", "U001:C001:invoice:F002:check-1",
        ])
        revision_conflict = cli([
            "update-invoice", "--database", database, "--user-id", "U001", "--company-id", "C001", "--invoice-id", "F002",
            "--remarks", "旧版本", "--expected-revision", "1", "--idempotency-key", "U001:C001:invoice:F002:check-2",
        ])
        revision_conflict_replay = cli([
            "update-invoice", "--database", database, "--user-id", "U001", "--company-id", "C001", "--invoice-id", "F002",
            "--remarks", "旧版本", "--expected-revision", "1", "--idempotency-key", "U001:C001:invoice:F002:check-2",
        ])

        def cli_record(process):
            return {"exit_code": process.returncode, "stdout": process.stdout[-4000:], "stderr": process.stderr[-2000:]}

        cli_checks = {
            "init": cli_record(init),
            "query_ok": cli_record(query_ok),
            "query_denied": cli_record(query_denied),
            "update_committed": cli_record(update),
            "revision_conflict": cli_record(revision_conflict),
            "revision_conflict_replay": cli_record(revision_conflict_replay),
        }
        cli_passed = (
            init.returncode == 0
            and query_ok.returncode == 0
            and query_denied.returncode != 0
            and update.returncode == 0
            and revision_conflict.returncode != 0
            and revision_conflict_replay.returncode != 0
        )

    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "python": str(PYTHON),
        "commands": {
            "preflight": " ".join(preflight_command),
            "pytest": " ".join(test_command),
            "cli": "temporary SQLite: init-db, query success, query permission denial, update commit, revision conflict",
        },
        "preflight_exit_code": preflight.returncode,
        "pytest_exit_code": tests.returncode,
        "model_calls": 0,
        "http_calls": 0,
        "preflight": preflight_result,
        "pytest_stdout": tests.stdout[-8000:],
        "pytest_stderr": tests.stderr[-4000:],
        "cli_checks": cli_checks,
        "cli_checks_passed": cli_passed,
        "status": "passed" if preflight.returncode == 0 and tests.returncode == 0 and cli_passed else "failed",
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "artifact": str(ARTIFACT), "preflight_exit_code": preflight.returncode, "pytest_exit_code": tests.returncode}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline environment/source preflight; never prints credential contents."""

from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.metadata as metadata
import json
import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from taxharness.provider import credential_status, provider_config


DEFAULT_XLSX = Path(os.getenv("TAXHARNESS_SOURCE_XLSX", str(PROJECT_ROOT / "data" / "02_示例数据.xlsx")))
DEFAULT_RULES = Path(os.getenv("TAXHARNESS_RULES_DOC", str(PROJECT_ROOT / "data" / "03_业务规则.docx.txt")))


def timezone_data_status() -> dict[str, object]:
    """Check the IANA zone data required by the Beijing-date runtime path."""

    try:
        ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return {"available": False, "zone": "Asia/Shanghai"}
    return {"available": True, "zone": "Asia/Shanghai"}


def run_preflight(source_xlsx: Path, rules_doc: Path, key_dir: Path | None = None) -> tuple[int, dict]:
    config = provider_config()
    if key_dir is not None:
        config = replace(config, api_key_dir=str(key_dir))
    packages = {}
    for name in ("openpyxl", "chainlit", "httpx", "langchain", "langchain-core", "langchain-openai", "langgraph", "langgraph-checkpoint-sqlite", "tzdata", "pytest"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    checks = {
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "python_supported": (3, 12) <= sys.version_info[:2] < (3, 14),
        "packages": packages,
        "required_packages": ["openpyxl", "chainlit", "httpx", "langchain", "langchain-core", "langchain-openai", "langgraph", "langgraph-checkpoint-sqlite", "tzdata"],
        "optional_packages": ["pytest"],
        "timezone_data": timezone_data_status(),
        "source_xlsx": {"path": str(source_xlsx), "exists": source_xlsx.is_file()},
        "rules_doc": {"path": str(rules_doc), "exists": rules_doc.is_file()},
        "provider": {"base_url": config.base_url, "model": config.model, "thinking": config.thinking, "http_called": False},
        "credential": credential_status(config),
    }
    checks["foundation_ready"] = bool(
        checks["python_supported"]
        and all(packages.get(name) for name in checks["required_packages"])
        and checks["source_xlsx"]["exists"]
        and checks["rules_doc"]["exists"]
        and checks["timezone_data"]["available"]
    )
    checks["provider_credential_ready"] = bool(
        checks["credential"].get("format_valid")
        and (checks["credential"].get("source") == "TAXHARNESS_API_KEY" or checks["credential"].get("nonempty_txt_count") == 1)
    )
    # `ready` intentionally means the local deterministic foundation is usable;
    # provider readiness is reported separately and is not required offline.
    checks["ready"] = checks["foundation_ready"]
    return (0 if checks["ready"] else 1), checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--rules-doc", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--key-dir", type=Path)
    args = parser.parse_args(argv)
    key_dir = args.key_dir.expanduser().resolve() if args.key_dir else None
    code, result = run_preflight(args.source_xlsx.expanduser().resolve(), args.rules_doc.expanduser().resolve(), key_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

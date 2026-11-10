from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTERPRISE_ROOT = (PROJECT_ROOT / "artifacts" / "enterprise_delivery_20261027").resolve()
DEFAULT_DATABASE = ENTERPRISE_ROOT / "enterprise.sqlite3"
REQUIRED_TABLES = {"enterprise_users", "enterprise_memberships", "enterprise_work_items", "enterprise_task_invoice_snapshots"}


def _enterprise_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(ENTERPRISE_ROOT):
        raise ValueError(f"企业操作路径必须在独立目录中：{ENTERPRISE_ROOT}")
    return path


def _validate_database(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"SQLite 文件不存在：{path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        if integrity != "ok" or missing:
            raise ValueError(f"备份无效：integrity_check={integrity}; missing_tables={missing}")
        return {"integrity_check": integrity, "tables": sorted(tables)}
    finally:
        connection.close()


def backup(database: Path, output: Path | None = None) -> Path:
    database = _enterprise_path(database)
    if not database.is_file():
        raise ValueError(f"企业业务库尚未创建：{database}")
    destination = _enterprise_path(output or (database.parent / "backups" / f"enterprise-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(database))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    _validate_database(destination)
    return destination


def restore(database: Path, source: Path) -> dict:
    database = _enterprise_path(database)
    source = _enterprise_path(source)
    if source == database:
        raise ValueError("恢复源不能与当前企业数据库相同。")
    _validate_database(source)
    database.parent.mkdir(parents=True, exist_ok=True)
    pre_restore = backup(database) if database.exists() else None
    staged = database.with_name(database.name + ".restore-" + uuid4().hex)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    staged_connection = sqlite3.connect(str(staged))
    try:
        source_connection.backup(staged_connection)
    finally:
        staged_connection.close()
        source_connection.close()
    _validate_database(staged)
    os.replace(staged, database)
    return {"database": str(database), "restored_from": str(source), "pre_restore_backup": str(pre_restore) if pre_restore else None, "integrity_check": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated enterprise database operations")
    parser.add_argument("action", choices=("backup", "verify", "restore"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.action == "backup":
        result = {"backup": str(backup(args.database, args.output)), "integrity_check": "ok"}
    elif args.action == "verify":
        result = {"database": str(_enterprise_path(args.source or args.database)), **_validate_database(_enterprise_path(args.source or args.database))}
    else:
        if args.source is None:
            parser.error("restore requires --source")
        result = restore(args.database, args.source)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

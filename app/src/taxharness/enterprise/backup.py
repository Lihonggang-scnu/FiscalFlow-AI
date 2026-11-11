"""Portable local backup for the enterprise workspace and its conversation files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .storage import EnterpriseDatabase, utc_now


def _snapshot(source: sqlite3.Connection, destination: Path) -> None:
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库备份完整性检查失败。")


def create_backup(database: EnterpriseDatabase, *, database_path: str | Path, destination: str | Path) -> dict:
    root = Path(database_path).resolve().parent
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fiscalflow-backup-") as directory:
        stage = Path(directory)
        _snapshot(database.connection, stage / "enterprise.sqlite3")
        entries = {"enterprise.sqlite3": stage / "enterprise.sqlite3"}
        for name in ("preferences.sqlite3", "assistant/agent.sqlite3", "assistant/checkpoint.sqlite3", "assistant/preferences.sqlite3"):
            source = root / name
            if not source.is_file():
                continue
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as current:
                _snapshot(current, target)
            entries[name] = target
        missing = []
        with closing(sqlite3.connect(stage / "enterprise.sqlite3")) as snapshot:
            attachments = snapshot.execute("SELECT stored_name,sha256 FROM enterprise_assistant_attachments").fetchall()
        for stored_name, expected_hash in attachments:
            if Path(stored_name).name != stored_name:
                raise ValueError("历史附件文件名无效。")
            source = root / "assistant" / "uploads" / stored_name
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
                missing.append(stored_name)
                continue
            entries["assistant/uploads/" + stored_name] = source
        manifest = {"format": "fiscalflow-workspace-v1", "created_at": utc_now(),
                    "schema_version": database.get_meta("enterprise_schema_version"),
                    "snapshot_scope": "individual_databases", "shared_model_ledger_included": False,
                    "missing_legacy_attachments": missing,
                    "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in entries.items()}}
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in entries.items():
                archive.write(path, name)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"status": "ok", "path": str(destination), "filename": destination.name,
            "integrity_check": "ok", "missing_legacy_attachments": missing,
            "snapshot_scope": "individual_databases"}


def restore_backup(archive_path: str | Path, *, destination: str | Path) -> dict:
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("恢复目录必须为空，以免覆盖已有数据。")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        files = manifest.get("files")
        if manifest.get("format") != "fiscalflow-workspace-v1" or not isinstance(files, dict) or "enterprise.sqlite3" not in files:
            raise ValueError("备份格式无效。")
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(files) | {"manifest.json"}:
            raise ValueError("备份文件清单不匹配。")
        for name, expected in files.items():
            parts = PurePosixPath(name).parts
            permitted = name in {"enterprise.sqlite3", "preferences.sqlite3", "assistant/agent.sqlite3",
                                 "assistant/checkpoint.sqlite3", "assistant/preferences.sqlite3"}
            permitted = permitted or (len(parts) == 3 and parts[:2] == ("assistant", "uploads") and
                                       parts[2] not in {".", ".."} and "\\" not in parts[2] and ":" not in parts[2])
            if not permitted or hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("备份文件路径或摘要无效。")
        root.mkdir(parents=True, exist_ok=True)
        for name in files:
            target = root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    with closing(sqlite3.connect(root / "enterprise.sqlite3")) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复后的数据库完整性检查失败。")
    return {"status": "ok", "database_path": str(root / "enterprise.sqlite3"),
            "missing_legacy_attachments": manifest["missing_legacy_attachments"]}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="将 FiscalFlow 备份恢复到空目录")
    parser.add_argument("archive", help="备份 ZIP 路径")
    parser.add_argument("destination", help="空的恢复目录")
    args = parser.parse_args()
    print(json.dumps(restore_backup(args.archive, destination=args.destination), ensure_ascii=False, indent=2))

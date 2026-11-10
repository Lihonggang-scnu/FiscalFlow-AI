"""Build a deterministic, allowlisted submission ZIP.

The final build requires --video. --dry-run performs the same allowlist and
portability checks without creating an archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


FIXED_DATE = (2026, 10, 26, 0, 0, 0)
TEXT_SUFFIXES = {".md", ".txt", ".toml", ".py", ".ps1", ".json", ".js", ".yml", ".yaml"}
ROOT_FILES = [
    "README.md", "pyproject.toml", "requirements.txt", "config.example.toml", "chainlit.md",
    ".chainlit/config.toml", "public/chainlit_ui.js",
    "data/01_参赛说明.docx.txt", "data/02_示例数据.xlsx", "data/03_业务规则.docx.txt",
    "docs/参赛设计说明.md", "docs/参赛启动说明.md", "docs/官方要求核对表.md", "docs/完整测试报告_2026-10-26.md",
    "docs/Chainlit使用说明.md", "docs/规则维护模块使用说明.md",
    "scripts/chainlit_app.py", "scripts/chat.py", "scripts/preflight.py", "scripts/run_cli.ps1",
    "scripts/start_chainlit.ps1", "scripts/stop_chainlit.ps1", "scripts/export_package_records.py",
    "scripts/build_submission.py",
]
SRC_ROOT = "src"
RECORD_ROOT = "artifacts/package_records"
PORTABILITY_PATTERNS = (
    ("windows_path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
    ("user_path", re.compile(r"(?:^|[\\/])Users[\\/]", re.IGNORECASE)),
    ("credential_name", re.compile("deepseek" + r"\s+key", re.IGNORECASE)),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for relative in ROOT_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"required package file missing: {relative}")
        files.append((path, relative.replace("\\", "/")))
    for path in sorted((root / SRC_ROOT).rglob("*.py")):
        if "__pycache__" not in path.parts:
            files.append((path, path.relative_to(root).as_posix()))
    record_dir = root / RECORD_ROOT
    if record_dir.is_dir():
        for path in sorted(record_dir.glob("*.json")):
            files.append((path, path.relative_to(root).as_posix()))
    unique: dict[str, Path] = {archive: path for path, archive in files}
    return [(unique[name], name) for name in sorted(unique)]


def check_portability(files: list[tuple[Path, str]]) -> list[str]:
    errors: list[str] = []
    for path, archive in files:
        if archive == "scripts/build_submission.py":
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for label, pattern in PORTABILITY_PATTERNS:
            for match in pattern.finditer(text):
                if label == "windows_path" and text[max(0, match.start() - 8):match.start()].lower().endswith(("http://", "https://")):
                    continue
                errors.append(f"{archive}: portability pattern {label}")
                break
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("dist/TaxAgentHarness_submission_20261026.zip"))
    parser.add_argument("--video", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    files = collect(root)
    errors = check_portability(files)
    if errors:
        raise SystemExit("portability check failed:\n" + "\n".join(errors))
    video = args.video.expanduser().resolve() if args.video else None
    if not args.dry_run:
        if video is None or not video.is_file():
            raise SystemExit("final build requires --video pointing to the real demonstration MP4")
        if args.output.exists() and not args.force:
            raise SystemExit(f"output exists; pass --force to replace: {args.output}")
    manifest = [{"path": archive, "sha256": sha256(path), "size": path.stat().st_size} for path, archive in files]
    summary = {"status": "dry_run" if args.dry_run else "ready", "files": manifest, "video": str(video) if video else None}
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, archive_name in files:
            info = zipfile.ZipInfo(archive_name, FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
        video_info = zipfile.ZipInfo("演示视频.mp4", FIXED_DATE)
        video_info.compress_type = zipfile.ZIP_DEFLATED
        video_info.external_attr = 0o100644 << 16
        archive.writestr(video_info, video.read_bytes())
        manifest[0:0] = [{"path": "演示视频.mp4", "sha256": sha256(video), "size": video.stat().st_size}]
        archive.writestr("包内文件清单.json", json.dumps({"files": manifest}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "built", "output": str(output), "file_count": len(manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

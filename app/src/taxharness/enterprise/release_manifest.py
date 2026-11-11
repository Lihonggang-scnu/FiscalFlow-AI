"""Record the software and model configuration present when a delivery is generated."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from ..agent import SYSTEM_PROMPT
from ..errors import InvalidInputError
from ..provider import provider_config
from .agent import EXTRACTION_SYSTEM
from .conversation import SYSTEM as ENTERPRISE_PROMPT
from .multimodal import SYSTEM as VISION_PROMPT


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def release_manifest(database, *, company_id: str, period: str) -> dict:
    package = Path(__file__).resolve().parents[1]
    files = sorted(path for path in package.rglob("*") if path.is_file() and path.suffix in {".py", ".html"})
    sources = {path.relative_to(package).as_posix(): _hash(path.read_bytes()) for path in files}
    tree = "\n".join(f"{name} {digest}" for name, digest in sources.items()).encode("utf-8")
    try:
        config = provider_config()
        configured_model = {"provider": config.kind, "model": config.model,
            "endpoint_host": urlsplit(config.base_url).hostname, "profile": os.getenv("TAXHARNESS_PROFILE") or "default"}
    except (InvalidInputError, OSError, ValueError):
        configured_model = {"status": "unavailable"}
    method = database.connection.execute("""SELECT version,method_name,created_at FROM enterprise_professional_methods
        WHERE company_id=? AND period=? ORDER BY version DESC LIMIT 1""", (company_id, period)).fetchone()
    return {"source_tree_sha256": _hash(tree), "source_files": sources,
            "database_schema_version": database.get_meta("enterprise_schema_version"),
            "configured_model": configured_model,
            "prompt_sha256": {"assistant": _hash(SYSTEM_PROMPT.encode("utf-8")),
                              "enterprise_assistant": _hash(ENTERPRISE_PROMPT.encode("utf-8")),
                              "reply_extraction": _hash(EXTRACTION_SYSTEM.encode("utf-8")),
                              "vision": _hash(VISION_PROMPT.encode("utf-8"))},
            "tool_source_sha256": {"assistant": sources["agent_tools.py"],
                                    "enterprise_assistant": sources["enterprise/conversation.py"]},
            "professional_method": dict(method) if method else {"version": 0, "method_name": "默认复核方法", "status": "default"},
            "scope": "生成交付包时的代码与配置；实际每次模型调用以对应运行记录为准。"}

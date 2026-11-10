"""Small secret-safe JSON normalizer adapted from ChemIntent's case store.

Adapted as a small local helper; the packaged project does not depend on the source project.
The implementation is intentionally isolated from the old package namespace.
"""

from __future__ import annotations

import math
import re
from typing import Any

_OMIT = object()
_SECRET_KEYS = {
    "apikey",
    "accesskey",
    "accesstoken",
    "refreshtoken",
    "password",
    "secret",
    "authorization",
    "credentials",
    "credential",
    "privatekey",
    "clientsecret",
}


def safe_case_json(value: Any):
    """Keep JSON-compatible values while dropping credential-shaped keys."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SECRET_KEYS or any(normalized.endswith(k) for k in _SECRET_KEYS):
                continue
            cleaned = safe_case_json(child)
            if cleaned is not _OMIT:
                result[key] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [cleaned for child in value if (cleaned := safe_case_json(child)) is not _OMIT]
    return _OMIT

"""Small immutable-ish containers shared by import and storage layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbookData:
    source_path: str
    source_sha256: str
    companies: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    invoices: list[dict[str, Any]] = field(default_factory=list)
    bank_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RuleSpec:
    rule_id: str
    version: str
    name: str
    kind: str
    company_id: str | None
    effective_from: str
    status: str
    content: str
    source_path: str
    source_sha256: str
    condition: dict[str, Any] = field(default_factory=dict)

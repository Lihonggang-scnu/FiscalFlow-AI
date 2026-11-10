"""Deterministic rule parsing, version selection and condition execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import InvalidInputError, RuleConflictError, RuleExecutionError, RuleNotFoundError
from .importer import file_sha256
from .models import RuleSpec
from .money import cents_to_text


RULE_KINDS = {"amount_statistics", "invoice_purpose", "bank_requirements"}
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


_RULE_META: dict[tuple[str, str], dict[str, Any]] = {
    ("R001", "1.0"): {
        "kind": "amount_statistics", "company_id": None, "effective_from": "2026-01-01", "status": "active",
        "condition": {"type": "not_applicable"},
    },
    ("R002", "1.0"): {
        "kind": "invoice_purpose", "company_id": "C001", "effective_from": "2026-08-01", "status": "inactive",
        "condition": {"type": "amount_gte", "amount_cents": 100000},
    },
    ("R002", "2.0"): {
        "kind": "invoice_purpose", "company_id": "C001", "effective_from": "2026-09-01", "status": "active",
        "condition": {"type": "all"},
    },
    ("R003", "1.0"): {
        "kind": "invoice_purpose", "company_id": "C002", "effective_from": "2026-08-01", "status": "active",
        "condition": {"type": "amount_gte", "amount_cents": 100000},
    },
    ("R004", "1.0"): {
        "kind": "bank_requirements", "company_id": None, "effective_from": "2026-01-01", "status": "active",
        "condition": {"type": "not_applicable"},
    },
}


def _version_tuple(version: str) -> tuple[int, int]:
    match = _VERSION_RE.fullmatch(str(version).strip())
    if not match:
        raise InvalidInputError("规则版本必须是 major.minor 数字格式。")
    return int(match.group(1)), int(match.group(2))


def _condition_payload(condition: Any) -> dict[str, Any]:
    if not isinstance(condition, dict):
        raise InvalidInputError("规则 condition 必须是结构化 JSON 对象。")
    value = dict(condition)
    condition_type = value.get("type", value.get("kind"))
    if not isinstance(condition_type, str) or not condition_type.strip():
        raise InvalidInputError("规则 condition 必须包含 type。")
    condition_type = condition_type.strip()
    if condition_type == "amount_gte":
        threshold = value.get("amount_cents", value.get("threshold_cents"))
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise InvalidInputError("amount_gte 条件必须包含非负整数 amount_cents。")
        return {"type": "amount_gte", "amount_cents": threshold}
    if condition_type in {"all", "not_applicable", "none"}:
        return {"type": condition_type}
    value["type"] = condition_type
    json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def condition_execution(condition: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """Return execution metadata and, for invoice rules, a predicate result."""

    condition = _condition_payload(condition)
    condition_type = condition["type"]
    if kind != "invoice_purpose":
        if condition_type == "not_applicable":
            return {"status": "not_applicable", "summary": "该规则类型不执行发票用途条件。", "required": None}
        return {"status": "unsupported", "summary": f"条件 {condition_type} 不适用于该规则类型。", "required": None}
    if condition_type == "all":
        return {"status": "supported", "summary": "每张发票都必须填写用途。", "required": True}
    if condition_type == "amount_gte":
        threshold = int(condition["amount_cents"])
        return {"status": "supported", "summary": f"发票金额大于或等于 {cents_to_text(threshold)} 元时必须填写用途。", "amount_cents": threshold}
    if condition_type == "none":
        return {"status": "supported", "summary": "该规则不要求发票填写用途。", "required": False}
    return {"status": "unsupported", "summary": f"条件 {condition_type} 暂不支持自动判定。", "required": None}


def _rule_view(row: dict[str, Any] | Any) -> dict[str, Any]:
    value = dict(row)
    raw_condition = value.pop("condition_json", "{}") or "{}"
    try:
        condition = json.loads(raw_condition)
    except (TypeError, json.JSONDecodeError):
        condition = {"type": "unsupported", "raw": str(raw_condition)}
    if not isinstance(condition, dict) or not isinstance(condition.get("type", condition.get("kind")), str):
        condition = {"type": "unsupported", "raw": raw_condition}
    value["condition"] = condition
    try:
        execution = condition_execution(condition, kind=value["kind"])
    except InvalidInputError:
        execution = {"status": "unsupported", "summary": "规则条件为空或格式不完整，不能自动判定。"}
    value["execution_status"] = value.get("condition_status") or execution["status"]
    value["execution_summary"] = value.get("condition_summary") or execution["summary"]
    value.pop("condition_status", None)
    value.pop("condition_summary", None)
    return value


def parse_rules_document(path: str | Path) -> list[RuleSpec]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise InvalidInputError(f"业务规则文档不存在：{source}")
    lines = source.read_text(encoding="utf-8").splitlines()
    headers = []
    pattern = re.compile(r"^(R\d{3})\s+(.+?)\s+(\d+\.\d+)版(?:\s+已停用)?$")
    for index, line in enumerate(lines):
        match = pattern.match(line.strip())
        if match:
            headers.append((index, match.group(1), match.group(2), match.group(3)))
    expected = set(_RULE_META)
    found = {(rule_id, version) for _, rule_id, _, version in headers}
    if found != expected:
        raise InvalidInputError(f"业务规则版本清单不符：找到 {sorted(found)}")
    digest = file_sha256(source)
    specs = []
    for position, (line_index, rule_id, name, version) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        meta = _RULE_META[(rule_id, version)]
        content = "\n".join(lines[line_index:end]).strip()
        specs.append(RuleSpec(rule_id, version, name, meta["kind"], meta["company_id"], meta["effective_from"], meta["status"], content, str(source), digest, dict(meta["condition"])))
    return specs


def seed_rules(connection, path: str | Path) -> list[RuleSpec]:
    """Insert initial rules only when their version is absent; never overwrite maintenance."""

    specs = parse_rules_document(path)
    for rule in specs:
        execution = condition_execution(rule.condition, kind=rule.kind)
        connection.execute(
            """INSERT OR IGNORE INTO rules(rule_id,version,name,kind,company_id,effective_from,status,content,
               source_path,source_sha256,condition_json,condition_status,condition_summary)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rule.rule_id, rule.version, rule.name, rule.kind, rule.company_id, rule.effective_from, rule.status,
             rule.content, rule.source_path, rule.source_sha256, json.dumps(rule.condition, ensure_ascii=False, sort_keys=True),
             execution["status"], execution["summary"]),
        )
    return specs


def _month_start(month: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise InvalidInputError("月份必须是 YYYY-MM。")
    return month + "-01"


def applicable_rule(connection, *, kind: str, company_id: str, month: str) -> dict:
    start = _month_start(month)
    rows = connection.execute(
        """SELECT rule_id,version,name,kind,company_id,effective_from,status,content,source_path,source_sha256,
                  condition_json,condition_status,condition_summary
           FROM rules WHERE kind=? AND effective_from<=?
             AND (company_id IS NULL OR company_id=?)""",
        (kind, start, company_id),
    ).fetchall()
    if not rows:
        raise RuleNotFoundError(f"未找到 {company_id} 在 {month} 适用的 {kind} 规则。")
    specific = [row for row in rows if row["company_id"] is not None]
    candidates = specific or [row for row in rows if row["company_id"] is None]
    latest_date = max(row["effective_from"] for row in candidates)
    candidates = [row for row in candidates if row["effective_from"] == latest_date]
    if len({row["rule_id"] for row in candidates}) > 1:
        raise RuleConflictError(f"{company_id} 在 {month} 的 {kind} 存在并列最新规则，不能自动选择。")
    latest_version = max(_version_tuple(row["version"]) for row in candidates)
    candidates = [row for row in candidates if _version_tuple(row["version"]) == latest_version]
    chosen = candidates[0]
    if chosen["status"] != "active":
        raise RuleNotFoundError(f"{company_id} 在 {month} 的最新规则 {chosen['rule_id']} {chosen['version']} 已停用，无适用依据。")
    return _rule_view(chosen)


def applicable_rules(connection, *, company_id: str, month: str) -> list[dict]:
    _month_start(month)
    result = []
    for kind in ("amount_statistics", "invoice_purpose", "bank_requirements"):
        try:
            result.append(applicable_rule(connection, kind=kind, company_id=company_id, month=month))
        except RuleNotFoundError:
            continue
        except RuleConflictError as error:
            result.append({"kind": kind, "status": error.code, "summary": str(error)})
    return result


def evaluate_invoice_purpose(rule: dict, amount_cents: int, purpose: Any) -> dict[str, Any]:
    """Evaluate one invoice or return an explicit cannot-determine result."""

    execution = condition_execution(rule["condition"], kind=rule["kind"])
    if execution["status"] == "unsupported":
        raise RuleExecutionError(execution["summary"])
    if execution["status"] == "not_applicable":
        raise RuleExecutionError("当前规则不能用于发票用途完整性判断。")
    blank = not str(purpose or "").strip()
    required = execution.get("required")
    if required is None and "amount_cents" in execution:
        required = amount_cents >= int(execution["amount_cents"])
    return {"required": bool(required), "missing": bool(required and blank)}


def migrate_initial_conditions(connection, official_specs: list[RuleSpec] | None = None) -> None:
    """Backfill condition metadata once for databases created before rule conditions."""

    if not official_specs:
        return
    for spec in official_specs:
        condition = dict(spec.condition)
        execution = condition_execution(condition, kind=spec.kind)
        connection.execute(
            """UPDATE rules SET condition_json=?,condition_status=?,condition_summary=?
               WHERE rule_id=? AND version=? AND source_sha256=? AND name=? AND kind=?
                 AND company_id IS ? AND effective_from=? AND content=?
                 AND (condition_json IS NULL OR condition_json='{}')
                 AND (condition_status IS NULL OR condition_status='unsupported')""",
            (json.dumps(condition, ensure_ascii=False, sort_keys=True), execution["status"], execution["summary"], spec.rule_id, spec.version,
             spec.source_sha256, spec.name, spec.kind, spec.company_id, spec.effective_from, spec.content),
        )

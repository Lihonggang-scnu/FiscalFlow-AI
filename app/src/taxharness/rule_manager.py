"""Local trusted CLI rule maintenance over the deterministic rule table."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import InvalidInputError, RuleConflictError, RuleNotFoundError
from .importer import file_sha256
from .rules import RULE_KINDS, _condition_payload, _rule_view, _version_tuple, condition_execution
from .storage import TaxDatabase, record_task_event


_RULE_ID_RE = re.compile(r"^R\d{3}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_payload(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise InvalidInputError(f"规则导入文件不存在：{source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidInputError(f"规则导入文件不是有效 JSON：{source}") from error
    if isinstance(value, dict):
        value = value.get("rules")
    if not isinstance(value, list) or not value:
        raise InvalidInputError("规则导入文件必须是非空规则数组，或包含 rules 数组。")
    if not all(isinstance(item, dict) for item in value):
        raise InvalidInputError("规则数组中的每项必须是 JSON 对象。")
    return value


class RuleManager:
    """Rule maintenance is intentionally a local trusted CLI/API, not an Agent tool."""

    def __init__(self, database: TaxDatabase):
        self.database = database

    def _validate_companies(self, values: list[dict[str, Any]]) -> None:
        known = {
            row[0] for row in self.database.connection.execute("SELECT company_id FROM companies").fetchall()
        }
        missing = sorted({value["company_id"] for value in values if value.get("company_id") and value["company_id"] not in known})
        if missing:
            raise InvalidInputError(f"规则适用企业不存在：{'、'.join(missing)}。整批导入未写入。")

    def _row(self, rule_id: str, version: str) -> sqlite3.Row | None:
        return self.database.connection.execute(
            "SELECT * FROM rules WHERE rule_id=? AND version=?", (rule_id, version)
        ).fetchone()

    @staticmethod
    def _validate(item: dict[str, Any], *, source_path: str, source_sha256: str, require_complete: bool = True) -> dict[str, Any]:
        required = {"rule_id", "version", "name", "kind", "effective_from", "content", "condition"}
        missing = sorted(required - set(item))
        if require_complete and missing:
            raise InvalidInputError(f"规则字段不完整，缺少：{','.join(missing)}。正文和 condition 必须一起提供。")
        rule_id = str(item.get("rule_id", "")).strip()
        if not _RULE_ID_RE.fullmatch(rule_id):
            raise InvalidInputError("rule_id 必须是 R 加三位数字。")
        version = str(item.get("version", "")).strip()
        _version_tuple(version)
        kind = str(item.get("kind", "")).strip()
        if kind not in RULE_KINDS:
            raise InvalidInputError(f"kind 必须是：{'、'.join(sorted(RULE_KINDS))}。")
        effective_from = str(item.get("effective_from", "")).strip()
        if not _DATE_RE.fullmatch(effective_from):
            raise InvalidInputError("effective_from 必须是 YYYY-MM-DD。")
        try:
            datetime.fromisoformat(effective_from)
        except ValueError as error:
            raise InvalidInputError("effective_from 不是有效日期。") from error
        company_id = item.get("company_id")
        if company_id is not None:
            company_id = str(company_id).strip()
            if not company_id:
                company_id = None
        content = str(item.get("content", "")).strip()
        if not content:
            raise InvalidInputError("规则正文不能为空。")
        name = str(item.get("name", "")).strip()
        if not name:
            raise InvalidInputError("规则 name 不能为空。")
        condition = _condition_payload(item.get("condition"))
        execution = condition_execution(condition, kind=kind)
        status = str(item.get("status", "inactive")).strip().lower()
        if status not in {"active", "inactive"}:
            raise InvalidInputError("规则 status 只能是 active 或 inactive。")
        return {
            "rule_id": rule_id,
            "version": version,
            "name": name,
            "kind": kind,
            "company_id": company_id,
            "effective_from": effective_from,
            "status": status,
            "content": content,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "condition": condition,
            "condition_json": json.dumps(condition, ensure_ascii=False, sort_keys=True),
            "condition_status": execution["status"],
            "condition_summary": execution["summary"],
        }

    @staticmethod
    def _comparable(row: sqlite3.Row | dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        comparable = {
            key: row.get(key) if key not in {"condition"} else json.loads(row.get("condition_json") or "{}")
            for key in ("rule_id", "version", "name", "kind", "company_id", "effective_from", "status", "content", "condition")
        }
        comparable["condition"] = _condition_payload(comparable["condition"])
        return comparable

    def list_rules(self, *, rule_id: str | None = None, version: str | None = None, kind: str | None = None,
                   company_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if rule_id:
            clauses.append("rule_id=?")
            params.append(rule_id)
        if version:
            clauses.append("version=?")
            params.append(version)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if company_id:
            clauses.append("(company_id IS NULL OR company_id=?)")
            params.append(company_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.database.connection.execute(
            f"SELECT * FROM rules{where} ORDER BY rule_id, effective_from, version", params
        ).fetchall()
        return {"status": "ok", "rules": [_rule_view(row) for row in rows]}

    def get_rule(self, *, rule_id: str, version: str) -> dict[str, Any]:
        row = self._row(rule_id, version)
        if row is None:
            return {"status": "not_found", "summary": "规则版本不存在。"}
        return {"status": "ok", "rule": _rule_view(row)}

    def import_file(self, path: str | Path) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        payloads = _read_payload(source)
        source_hash = file_sha256(source)
        values = [self._validate(item, source_path=str(source), source_sha256=source_hash) for item in payloads]
        self._validate_companies(values)
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        results = []
        try:
            for value in values:
                prior = self._row(value["rule_id"], value["version"])
                if prior is not None:
                    if self._comparable(prior, value) == {key: value[key] for key in ("rule_id", "version", "name", "kind", "company_id", "effective_from", "status", "content", "condition")}:
                        results.append({"status": "unchanged", "rule": _rule_view(prior)})
                        continue
                    raise RuleConflictError(f"规则 {value['rule_id']} {value['version']} 已存在且内容不同，请创建新版本。")
                connection.execute(
                    """INSERT INTO rules(rule_id,version,name,kind,company_id,effective_from,status,content,source_path,
                       source_sha256,condition_json,condition_status,condition_summary)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (value["rule_id"], value["version"], value["name"], value["kind"], value["company_id"], value["effective_from"],
                     value["status"], value["content"], value["source_path"], value["source_sha256"], value["condition_json"],
                     value["condition_status"], value["condition_summary"]),
                )
                record_task_event(connection, user_id="__system__", company_id=value["company_id"] or "*", kind="rule_changed",
                    payload={"rule_id": value["rule_id"], "version": value["version"], "kind": value["kind"],
                             "effective_from": value["effective_from"], "status": value["status"], "condition": value["condition"]})
                results.append({"status": "imported", "rule": _rule_view(self._row(value["rule_id"], value["version"]))})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        status = "unchanged" if results and all(item["status"] == "unchanged" for item in results) else "imported"
        return {"status": status, "rules": results}

    def update_file(self, path: str | Path) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        payloads = _read_payload(source)
        if len(payloads) != 1:
            raise InvalidInputError("rule-update 文件必须只包含一个规则对象。")
        value = self._validate(payloads[0], source_path=str(source), source_sha256=file_sha256(source))
        self._validate_companies([value])
        prior = self._row(value["rule_id"], value["version"])
        base = self.database.connection.execute("SELECT 1 FROM rules WHERE rule_id=? LIMIT 1", (value["rule_id"],)).fetchone()
        if base is None:
            raise RuleNotFoundError("要更正的 rule_id 不存在，请先导入该规则的基础版本。")
        if prior is not None:
            raise RuleConflictError("规则版本不可原地更正，请使用新的 version；旧版本保持不变。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT INTO rules(rule_id,version,name,kind,company_id,effective_from,status,content,source_path,
                   source_sha256,condition_json,condition_status,condition_summary)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (value["rule_id"], value["version"], value["name"], value["kind"], value["company_id"], value["effective_from"],
                 value["status"], value["content"], value["source_path"], value["source_sha256"], value["condition_json"],
                 value["condition_status"], value["condition_summary"]),
            )
            updated = self._row(value["rule_id"], value["version"])
            record_task_event(connection, user_id="__system__", company_id=value["company_id"] or "*", kind="rule_changed",
                payload={"rule_id": value["rule_id"], "version": value["version"], "kind": value["kind"],
                         "effective_from": value["effective_from"], "status": value["status"], "condition": value["condition"]})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {"status": "updated", "rule": _rule_view(updated)}

    def set_status(self, *, rule_id: str, version: str, status: str) -> dict[str, Any]:
        if status not in {"active", "inactive"}:
            raise InvalidInputError("规则 status 只能是 active 或 inactive。")
        prior = self._row(rule_id, version)
        if prior is None:
            return {"status": "not_found", "summary": "规则版本不存在。"}
        if prior["status"] == status:
            return {"status": "unchanged", "rule": _rule_view(prior)}
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE rules SET status=? WHERE rule_id=? AND version=?", (status, rule_id, version))
            updated = self._row(rule_id, version)
            view = _rule_view(updated)
            record_task_event(connection, user_id="__system__", company_id=view["company_id"] or "*", kind="rule_changed",
                payload={"rule_id": rule_id, "version": version, "kind": view["kind"],
                         "effective_from": view["effective_from"], "status": status, "condition": view["condition"]})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {"status": "enabled" if status == "active" else "disabled", "rule": _rule_view(updated)}

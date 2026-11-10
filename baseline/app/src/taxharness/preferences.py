"""Durable, controlled expression preferences for the tax-harness Agent."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import InvalidInputError, PermissionDeniedError
from .services import TaxService
from .storage import TaxDatabase


AMOUNT_ORDER_OPTIONS: dict[str, str] = {
    "invoice_total_then_bank_income_then_bank_expense": "发票总额 → 银行收入 → 银行支出",
    "invoice_total_then_bank_expense_then_bank_income": "发票总额 → 银行支出 → 银行收入",
    "bank_income_then_invoice_total_then_bank_expense": "银行收入 → 发票总额 → 银行支出",
    "bank_income_then_bank_expense_then_invoice_total": "银行收入 → 银行支出 → 发票总额",
    "bank_expense_then_invoice_total_then_bank_income": "银行支出 → 发票总额 → 银行收入",
    "bank_expense_then_bank_income_then_invoice_total": "银行支出 → 银行收入 → 发票总额",
}


PREFERENCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "response_language": {
        "label": "回答语言",
        "values": {"zh-CN": "中文", "en-US": "英文"},
        "aliases": {"中文": "zh-CN", "英文": "en-US", "英语": "en-US", "English": "en-US"},
    },
    "response_detail": {
        "label": "回答详略",
        "values": {"concise": "简短", "standard": "标准", "detailed": "详细"},
        "aliases": {"简短": "concise", "标准": "standard", "详细": "detailed"},
    },
    "answer_order": {
        "label": "回答顺序",
        "values": {"result_first": "结果优先", "evidence_first": "依据优先"},
        "aliases": {"结果优先": "result_first", "依据优先": "evidence_first"},
    },
    "amount_order": {
        "label": "金额排列",
        "values": AMOUNT_ORDER_OPTIONS,
        "aliases": {
            "发票总额→银行收入→银行支出": "invoice_total_then_bank_income_then_bank_expense",
            "发票总额 → 银行收入 → 银行支出": "invoice_total_then_bank_income_then_bank_expense",
            "发票总额→银行支出→银行收入": "invoice_total_then_bank_expense_then_bank_income",
            "银行收入→发票总额→银行支出": "bank_income_then_invoice_total_then_bank_expense",
            "银行收入→银行支出→发票总额": "bank_income_then_bank_expense_then_invoice_total",
            "银行支出→发票总额→银行收入": "bank_expense_then_invoice_total_then_bank_income",
            "银行支出→银行收入→发票总额": "bank_expense_then_bank_income_then_invoice_total",
        },
    },
    "response_format": {
        "label": "回答格式",
        "values": {"auto": "自动", "text": "文字", "table": "表格"},
        "aliases": {"自动": "auto", "文字": "text", "表格": "table"},
    },
}


def preference_catalog() -> list[dict[str, Any]]:
    """Return the model-facing controlled key/value catalog from one source."""

    return [
        {
            "key": key,
            "label": definition["label"],
            "values": [
                {"value": value, "label": label}
                for value, label in definition["values"].items()
            ],
        }
        for key, definition in PREFERENCE_DEFINITIONS.items()
    ]


def preference_catalog_text() -> str:
    """Compact tool-description form of the controlled catalog."""

    return "；".join(
        f"{item['key']}（{item['label']}）：" + "、".join(
            f"{value['value']}={value['label']}" for value in item["values"]
        )
        for item in preference_catalog()
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('user','company')),
    company_id TEXT,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK ((scope='user' AND company_id IS NULL) OR (scope='company' AND company_id IS NOT NULL))
);
DROP INDEX IF EXISTS preferences_active_key;
CREATE UNIQUE INDEX IF NOT EXISTS preferences_active_user_key
    ON preferences(user_id, preference_key)
    WHERE deleted_at IS NULL AND scope='user';
CREATE UNIQUE INDEX IF NOT EXISTS preferences_active_company_key
    ON preferences(user_id, company_id, preference_key)
    WHERE deleted_at IS NULL AND scope='company';
CREATE INDEX IF NOT EXISTS preferences_user_scope
    ON preferences(user_id, scope, company_id, deleted_at, preference_key);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_preference_path(database_path: str | Path) -> Path:
    """Return the independent preference file beside the Agent store."""

    database = Path(database_path).expanduser().resolve()
    root = database.parent
    agent_root = root.parent / "agent" if root.name in {"runtime", "agent"} else root / "agent"
    return agent_root / "preferences.sqlite3"


def _row_value(row: sqlite3.Row) -> dict[str, Any]:
    definition = PREFERENCE_DEFINITIONS[row["preference_key"]]
    value = row["preference_value"]
    return {
        "preference_id": row["preference_id"],
        "user_id": row["user_id"],
        "scope": row["scope"],
        "company_id": row["company_id"],
        "key": row["preference_key"],
        "value": value,
        "value_label": definition["values"][value],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def normalize_key(key: str) -> str:
    if not isinstance(key, str):
        raise InvalidInputError("偏好名称必须是受控的文本标识。")
    value = key.strip()
    if value not in PREFERENCE_DEFINITIONS:
        allowed = "、".join(PREFERENCE_DEFINITIONS)
        raise InvalidInputError(f"不支持该偏好；可选项为：{allowed}。")
    return value


def normalize_value(key: str, value: str) -> str:
    definition = PREFERENCE_DEFINITIONS[normalize_key(key)]
    if not isinstance(value, str):
        raise InvalidInputError("偏好值必须是受控选项，不能保存任意指令。")
    candidate = value.strip()
    candidate = definition.get("aliases", {}).get(candidate, candidate)
    if candidate not in definition["values"]:
        allowed = "、".join(definition["values"].values())
        raise InvalidInputError(f"{definition['label']}只能选择：{allowed}。")
    return candidate


def normalize_scope(scope: str | None, company_id: str | None) -> tuple[str, str | None]:
    if scope is None or not str(scope).strip():
        scope = "user"
    scope = str(scope).strip().lower()
    scope = {"global": "user", "user_global": "user", "enterprise": "company"}.get(scope, scope)
    if scope not in {"user", "company"}:
        raise InvalidInputError("偏好范围只能是 user（用户通用）或 company（指定企业）。")
    company = str(company_id).strip() if company_id is not None else None
    if scope == "user" and company:
        raise InvalidInputError("用户通用偏好不能绑定企业；指定企业时请使用 scope=company。")
    if scope == "company" and not company:
        raise InvalidInputError("企业范围偏好必须提供 company_id。")
    return scope, company


class PreferenceStore:
    """Low-level durable preference storage, independent from business data."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _open(self):
        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _active_row(self, connection: sqlite3.Connection, *, user_id: str, scope: str, company_id: str | None, key: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM preferences
               WHERE user_id=? AND scope=? AND company_id IS ? AND preference_key=? AND deleted_at IS NULL""",
            (user_id, scope, company_id, key),
        ).fetchone()

    def _row_by_id(self, connection: sqlite3.Connection, preference_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM preferences WHERE preference_id=?", (preference_id,)).fetchone()

    def create(self, *, user_id: str, scope: str, company_id: str | None, key: str, value: str) -> dict[str, Any]:
        key = normalize_key(key)
        value = normalize_value(key, value)
        scope, company_id = normalize_scope(scope, company_id)
        now = _now()
        with self._open() as connection:
            existing = self._active_row(connection, user_id=user_id, scope=scope, company_id=company_id, key=key)
            if existing is not None:
                current = _row_value(existing)
                if existing["preference_value"] == value:
                    return {"status": "unchanged", "preference": current, "summary": "该偏好已经是相同值。"}
                return {"status": "preference_conflict", "preference": current, "summary": "该范围已有不同偏好值，请使用更正操作。"}
            preference_id = "pref-" + uuid4().hex
            connection.execute(
                """INSERT INTO preferences(preference_id,user_id,scope,company_id,preference_key,preference_value,
                   revision,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
                (preference_id, user_id, scope, company_id, key, value, 1, now, now),
            )
            row = self._row_by_id(connection, preference_id)
            return {"status": "created", "preference": _row_value(row), "summary": "长期偏好已保存。"}

    def list_active(self, *, user_id: str, company_id: str | None = None) -> list[dict[str, Any]]:
        with self._open() as connection:
            if company_id is None:
                rows = connection.execute(
                    """SELECT * FROM preferences WHERE user_id=? AND scope='user' AND deleted_at IS NULL
                       ORDER BY preference_key""",
                    (user_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM preferences
                       WHERE user_id=? AND deleted_at IS NULL AND (scope='user' OR (scope='company' AND company_id=?))
                       ORDER BY scope, preference_key""",
                    (user_id, company_id),
                ).fetchall()
            return [_row_value(row) for row in rows]

    def list_all_active(self, *, user_id: str, company_ids: list[str]) -> list[dict[str, Any]]:
        """List global values and company values in the caller's allowed set."""

        allowed = set(company_ids)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM preferences WHERE user_id=? AND deleted_at IS NULL ORDER BY scope,company_id,preference_key",
                (user_id,),
            ).fetchall()
            return [
                _row_value(row)
                for row in rows
                if row["scope"] == "user" or row["company_id"] in allowed
            ]

    def effective(self, *, user_id: str, company_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.list_active(user_id=user_id, company_id=company_id)
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["key"] not in selected or row["scope"] == "company":
                selected[row["key"]] = {**row, "source_scope": row["scope"]}
        return [selected[key] for key in sorted(selected)]

    def get(self, *, preference_id: str) -> dict[str, Any] | None:
        with self._open() as connection:
            row = self._row_by_id(connection, preference_id)
            return _row_value(row) if row is not None and row["deleted_at"] is None else None

    def update(self, *, preference_id: str, value: str, expected_revision: int | None = None) -> dict[str, Any]:
        with self._open() as connection:
            row = self._row_by_id(connection, preference_id)
            if row is None or row["deleted_at"] is not None:
                return {"status": "not_found", "summary": "偏好不存在。"}
            value = normalize_value(row["preference_key"], value)
            current = _row_value(row)
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                return {"status": "revision_conflict", "preference": current, "summary": "偏好版本已变化，请重新读取后更正。"}
            if row["preference_value"] == value:
                return {"status": "unchanged", "preference": current, "summary": "该偏好已经是相同值。"}
            now = _now()
            connection.execute(
                "UPDATE preferences SET preference_value=?,revision=revision+1,updated_at=? WHERE preference_id=? AND deleted_at IS NULL",
                (value, now, preference_id),
            )
            updated = self._row_by_id(connection, preference_id)
            return {"status": "updated", "preference": _row_value(updated), "summary": "长期偏好已更正。"}

    def delete(self, *, preference_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        with self._open() as connection:
            row = self._row_by_id(connection, preference_id)
            if row is None or row["deleted_at"] is not None:
                return {"status": "not_found", "summary": "偏好不存在。"}
            current = _row_value(row)
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                return {"status": "revision_conflict", "preference": current, "summary": "偏好版本已变化，请重新读取后删除。"}
            now = _now()
            connection.execute(
                "UPDATE preferences SET deleted_at=?,updated_at=? WHERE preference_id=? AND deleted_at IS NULL",
                (now, now, preference_id),
            )
            return {"status": "deleted", "preference_id": preference_id, "summary": "长期偏好已删除。"}


class PreferenceManager:
    """Public preference API with user existence and enterprise access checks."""

    def __init__(self, store: PreferenceStore, database: TaxDatabase):
        self.store = store
        self.database = database

    def _require_user(self, user_id: str) -> None:
        row = self.database.connection.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise PermissionDeniedError(f"用户 {user_id} 不存在。")

    def _authorize_scope(self, *, user_id: str, scope: str | None, company_id: str | None) -> tuple[str, str | None]:
        self._require_user(user_id)
        normalized_scope, normalized_company = normalize_scope(scope, company_id)
        if normalized_scope == "company":
            TaxService(self.database).require_access(user_id=user_id, company_id=normalized_company)
        return normalized_scope, normalized_company

    def _authorize_company(self, *, user_id: str, company_id: str) -> None:
        self._require_user(user_id)
        TaxService(self.database).require_access(user_id=user_id, company_id=company_id)

    def _accessible_companies(self, user_id: str) -> list[str]:
        self._require_user(user_id)
        rows = self.database.connection.execute(
            """SELECT company_id FROM user_company_permissions
               WHERE user_id=? AND can_view=1 ORDER BY company_id""",
            (user_id,),
        ).fetchall()
        return [str(row["company_id"]) for row in rows]

    def create(self, *, user_id: str, key: str, value: str, scope: str | None = None, company_id: str | None = None) -> dict[str, Any]:
        normalized_scope, normalized_company = self._authorize_scope(user_id=user_id, scope=scope, company_id=company_id)
        return self.store.create(user_id=user_id, scope=normalized_scope, company_id=normalized_company, key=key, value=value)

    def list(self, *, user_id: str, company_id: str | None = None) -> dict[str, Any]:
        self._require_user(user_id)
        if company_id is not None:
            self._authorize_company(user_id=user_id, company_id=company_id)
        if company_id is None:
            accessible_companies = self._accessible_companies(user_id)
            preferences = self.store.list_all_active(user_id=user_id, company_ids=accessible_companies)
        else:
            accessible_companies = [company_id]
            preferences = self.store.list_active(user_id=user_id, company_id=company_id)
        effective = self.store.effective(user_id=user_id, company_id=company_id)
        return {
            "status": "ok",
            "user_id": user_id,
            "company_id": company_id,
            "accessible_company_ids": accessible_companies,
            "preferences": preferences,
            "effective": effective,
        }

    def get(self, *, user_id: str, preference_id: str) -> dict[str, Any]:
        self._require_user(user_id)
        preference = self.store.get(preference_id=preference_id)
        if preference is None or preference["user_id"] != user_id:
            return {"status": "not_found", "summary": "偏好不存在。"}
        if preference["scope"] == "company":
            self._authorize_company(user_id=user_id, company_id=preference["company_id"])
        return {"status": "ok", "preference": preference}

    def update(self, *, user_id: str, preference_id: str, value: str, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_user(user_id)
        preference = self.store.get(preference_id=preference_id)
        if preference is None or preference["user_id"] != user_id:
            return {"status": "not_found", "summary": "偏好不存在。"}
        if preference["scope"] == "company":
            self._authorize_company(user_id=user_id, company_id=preference["company_id"])
        return self.store.update(preference_id=preference_id, value=value, expected_revision=expected_revision)

    def delete(self, *, user_id: str, preference_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        self._require_user(user_id)
        preference = self.store.get(preference_id=preference_id)
        if preference is None or preference["user_id"] != user_id:
            return {"status": "not_found", "summary": "偏好不存在。"}
        if preference["scope"] == "company":
            self._authorize_company(user_id=user_id, company_id=preference["company_id"])
        return self.store.delete(preference_id=preference_id, expected_revision=expected_revision)


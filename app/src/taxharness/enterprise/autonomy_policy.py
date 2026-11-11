"""Versioned, deterministic enterprise rules for invoice-field autonomy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from ..services import TaxService
from .auth import require_role
from .storage import utc_now


OUTCOMES = {"prepare_only", "single_confirm", "dual_review", "prohibited"}
# prepare_only blocks a commit entirely; dual_review allows it only through two distinct people.
_STRICTNESS = {"single_confirm": 1, "dual_review": 2, "prepare_only": 3, "prohibited": 4}
_TRUST_STATES = {"trusted", "unverified", "untrusted", "unknown"}
_FIELDS = {"purpose", "remarks"}
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_RULE_KEYS = {"id", "field", "currency", "amount_min", "amount_max", "source_type",
              "source_trust", "customer_risk_level", "outcome"}


class AutonomyPolicyConflict(ValueError):
    """A policy or preview changed since it was read."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _period(value: str) -> str:
    if not isinstance(value, str) or not _PERIOD.fullmatch(value):
        raise ValueError("期间必须为 YYYY-MM。")
    return value


def _text(value: Any, label: str, *, max_length: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"{label}须为 1 到 {max_length} 个字符。")
    return value.strip()


def _decimal_text(value: Any, label: str) -> str:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{label}须使用 Decimal 或十进制字符串。")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label}不是有效的十进制金额。") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label}须为非负有限金额。")
    return format(number, "f")


def _policy(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("策略必须是对象。")
    allowed = {"default_outcome", "unknown_outcome", "customer_risk_level", "source_trust_by_type", "rules"}
    if set(value) - allowed:
        raise ValueError("策略包含不支持的字段。")
    default_outcome = value.get("default_outcome", "single_confirm")
    unknown_outcome = value.get("unknown_outcome", "prepare_only")
    if not isinstance(default_outcome, str) or default_outcome not in OUTCOMES or not isinstance(unknown_outcome, str) or unknown_outcome not in OUTCOMES:
        raise ValueError("default_outcome 和 unknown_outcome 必须是受支持的审批结果。")

    risk = value.get("customer_risk_level")
    if risk is not None:
        risk = _text(risk, "机构配置的客户风险等级", max_length=60).casefold()

    trust_map = value.get("source_trust_by_type", {})
    if not isinstance(trust_map, dict):
        raise ValueError("source_trust_by_type 必须是来源类型到可信状态的对象。")
    normalized_trust = {}
    for source_type, trust in trust_map.items():
        key = _text(source_type, "来源类型", max_length=100)
        if not isinstance(trust, str) or trust not in _TRUST_STATES:
            raise ValueError("来源可信状态只支持 trusted、unverified、untrusted、unknown。")
        normalized_trust[key] = trust

    rules = value.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 250:
        raise ValueError("rules 必须是最多 250 条规则的数组。")
    normalized_rules = []
    seen_ids = set()
    for item in rules:
        if not isinstance(item, dict) or set(item) - _RULE_KEYS or not {"id", "outcome"}.issubset(item):
            raise ValueError("每条规则须有 id、outcome，且只使用受支持的条件字段。")
        rule_id = _text(item["id"], "规则编号", max_length=80)
        if rule_id in seen_ids:
            raise ValueError("规则编号不能重复。")
        seen_ids.add(rule_id)
        outcome = item["outcome"]
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise ValueError("规则 outcome 必须是受支持的审批结果。")
        rule: dict[str, Any] = {"id": rule_id, "outcome": outcome}
        field = item.get("field", "*")
        if not isinstance(field, str) or field not in _FIELDS | {"*"}:
            raise ValueError("field 只支持 purpose、remarks 或 *。")
        if field != "*":
            rule["field"] = field
        if "currency" in item:
            currency = _text(item["currency"], "币种", max_length=3).upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("币种须为三位大写代码，例如 CNY。")
            rule["currency"] = currency
        for key in ("amount_min", "amount_max"):
            if key in item:
                rule[key] = _decimal_text(item[key], key)
        if ("amount_min" in rule or "amount_max" in rule) and "currency" not in rule:
            raise ValueError("金额门槛必须同时指定币种。")
        if "amount_min" in rule and "amount_max" in rule and Decimal(rule["amount_min"]) > Decimal(rule["amount_max"]):
            raise ValueError("amount_min 不能大于 amount_max。")
        if "source_type" in item:
            rule["source_type"] = _text(item["source_type"], "来源类型", max_length=100)
        if "source_trust" in item:
            if not isinstance(item["source_trust"], str) or item["source_trust"] not in _TRUST_STATES:
                raise ValueError("source_trust 必须是受支持的可信状态。")
            rule["source_trust"] = item["source_trust"]
        if "customer_risk_level" in item:
            rule["customer_risk_level"] = _text(item["customer_risk_level"], "客户风险等级", max_length=60).casefold()
        if len(rule) == 2:
            raise ValueError("规则至少需要一个字段、金额、来源或风险条件；通用结果请使用 default_outcome。")
        normalized_rules.append(rule)

    return {"default_outcome": default_outcome, "unknown_outcome": unknown_outcome,
            "customer_risk_level": risk, "source_trust_by_type": normalized_trust,
            "rules": normalized_rules}


def _schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_autonomy_policy_versions (
        company_id TEXT NOT NULL,
        period TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','paused','revoked','superseded','disabled')),
        policy_json TEXT NOT NULL,
        preview_summary_json TEXT NOT NULL,
        preview_digest TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status_updated_by TEXT,
        status_updated_at TEXT,
        PRIMARY KEY(company_id,period,version),
        UNIQUE(company_id,period,operation_id),
        FOREIGN KEY(company_id) REFERENCES companies(company_id),
        FOREIGN KEY(created_by) REFERENCES enterprise_users(user_id)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS autonomy_policy_latest ON enterprise_autonomy_policy_versions(company_id,period,version DESC)")
    connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_autonomy_policy_events (
        event_id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        period TEXT NOT NULL,
        version INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor_user_id TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        details_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(company_id,period,operation_id),
        FOREIGN KEY(company_id,period,version)
            REFERENCES enterprise_autonomy_policy_versions(company_id,period,version),
        FOREIGN KEY(actor_user_id) REFERENCES enterprise_users(user_id)
    )""")


class AutonomyPolicyService:
    """Rule evaluation and lifecycle operations; it never edits invoice data."""

    def __init__(self, database):
        self.database = database
        self.connection = database.connection if hasattr(database, "connection") else database
        _schema(self.connection)

    def _authorize(self, user: dict, company_id: str, *, action: str) -> dict:
        if not isinstance(user, dict) or not isinstance(user.get("user_id"), str):
            raise PermissionError("需要当前已认证的企业用户。")
        if action in {"publish", "lifecycle", "preview", "replay"}:
            allowed = {"admin"} if action in {"publish", "lifecycle"} else {"admin", "accountant", "reviewer"}
        elif action == "review":
            allowed = {"reviewer"}
        else:
            allowed = {"accountant"}
        membership = require_role(self.database, user, company_id, allowed)
        if action in {"prepare", "confirm", "review"} or (action in {"preview", "replay"} and not membership["is_admin"]):
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=company_id,
                                                     modify=action in {"prepare", "confirm"})
        if self.connection.execute("SELECT 1 FROM companies WHERE company_id=?", (company_id,)).fetchone() is None:
            raise ValueError("企业不存在。")
        return membership

    def _latest(self, company_id: str, period: str):
        return self.connection.execute("""SELECT * FROM enterprise_autonomy_policy_versions
            WHERE company_id=? AND period=? ORDER BY version DESC LIMIT 1""", (company_id, period)).fetchone()

    def current(self, *, user: dict, company_id: str, period: str) -> dict[str, Any]:
        period = _period(period)
        self._authorize(user, company_id, action="preview")
        row = self._latest(company_id, period)
        config, state, version = self._resolve(row)
        if row and state == "paused":
            config = json.loads(row["policy_json"])
        return {"company_id": company_id, "period": period, "version": version, "status": state, "policy": config}

    def _review_required(self, company_id: str) -> bool:
        row = self.connection.execute("SELECT require_independent_review FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
        return True if row is None else bool(row["require_independent_review"])

    def _current_cases(self, company_id: str, period: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("""SELECT i.company_id,i.invoice_id,i.issue_date,i.purpose,i.remarks,
            x.amount_text,x.currency,x.source_status,l.source_system AS source_type
            FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
            LEFT JOIN enterprise_invoice_links l USING(company_id,invoice_id)
            WHERE i.company_id=? AND substr(i.issue_date,1,7)=? ORDER BY i.invoice_id""", (company_id, period)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _basis_digest(cases: list[dict[str, Any]]) -> str:
        basis = [{key: case.get(key) for key in ("company_id", "invoice_id", "issue_date", "purpose", "remarks",
                                                  "amount_text", "currency", "source_status", "source_type")}
                 for case in cases]
        return _digest(basis)

    def _resolve(self, latest) -> tuple[dict[str, Any] | None, str, int]:
        if latest is None:
            return None, "none", 0
        status = latest["status"]
        return (json.loads(latest["policy_json"]) if status == "active" else None,
                status, int(latest["version"]))

    @staticmethod
    def _decision(policy: dict[str, Any] | None, state: str, case: dict[str, Any], field: str,
                  legacy_review_required: bool) -> dict[str, Any]:
        if state == "paused":
            outcome, matched, uncertain, reason = "prepare_only", [], [], "策略已暂停，当前只允许准备。"
        elif policy is None:
            outcome, matched, uncertain, reason = "single_confirm", [], [], "未启用自治策略，沿用会计确认和企业现有独立复核配置。"
        else:
            source_type = str(case.get("source_type") or "unknown").strip() or "unknown"
            source_trust = policy["source_trust_by_type"].get(source_type, "unknown")
            risk = (policy.get("customer_risk_level") or "unknown").casefold()
            matched, uncertain = [], []
            outcomes = []
            amount = Decimal(_decimal_text(case.get("amount_text", "0"), "票据金额"))
            currency = str(case.get("currency") or "").upper()
            for rule in policy["rules"]:
                if rule.get("field", "*") not in {"*", field}:
                    continue
                if "currency" in rule and currency != rule["currency"]:
                    continue
                if "amount_min" in rule and amount < Decimal(rule["amount_min"]):
                    continue
                if "amount_max" in rule and amount > Decimal(rule["amount_max"]):
                    continue
                unresolved = False
                if "source_type" in rule:
                    if source_type == "unknown" and rule["source_type"] != "unknown":
                        unresolved = True
                    elif source_type != rule["source_type"]:
                        continue
                if "source_trust" in rule:
                    if source_trust == "unknown" and rule["source_trust"] != "unknown":
                        unresolved = True
                    elif source_trust != rule["source_trust"]:
                        continue
                if "customer_risk_level" in rule:
                    if risk == "unknown" and rule["customer_risk_level"] != "unknown":
                        unresolved = True
                    elif risk != rule["customer_risk_level"]:
                        continue
                if unresolved:
                    uncertain.append(rule["id"])
                else:
                    matched.append(rule["id"])
                    outcomes.append(rule["outcome"])
            if uncertain:
                outcomes.append(policy["unknown_outcome"])
            if outcomes:
                outcome = max(outcomes, key=lambda item: _STRICTNESS[item])
            else:
                outcome = policy["default_outcome"]
            reason = ("匹配规则：" + "、".join(matched) if matched else "未匹配具体规则，使用默认结果。")
            if uncertain:
                reason += " 部分条件缺少可信输入，按未知条件结果处理：" + "、".join(uncertain) + "。"

        requires_independent = outcome == "dual_review" or (outcome == "single_confirm" and legacy_review_required)
        return {"outcome": outcome, "matched_rules": matched, "undetermined_rules": uncertain,
                "requires_independent_review": requires_independent, "reason": reason}

    def _impact(self, cases: list[dict[str, Any]], before_policy, before_state: str,
                after_policy: dict[str, Any], legacy_review_required: bool, *, include_items: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        before_counts = {outcome: 0 for outcome in sorted(OUTCOMES)}
        after_counts = {outcome: 0 for outcome in sorted(OUTCOMES)}
        transitions: dict[str, int] = {}
        items = []
        independent_count = 0
        for case in cases:
            for field in ("purpose", "remarks"):
                before = self._decision(before_policy, before_state, case, field, legacy_review_required)
                after = self._decision(after_policy, "active", case, field, legacy_review_required)
                before_counts[before["outcome"]] += 1
                after_counts[after["outcome"]] += 1
                transition = f"{before['outcome']}->{after['outcome']}"
                transitions[transition] = transitions.get(transition, 0) + 1
                independent_count += int(after["requires_independent_review"])
                if include_items:
                    items.append({"task_id": case.get("task_id"), "invoice_id": case["invoice_id"], "field": field,
                                  "before": before["outcome"], "after": after["outcome"],
                                  "matched_rules": after["matched_rules"], "undetermined_rules": after["undetermined_rules"]})
        summary = {"invoice_count": len(cases), "field_decision_count": len(cases) * 2,
                   "before": before_counts, "after": after_counts,
                   "transitions": dict(sorted(transitions.items())),
                   "changed_field_count": sum(count for transition, count in transitions.items()
                                              if transition.split("->", 1)[0] != transition.split("->", 1)[1]),
                   "after_requires_independent_review_count": independent_count,
                   "legacy_require_independent_review": legacy_review_required,
                   "basis_digest": self._basis_digest(cases)}
        return summary, items

    def _preview(self, company_id: str, period: str, config: dict[str, Any]) -> dict[str, Any]:
        latest = self._latest(company_id, period)
        before_policy, before_state, current_version = self._resolve(latest)
        cases = self._current_cases(company_id, period)
        legacy_review_required = self._review_required(company_id)
        summary, _ = self._impact(cases, before_policy, before_state, config, legacy_review_required)
        summary["current_policy_version"] = current_version
        summary["current_policy_status"] = before_state
        summary_hash = _digest(summary)
        preview_digest = _digest({"company_id": company_id, "period": period,
                                  "expected_version": current_version, "policy": config,
                                  "summary_hash": summary_hash})
        return {"status": "ok", "preview_only": True, "company_id": company_id, "period": period,
                "current_version": current_version, "current_status": before_state,
                "expected_version": current_version, "policy": config, "summary": summary,
                "summary_hash": summary_hash, "preview_digest": preview_digest}

    def preview(self, *, user: dict, company_id: str, period: str, policy: dict[str, Any]) -> dict[str, Any]:
        """Read-only impact preview for the current invoice set in one period."""
        period = _period(period)
        self._authorize(user, company_id, action="preview")
        return self._preview(company_id, period, _policy(policy))

    def _valid_preview(self, preview: dict[str, Any], company_id: str, period: str) -> dict[str, Any]:
        if not isinstance(preview, dict) or preview.get("company_id") != company_id or preview.get("period") != period:
            raise ValueError("发布必须使用当前企业期间生成的预览。")
        config = _policy(preview.get("policy"))
        expected_version = preview.get("expected_version")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("预览缺少有效的当前版本。")
        summary = preview.get("summary")
        if not isinstance(summary, dict) or preview.get("summary_hash") != _digest(summary):
            raise ValueError("预览摘要无效，请重新预览。")
        expected_digest = _digest({"company_id": company_id, "period": period,
                                   "expected_version": expected_version, "policy": config,
                                   "summary_hash": preview["summary_hash"]})
        if preview.get("preview_digest") != expected_digest:
            raise ValueError("预览内容已变化，请重新预览。")
        return {"policy": config, "expected_version": expected_version, "summary": summary,
                "summary_hash": preview["summary_hash"], "preview_digest": expected_digest}

    def _replay_command(self, *, company_id: str, period: str, operation_id: str, request_hash: str):
        row = self.connection.execute("""SELECT request_hash,result_json FROM enterprise_autonomy_policy_events
            WHERE company_id=? AND period=? AND operation_id=?""", (company_id, period, operation_id)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ValueError("operation_id 已用于不同的自治策略操作。")
        return json.loads(row["result_json"])

    def _event(self, *, company_id: str, period: str, version: int, operation_id: str,
               action: str, user_id: str, request_hash: str, details: dict[str, Any], result: dict[str, Any]) -> None:
        self.connection.execute("""INSERT INTO enterprise_autonomy_policy_events
            (event_id,company_id,period,version,operation_id,action,actor_user_id,request_hash,details_json,result_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid4().hex, company_id, period, version, operation_id, action, user_id, request_hash,
             _json(details), _json(result), utc_now()))

    def publish(self, *, user: dict, company_id: str, period: str, expected_version: int,
                preview: dict[str, Any], operation_id: str | None = None) -> dict[str, Any]:
        """Publish exactly the previewed policy if both version and impact basis are current."""
        period = _period(period)
        previewed = self._valid_preview(preview, company_id, period)
        if type(expected_version) is not int or expected_version < 0 or expected_version != previewed["expected_version"]:
            raise ValueError("expected_version 必须与预览中的当前版本一致。")
        operation_id = _text(operation_id, "operation_id", max_length=200) if operation_id is not None else "publish-" + previewed["preview_digest"]
        request_hash = _digest({"action": "publish", "expected_version": expected_version,
                                "preview_digest": previewed["preview_digest"]})
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._authorize(user, company_id, action="publish")
            replay = self._replay_command(company_id=company_id, period=period, operation_id=operation_id,
                                          request_hash=request_hash)
            if replay is not None:
                connection.commit()
                return replay
            latest = self._latest(company_id, period)
            current_version = int(latest["version"]) if latest else 0
            if current_version != expected_version:
                raise AutonomyPolicyConflict("策略版本已更新，请重新预览。")
            fresh = self._preview(company_id, period, previewed["policy"])
            if fresh["summary_hash"] != previewed["summary_hash"] or fresh["preview_digest"] != previewed["preview_digest"]:
                raise AutonomyPolicyConflict("票据、复核配置或预览摘要已变化，请重新预览。")
            version = current_version + 1
            now = utc_now()
            if latest and latest["status"] == "active":
                connection.execute("""UPDATE enterprise_autonomy_policy_versions SET status='superseded',
                    status_updated_by=?,status_updated_at=? WHERE company_id=? AND period=? AND version=?""",
                    (user["user_id"], now, company_id, period, current_version))
            connection.execute("""INSERT INTO enterprise_autonomy_policy_versions
                (company_id,period,version,status,policy_json,preview_summary_json,preview_digest,
                 operation_id,created_by,created_at,status_updated_by,status_updated_at)
                VALUES(?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (company_id, period, version, _json(previewed["policy"]), _json(previewed["summary"]),
                 previewed["preview_digest"], operation_id, user["user_id"], now, user["user_id"], now))
            result = {"status": "published", "company_id": company_id, "period": period,
                      "version": version, "previous_version": current_version,
                      "preview_digest": previewed["preview_digest"],
                      "summary": previewed["summary"], "policy": previewed["policy"]}
            self._event(company_id=company_id, period=period, version=version, operation_id=operation_id,
                        action="publish", user_id=user["user_id"], request_hash=request_hash,
                        details={"policy": previewed["policy"], "preview_digest": previewed["preview_digest"]}, result=result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def _change_status(self, *, user: dict, company_id: str, period: str, version: int,
                       target_status: str, action: str, operation_id: str | None = None,
                       expected_version: int | None = None) -> dict[str, Any]:
        period = _period(period)
        if type(version) is not int or version < 1:
            raise ValueError("version 必须是正整数。")
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("expected_version 必须是非负整数。")
        operation_id = _text(operation_id, "operation_id", max_length=200) if operation_id is not None else "policy-" + uuid4().hex
        request_hash = _digest({"action": action, "version": version, "expected_version": expected_version,
                                "target_status": target_status})
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._authorize(user, company_id, action="lifecycle")
            replay = self._replay_command(company_id=company_id, period=period, operation_id=operation_id,
                                          request_hash=request_hash)
            if replay is not None:
                connection.commit()
                return replay
            latest = self._latest(company_id, period)
            if expected_version is not None and (latest is None or int(latest["version"]) != expected_version or version != expected_version):
                raise AutonomyPolicyConflict("策略版本已更新，请刷新后重试。")
            row = self.connection.execute("""SELECT status FROM enterprise_autonomy_policy_versions
                WHERE company_id=? AND period=? AND version=?""", (company_id, period, version)).fetchone()
            if row is None:
                raise ValueError("自治策略版本不存在。")
            current_status = row["status"]
            allowed = {"pause": {"active", "paused"}, "resume": {"paused", "active"}, "revoke": {"active", "paused", "revoked"},
                       "deactivate": {"active", "paused", "superseded", "disabled"}}[action]
            if current_status not in allowed:
                raise AutonomyPolicyConflict("此策略版本已失效，不能执行该状态操作。")
            now = utc_now()
            if current_status != target_status:
                connection.execute("""UPDATE enterprise_autonomy_policy_versions SET status=?,
                    status_updated_by=?,status_updated_at=? WHERE company_id=? AND period=? AND version=?""",
                    (target_status, user["user_id"], now, company_id, period, version))
            result = {"status": target_status, "company_id": company_id, "period": period,
                      "version": version, "previous_status": current_status,
                      "policy_effect": "prepare_only" if target_status == "paused" else "legacy_controls" if target_status in {"revoked", "disabled"} else "active"}
            self._event(company_id=company_id, period=period, version=version, operation_id=operation_id,
                        action=action, user_id=user["user_id"], request_hash=request_hash,
                        details={"from": current_status, "to": target_status}, result=result)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def pause(self, *, user: dict, company_id: str, period: str, expected_version: int,
              operation_id: str | None = None) -> dict[str, Any]:
        return self._change_status(user=user, company_id=company_id, period=period, version=expected_version,
                                   target_status="paused", action="pause", operation_id=operation_id,
                                   expected_version=expected_version)

    def resume(self, *, user: dict, company_id: str, period: str, expected_version: int,
               operation_id: str | None = None) -> dict[str, Any]:
        return self._change_status(user=user, company_id=company_id, period=period, version=expected_version,
                                   target_status="active", action="resume", operation_id=operation_id,
                                   expected_version=expected_version)

    def revoke(self, *, user: dict, company_id: str, period: str, expected_version: int,
               operation_id: str | None = None) -> dict[str, Any]:
        return self._change_status(user=user, company_id=company_id, period=period, version=expected_version,
                                   target_status="revoked", action="revoke", operation_id=operation_id,
                                   expected_version=expected_version)

    def deactivate(self, *, user: dict, company_id: str, period: str, version: int,
                   operation_id: str | None = None) -> dict[str, Any]:
        return self._change_status(user=user, company_id=company_id, period=period, version=version,
                                   target_status="disabled", action="deactivate", operation_id=operation_id)

    def evaluate(self, *, user: dict, company_id: str, period: str, invoice_id: str,
                 field: str, action: str, task_id: str | None = None,
                 submitted_by: str | None = None) -> dict[str, Any]:
        """Re-evaluate one live ticket field on the caller's existing DB transaction."""
        period = _period(period)
        if field not in _FIELDS:
            raise ValueError("field 只支持 purpose 或 remarks。")
        if action not in {"prepare", "confirm", "review"}:
            raise ValueError("action 只支持 prepare、confirm 或 review。")
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError("invoice_id 不能为空。")
        self._authorize(user, company_id, action=action)
        if task_id:
            row = self.connection.execute("""SELECT i.company_id,i.invoice_id,i.issue_date,i.purpose,i.remarks,
                x.amount_text,x.currency,x.source_status,l.source_system AS source_type,w.period
                FROM enterprise_work_items w JOIN enterprise_task_invoices ti
                  ON ti.task_id=w.task_id AND ti.company_id=w.company_id
                JOIN invoices i ON i.company_id=ti.company_id AND i.invoice_id=ti.invoice_id
                JOIN enterprise_invoice_extra x ON x.company_id=i.company_id AND x.invoice_id=i.invoice_id
                LEFT JOIN enterprise_invoice_links l ON l.company_id=i.company_id AND l.invoice_id=i.invoice_id
                WHERE w.task_id=? AND w.company_id=? AND w.period=? AND ti.invoice_id=?""",
                (task_id, company_id, period, invoice_id)).fetchone()
        else:
            row = self.connection.execute("""SELECT i.company_id,i.invoice_id,i.issue_date,i.purpose,i.remarks,
                x.amount_text,x.currency,x.source_status,l.source_system AS source_type,substr(i.issue_date,1,7) AS period
                FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                LEFT JOIN enterprise_invoice_links l USING(company_id,invoice_id)
                WHERE i.company_id=? AND i.invoice_id=? AND substr(i.issue_date,1,7)=?""",
                (company_id, invoice_id, period)).fetchone()
        if row is None:
            raise ValueError("发票不属于当前企业期间或工作项。")
        latest = self._latest(company_id, period)
        policy, state, version = self._resolve(latest)
        legacy_review_required = self._review_required(company_id)
        decision = self._decision(policy, state, dict(row), field, legacy_review_required)
        outcome = decision["outcome"]
        allowed = action == "prepare" and outcome != "prohibited" or action in {"confirm", "review"} and outcome in {"single_confirm", "dual_review"}
        independence_reason = None
        if action == "review" and allowed and decision["requires_independent_review"]:
            if not isinstance(submitted_by, str) or not submitted_by:
                allowed = False
                independence_reason = "缺少本次修改提交人，无法完成独立复核检查。"
            elif submitted_by == user["user_id"]:
                allowed = False
                independence_reason = "当前策略要求由不同于提交人的复核人审批。"
        return {"status": "ok", "company_id": company_id, "period": period,
                "invoice_id": invoice_id, "field": field, "action": action,
                "policy_version": version, "policy_status": state, **decision,
                "action_allowed": allowed,
                "summary": independence_reason or ("允许此动作；现有会计确认及独立复核配置继续生效。" if allowed else "当前策略不允许此动作。")}

    def _work_item_cases(self, *, user: dict, company_id: str, work_item_ids: list[str]) -> list[dict[str, Any]]:
        if not isinstance(work_item_ids, list) or not work_item_ids or any(not isinstance(item, str) or not item for item in work_item_ids):
            raise ValueError("work_item_ids 须为非空工作项编号数组。")
        ids = list(dict.fromkeys(work_item_ids))
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(f"""SELECT w.task_id,w.company_id,w.period,w.status AS work_status,
            COALESCE(s.invoice_id,ti.invoice_id) AS invoice_id,
            COALESCE(s.issue_date,i.issue_date) AS issue_date,
            COALESCE(s.purpose,i.purpose) AS purpose,COALESCE(s.remarks,i.remarks) AS remarks,
            COALESCE(s.amount_text,x.amount_text) AS amount_text,COALESCE(s.currency,x.currency) AS currency,
            COALESCE(s.source_status,x.source_status) AS source_status,
            COALESCE(s.source_system,l.source_system) AS source_type,
            CASE WHEN s.task_id IS NOT NULL THEN 1 ELSE 0 END AS is_snapshot
            FROM enterprise_work_items w
            LEFT JOIN enterprise_task_invoices ti ON ti.task_id=w.task_id AND ti.company_id=w.company_id
            LEFT JOIN enterprise_task_invoice_snapshots s ON s.task_id=w.task_id AND s.company_id=w.company_id
                AND (s.invoice_id=ti.invoice_id OR ti.invoice_id IS NULL)
            LEFT JOIN invoices i ON i.company_id=ti.company_id AND i.invoice_id=ti.invoice_id
            LEFT JOIN enterprise_invoice_extra x ON x.company_id=i.company_id AND x.invoice_id=i.invoice_id
            LEFT JOIN enterprise_invoice_links l ON l.company_id=i.company_id AND l.invoice_id=i.invoice_id
            WHERE w.company_id=? AND w.task_id IN ({placeholders})
            ORDER BY w.task_id,invoice_id""", (company_id, *ids)).fetchall()
        found = {row["task_id"] for row in rows}
        if found != set(ids):
            raise PermissionError("工作项不存在或不属于当前企业。")
        cases = []
        snapshot_by_task = {}
        missing_snapshot_by_task = set()
        for row in rows:
            item = dict(row)
            if item["invoice_id"] is None:
                continue
            snapshot_by_task[item["task_id"]] = snapshot_by_task.get(item["task_id"], False) or bool(item["is_snapshot"])
            if item["work_status"] == "closed" and not item["is_snapshot"]:
                missing_snapshot_by_task.add(item["task_id"])
                continue
            item["company_id"] = company_id
            item["source_type"] = item.get("source_type") or "unknown"
            cases.append(item)
        for task_id in ids:
            task = self.connection.execute("SELECT status FROM enterprise_work_items WHERE company_id=? AND task_id=?", (company_id, task_id)).fetchone()
            if task["status"] == "closed" and (not snapshot_by_task.get(task_id) or task_id in missing_snapshot_by_task):
                raise ValueError("历史工作项没有关闭时快照，不能用当前票据冒充历史数据。")
        return cases

    def replay(self, *, user: dict, company_id: str, period: str,
               work_item_ids: list[str], policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compare a candidate policy with the current policy on selected stored work items."""
        period = _period(period)
        self._authorize(user, company_id, action="replay")
        candidate = _policy(policy) if policy is not None else None
        latest = self._latest(company_id, period)
        before_policy, before_state, before_version = self._resolve(latest)
        after_policy = candidate if candidate is not None else before_policy
        cases = self._work_item_cases(user=user, company_id=company_id, work_item_ids=work_item_ids)
        summary, items = self._impact(cases, before_policy, before_state, after_policy,
                                      self._review_required(company_id), include_items=True)
        periods = sorted({str(case["period"]) for case in cases})
        return {"status": "ok", "company_id": company_id, "policy_period": period,
                "policy_version": before_version, "replayed_periods": periods,
                "work_item_ids": list(dict.fromkeys(work_item_ids)), "summary": summary, "items": items}

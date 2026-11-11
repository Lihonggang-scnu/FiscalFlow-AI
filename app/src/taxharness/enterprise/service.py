from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..errors import RuleConflictError, RuleNotFoundError
from ..invoice_plans import _condition_met, _condition_valid
from ..rule_manager import RuleManager
from ..rules import applicable_rule, condition_execution
from ..services import TaxService, _clean_text
from ..storage import row_dict
from .auth import membership, require_role
from .field_evaluation import evaluate_draft_fields
from .storage import CURRENCY_DIGITS as _CURRENCY_DIGITS, EnterpriseDatabase, utc_now


ACTIVE_TASKS = ("open", "in_progress", "needs_review", "returned")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _record_actor_message(connection, *, user_id: str, company_id: str, task_id: str,
                          turn_id: str | None, content: str | None) -> None:
    if not turn_id or not content:
        return
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    prior = connection.execute("""SELECT content_hash FROM enterprise_actor_messages
        WHERE turn_id=? AND user_id=? AND company_id=? AND task_id=?""", (turn_id, user_id, company_id, task_id)).fetchone()
    if prior and prior["content_hash"] != digest:
        raise ValueError("同一对话来源的内容已变化，请重新办理。")
    connection.execute("""INSERT OR IGNORE INTO enterprise_actor_messages
        (turn_id,user_id,company_id,task_id,content,content_hash,created_at) VALUES(?,?,?,?,?,?,?)""",
        (turn_id, user_id, company_id, task_id, content, digest, utc_now()))


def _period(value: str) -> str:
    if not _PERIOD_RE.fullmatch(str(value)):
        raise ValueError("期间必须为 YYYY-MM。")
    return str(value)


def invoice_key(company_id: str, source_system: str, source_invoice_id: str) -> str:
    source = "\0".join((company_id, source_system, source_invoice_id)).encode("utf-8")
    return "ei-" + hashlib.sha256(source).hexdigest()[:40]


_REVIEW_FIELDS = ("invoice_id", "issue_date", "seller_name", "project_name", "amount_text", "currency",
                  "source_status", "purpose", "remarks", "revision", "task_status")


def _confirmed_field_policies(connection, *, task_id: str, invoice_id: str) -> dict:
    rows = connection.execute("""SELECT r.confirmed_by,r.result_json FROM enterprise_confirmation_receipts r
        JOIN enterprise_drafts d USING(draft_id) WHERE d.task_id=? AND r.invoice_id=?
        ORDER BY r.created_at DESC,r.rowid DESC""", (task_id, invoice_id)).fetchall()
    found, seen = {}, set()
    for row in rows:
        result = json.loads(row["result_json"])
        for field in result.get("confirmed_fields", []):
            if field in seen:
                continue
            seen.add(field)
            decision = result.get("policy_decisions", {}).get(field)
            if decision:
                found[field] = {"confirmed_by": row["confirmed_by"], "decision": decision}
    return found


def _review_snapshot(database: EnterpriseDatabase, user: dict, task_id: str, invoice: dict) -> dict:
    from .professional import review_basis
    connection = database.connection
    task = connection.execute("SELECT company_id,period FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
    setting = connection.execute("SELECT default_purpose_policy,require_independent_review FROM enterprise_company_settings WHERE company_id=?", (task["company_id"],)).fetchone()
    try:
        rule = applicable_rule(connection, kind="invoice_purpose", company_id=task["company_id"], month=task["period"])
        method = {key: rule.get(key) for key in ("rule_id", "version", "name", "condition", "status", "effective_from")}
    except RuleNotFoundError:
        method = {"rule_status": "unavailable", "default_purpose_policy": setting["default_purpose_policy"]}
    except RuleConflictError:
        method = {"rule_status": "conflict"}
    drafts = connection.execute("""SELECT draft_id,status,draft_revision,proposed_purpose,proposed_remarks,
        evidence_quote,field_context_json,response_id FROM enterprise_drafts
        WHERE task_id=? AND invoice_id=? AND (status IN ('proposed','clarification','conflict','confirmed'))
        AND superseded_by IS NULL ORDER BY draft_id""", (task_id, invoice["invoice_id"])).fetchall()
    basis = [{**{key: row[key] for key in ("draft_id", "status", "draft_revision", "proposed_purpose", "proposed_remarks", "evidence_quote", "response_id")},
              "field_context": json.loads(row["field_context_json"] or "{}")} for row in drafts]
    professional = review_basis(database, user=user, company_id=task["company_id"], period=task["period"], invoice_ids=[invoice["invoice_id"]])
    review = {"method": method, "require_independent_review": bool(setting["require_independent_review"]),
              "drafts": basis, "professional_method": professional["method"], "professional": professional["invoices"][0]}
    policies = _confirmed_field_policies(connection, task_id=task_id, invoice_id=invoice["invoice_id"])
    if policies:
        review["autonomy_policy"] = policies
    return {"task_id": task_id, **{field: invoice[field] for field in _REVIEW_FIELDS}, "review_basis": review}


def _review_token(snapshot: dict) -> str:
    return hashlib.sha256(_json(snapshot).encode("utf-8")).hexdigest()


def _review_submitter(connection, *, task, invoice_id: str) -> str:
    receipt = connection.execute("""SELECT r.confirmed_by FROM enterprise_confirmation_receipts r
        JOIN enterprise_drafts d USING(draft_id) WHERE r.invoice_id=? AND r.company_id=? AND d.task_id=?
        ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1""", (invoice_id, task["company_id"], task["task_id"])).fetchone()
    if receipt:
        return receipt["confirmed_by"]
    imported = connection.execute("""SELECT i.user_id FROM enterprise_import_rows r JOIN enterprise_imports i USING(import_id)
        WHERE r.company_id=? AND r.invoice_id=? ORDER BY i.created_at DESC LIMIT 1""", (task["company_id"], invoice_id)).fetchone()
    return imported["user_id"] if imported else task["created_by"]


class EnterpriseService:
    def __init__(self, database: EnterpriseDatabase):
        self.database = database
        from .autonomy_policy import AutonomyPolicyService
        self.autonomy_policy = AutonomyPolicyService(database)

    def field_decision_versions(self, task_id: str) -> dict[str, dict[str, int]]:
        versions: dict[str, dict[str, int]] = {}
        for row in self.database.connection.execute(
                "SELECT invoice_id,field,decision_revision FROM enterprise_field_decisions WHERE task_id=?", (task_id,)):
            versions.setdefault(row["invoice_id"], {})[row["field"]] = int(row["decision_revision"])
        return versions

    @staticmethod
    def _mark_field_decisions(connection, task_id: str, invoice_id: str | None, fields: list[str]) -> None:
        if not invoice_id:
            return
        for field in fields:
            connection.execute("""INSERT INTO enterprise_field_decisions(task_id,invoice_id,field,decision_revision)
                VALUES(?,?,?,1) ON CONFLICT(task_id,invoice_id,field)
                DO UPDATE SET decision_revision=decision_revision+1""", (task_id, invoice_id, field))

    def _role(self, user: dict, company_id: str, roles: set[str]) -> dict:
        return require_role(self.database, user, company_id, roles)

    def list_companies(self, user: dict) -> list[dict]:
        if user["is_admin"]:
            rows = self.database.connection.execute(
                """SELECT c.company_id,c.name,c.short_name,s.default_currency,s.default_purpose_policy,s.require_independent_review,'admin' AS role
                   FROM companies c JOIN enterprise_company_settings s USING(company_id) ORDER BY c.name,c.company_id"""
            ).fetchall()
        else:
            rows = self.database.connection.execute(
                """SELECT c.company_id,c.name,c.short_name,s.default_currency,s.default_purpose_policy,s.require_independent_review,m.role
                   FROM companies c JOIN enterprise_company_settings s USING(company_id)
                   JOIN enterprise_memberships m USING(company_id) WHERE m.user_id=? AND m.active=1
                   AND m.role<>'requester' AND EXISTS (SELECT 1 FROM user_company_permissions p
                       WHERE p.user_id=m.user_id AND p.company_id=m.company_id AND p.can_view=1)
                   ORDER BY c.name,c.company_id""",
                (user["user_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_company(self, *, actor: dict, name: str, currency: str = "CNY", require_independent_review: bool = True) -> dict:
        if not actor["is_admin"]:
            raise PermissionError("只有系统管理员能创建企业。")
        name = name.strip()
        currency = currency.strip().upper()
        if not name or currency not in _CURRENCY_DIGITS:
            raise ValueError("企业名称不能为空；当前仅支持两位小数币种 CNY、GBP、USD、EUR。")
        company_id = "ent-" + uuid4().hex[:20]
        short_name = name[:24]
        self.database.connection.execute("BEGIN IMMEDIATE")
        try:
            self.database.connection.execute("INSERT INTO companies(company_id,name,short_name) VALUES(?,?,?)", (company_id, name, short_name))
            self.database.connection.execute(
                "INSERT INTO enterprise_company_settings(company_id,default_currency,require_independent_review,created_at) VALUES(?,?,?,?)",
                (company_id, currency, int(require_independent_review), utc_now()),
            )
            for admin in self.database.connection.execute("SELECT user_id FROM enterprise_users WHERE is_admin=1 AND active=1").fetchall():
                self.database.connection.execute("INSERT INTO user_company_permissions(user_id,company_id,can_view,can_modify) VALUES(?,?,1,1)", (admin["user_id"], company_id))
            self.database.connection.commit()
        except Exception:
            self.database.connection.rollback()
            raise
        return {"company_id": company_id, "name": name, "short_name": short_name, "default_currency": currency, "require_independent_review": bool(require_independent_review)}

    def update_company_settings(self, *, actor: dict, company_id: str, default_currency: str, require_independent_review: bool, allowed_dispositions: list[str], default_purpose_policy: str = "all_blank") -> dict:
        if not actor["is_admin"]:
            raise PermissionError("只有系统管理员能修改企业设置。")
        currency = default_currency.upper()
        if currency not in _CURRENCY_DIGITS:
            raise ValueError("币种映射不支持。")
        normalized = sorted(set(allowed_dispositions))
        if set(normalized) - {"waive_missing"}:
            raise ValueError("仅支持由复核人明确批准的 waive_missing 处置。")
        if default_purpose_policy not in {"all_blank", "none"}:
            raise ValueError("default_purpose_policy 必须是 all_blank 或 none。")
        self.database.connection.execute(
            "UPDATE enterprise_company_settings SET default_currency=?,default_purpose_policy=?,require_independent_review=?,allowed_dispositions_json=? WHERE company_id=?",
            (currency, default_purpose_policy, int(require_independent_review), _json(normalized), company_id),
        )
        row = self.database.connection.execute("SELECT * FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
        if row is None:
            raise ValueError("企业不存在。")
        result = dict(row)
        result["allowed_dispositions"] = json.loads(result.pop("allowed_dispositions_json"))
        result["require_independent_review"] = bool(result["require_independent_review"])
        return result

    def list_users(self) -> list[dict]:
        rows = self.database.connection.execute(
            """SELECT u.user_id,u.username,u.display_name,u.is_admin,u.active,u.created_at,
                      m.company_id,m.role,m.active AS membership_active,c.name AS company_name
               FROM enterprise_users u LEFT JOIN enterprise_memberships m USING(user_id)
               LEFT JOIN companies c USING(company_id) ORDER BY u.created_at,u.username,c.name"""
        ).fetchall()
        return [dict(row) for row in rows]

    def create_membership(self, *, user_id: str, company_id: str, role: str, active: bool = True) -> dict:
        from .auth import assign_membership
        assign_membership(self.database, user_id=user_id, company_id=company_id, role=role, active=active)
        return {"user_id": user_id, "company_id": company_id, "role": role, "active": active}

    def set_user_active(self, user_id: str, active: bool) -> dict:
        self.database.connection.execute("UPDATE enterprise_users SET active=? WHERE user_id=?", (int(active), user_id))
        if not active:
            self.database.connection.execute("UPDATE enterprise_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (utc_now(), user_id))
            self.database.connection.execute("UPDATE enterprise_memberships SET active=0 WHERE user_id=?", (user_id,))
            self.database.connection.execute("UPDATE user_company_permissions SET can_view=0,can_modify=0 WHERE user_id=?", (user_id,))
        row = self.database.connection.execute("SELECT user_id,username,display_name,is_admin,active FROM enterprise_users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("用户不存在。")
        return dict(row)

    def finance_summary(self, *, user: dict, company_id: str, period: str) -> dict:
        period = _period(period)
        self._role(user, company_id, {"accountant", "reviewer"})
        rows = self.database.connection.execute(
            """SELECT x.currency,x.amount_text FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
               WHERE i.company_id=? AND i.issue_date>=? AND i.issue_date<? AND x.source_status='active' ORDER BY i.issue_date,i.invoice_id""",
            (company_id, period + "-01", self._next_month(period)),
        ).fetchall()
        totals: dict[str, Decimal] = {}
        counts: dict[str, int] = {}
        for row in rows:
            currency = row["currency"]
            totals[currency] = totals.get(currency, Decimal(0)) + Decimal(row["amount_text"])
            counts[currency] = counts.get(currency, 0) + 1
        rules = []
        for kind in ("invoice_purpose", "amount_statistics", "bank_requirements"):
            try:
                rules.append(applicable_rule(self.database.connection, kind=kind, company_id=company_id, month=period))
            except Exception as error:
                rules.append({"kind": kind, "status": getattr(error, "code", "not_found"), "summary": str(error)})
        return {
            "status": "ok", "company_id": company_id, "period": period,
            "invoice_count": len(rows),
            "by_currency": [{"currency": currency, "invoice_count": counts[currency], "invoice_total": format(totals[currency], "f")} for currency in sorted(totals)],
            "rules": rules,
        }

    @staticmethod
    def _next_month(period: str) -> str:
        year, month = (int(item) for item in period.split("-"))
        return f"{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}-01"

    def invoice_page(self, *, user: dict, company_id: str, period: str, page: int = 1, page_size: int = 50) -> dict:
        period = _period(period)
        self._role(user, company_id, {"accountant", "reviewer"})
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        where = "i.company_id=? AND i.issue_date>=? AND i.issue_date<?"
        params = (company_id, period + "-01", self._next_month(period))
        total = int(self.database.connection.execute(f"SELECT COUNT(*) FROM invoices i WHERE {where}", params).fetchone()[0])
        rows = self.database.connection.execute(
            f"""SELECT i.*,e.currency,e.source_status,e.amount_text,e.unit_price_text,e.quantity_text,e.purpose_review_status,e.disposition,
                       l.source_system,l.source_invoice_id
                FROM invoices i JOIN enterprise_invoice_extra e USING(company_id,invoice_id)
                JOIN enterprise_invoice_links l USING(company_id,invoice_id) WHERE {where}
                ORDER BY i.issue_date,i.invoice_id LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"status": "ok", "page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size, "items": [dict(row) for row in rows]}

    def _ensure_task(self, *, company_id: str, period: str, user_id: str, invoice_ids: list[str], origin: str = "explicit") -> dict:
        period = _period(period)
        row = self.database.connection.execute(
            "SELECT * FROM enterprise_work_items WHERE company_id=? AND period=? AND status<>'closed' ORDER BY created_at DESC LIMIT 1",
            (company_id, period),
        ).fetchone()
        if row is None:
            task_id = _id("work-")
            now = utc_now()
            self.database.connection.execute(
                "INSERT INTO enterprise_work_items(task_id,company_id,period,status,revision,created_by,created_at,updated_at) VALUES(?,?,?,'open',1,?,?,?)",
                (task_id, company_id, period, user_id, now, now),
            )
            row = self.database.connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
        now = utc_now()
        self.database.connection.executemany(
            "INSERT OR IGNORE INTO enterprise_task_invoices(task_id,company_id,invoice_id,status,origin,updated_at) VALUES(?,?,?,'pending',?,?)",
            ((row["task_id"], company_id, invoice_id, origin, now) for invoice_id in invoice_ids),
        )
        return dict(row)

    def list_missing(self, *, user: dict, company_id: str, period: str, page: int = 1, page_size: int = 100) -> dict:
        period = _period(period)
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._role(user, company_id, {"accountant", "reviewer"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=company_id)
            settings = connection.execute("SELECT default_currency,default_purpose_policy FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
            if settings is None:
                raise ValueError("企业不存在。")
            rule = None
            rule_status = "no_applicable_rule"
            try:
                rule = applicable_rule(connection, kind="invoice_purpose", company_id=company_id, month=period)
                rule_status = "ok"
            except Exception as error:
                candidates = connection.execute(
                    """SELECT 1 FROM rules WHERE kind='invoice_purpose' AND effective_from<=?
                       AND (company_id IS NULL OR company_id=?) LIMIT 1""", (period + "-01", company_id)
                ).fetchone()
                if candidates is not None:
                    connection.commit()
                    return {"status": getattr(error, "code", "cannot_determine"), "summary": str(error), "missing": [], "missing_count": None, "rule_status": "cannot_determine", "page": page, "page_size": page_size}
                if settings["default_purpose_policy"] == "all_blank":
                    rule_status = "internal_all_blank"
                else:
                    connection.commit()
                    return {"status": "cannot_determine", "summary": "没有适用于该期间的发票用途规则，且企业内部默认策略为不自动判定。", "missing": [], "missing_count": None, "rule_status": "no_applicable_rule", "page": page, "page_size": page_size}

            empty = "(i.purpose IS NULL OR trim(i.purpose)='')"
            extra_clause = empty
            extra_params: list[Any] = []
            if rule is not None:
                execution = condition_execution(rule["condition"], kind="invoice_purpose")
                if execution["status"] == "unsupported":
                    connection.commit()
                    return {"status": "cannot_determine", "summary": execution["summary"], "missing": [], "missing_count": None, "rule_status": "cannot_determine", "page": page, "page_size": page_size}
                if execution.get("required") is False:
                    extra_clause = "0"
                elif "amount_cents" in execution:
                    if _CURRENCY_DIGITS.get(settings["default_currency"], 2) != 2:
                        connection.commit()
                        return {"status": "cannot_determine", "summary": f"规则门槛按两位小数表示，暂不能用于 {settings['default_currency']}。", "missing": [], "missing_count": None, "rule_status": "cannot_determine", "page": page, "page_size": page_size}
                    foreign_missing = connection.execute(
                        """SELECT 1 FROM invoices i JOIN enterprise_invoice_extra e USING(company_id,invoice_id)
                           WHERE i.company_id=? AND i.issue_date>=? AND i.issue_date<? AND e.source_status='active' AND e.currency<>? AND (i.purpose IS NULL OR trim(i.purpose)='') LIMIT 1""",
                        (company_id, period + "-01", self._next_month(period), settings["default_currency"]),
                    ).fetchone()
                    if foreign_missing:
                        connection.commit()
                        return {"status": "cannot_determine", "summary": f"规则门槛以企业币种 {settings['default_currency']} 表示，无法自动套用于其他币种空用途。", "missing": [], "missing_count": None, "rule_status": "cannot_determine", "page": page, "page_size": page_size}
                    extra_clause = f"{empty} AND i.amount_cents>=?"
                    extra_params.append(int(execution["amount_cents"]))

            closed_clause = "i.invoice_id NOT IN (SELECT ti.invoice_id FROM enterprise_task_invoices ti JOIN enterprise_work_items w USING(task_id) WHERE w.company_id=? AND w.period=? AND w.status='closed' AND ti.status IN ('approved','waived'))"
            where = f"i.company_id=? AND i.issue_date>=? AND i.issue_date<? AND e.source_status='active' AND {extra_clause} AND {closed_clause}"
            params = [company_id, period + "-01", self._next_month(period), *extra_params, company_id, period]
            count = int(connection.execute(
                f"SELECT COUNT(*) FROM invoices i JOIN enterprise_invoice_extra e USING(company_id,invoice_id) WHERE {where}", params
            ).fetchone()[0])
            active_task = connection.execute("SELECT * FROM enterprise_work_items WHERE company_id=? AND period=? AND status<>'closed' ORDER BY created_at DESC LIMIT 1", (company_id, period)).fetchone()
            if active_task is not None:
                protected: set[str] = set()
                for request in connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE task_id=? AND status<>'cancelled'", (active_task["task_id"],)):
                    protected.update(json.loads(request["target_invoice_ids_json"]))
                for draft in connection.execute("SELECT invoice_id,candidate_invoice_ids_json FROM enterprise_drafts WHERE task_id=? AND status IN ('proposed','clarification','conflict')", (active_task["task_id"],)):
                    protected.update(json.loads(draft["candidate_invoice_ids_json"]))
                    if draft["invoice_id"]:
                        protected.add(draft["invoice_id"])
                for row in connection.execute("""SELECT r.invoice_id FROM enterprise_confirmation_receipts r
                        JOIN enterprise_drafts d USING(draft_id) WHERE d.task_id=?
                    UNION SELECT invoice_id FROM enterprise_reviews WHERE task_id=?
                    UNION SELECT c.invoice_id FROM enterprise_import_conflicts c
                        JOIN enterprise_task_invoices ti USING(company_id,invoice_id)
                        WHERE ti.task_id=? AND c.status='open'""",
                    (active_task["task_id"], active_task["task_id"], active_task["task_id"])):
                    protected.add(row["invoice_id"])
                protected_clause = f" AND invoice_id NOT IN ({','.join('?' for _ in protected)})" if protected else ""
                connection.execute(
                    f"""DELETE FROM enterprise_task_invoices WHERE task_id=? AND origin='rule_missing' AND status='pending'
                        AND invoice_id NOT IN (SELECT i.invoice_id FROM invoices i
                            JOIN enterprise_invoice_extra e USING(company_id,invoice_id) WHERE {where}){protected_clause}""",
                    (active_task["task_id"], *params, *sorted(protected)),
                )
                remaining = int(connection.execute("SELECT COUNT(*) FROM enterprise_task_invoices WHERE task_id=?", (active_task["task_id"],)).fetchone()[0])
                used = connection.execute("SELECT 1 FROM enterprise_requests WHERE task_id=? UNION ALL SELECT 1 FROM enterprise_reviews WHERE task_id=? LIMIT 1", (active_task["task_id"], active_task["task_id"])).fetchone()
                if remaining == 0 and used is None:
                    connection.execute("DELETE FROM enterprise_work_items WHERE task_id=?", (active_task["task_id"],))
                    active_task = None
            task = dict(active_task) if active_task is not None else None
            if count:
                if active_task is None:
                    task_id = _id("work-")
                    now = utc_now()
                    connection.execute("INSERT INTO enterprise_work_items(task_id,company_id,period,status,revision,created_by,created_at,updated_at) VALUES(?,?,?,'open',1,?,?,?)", (task_id, company_id, period, user["user_id"], now, now))
                    task = dict(connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone())
                connection.execute(
                    f"""INSERT OR IGNORE INTO enterprise_task_invoices(task_id,company_id,invoice_id,status,origin,updated_at)
                        SELECT ?,i.company_id,i.invoice_id,'pending','rule_missing',? FROM invoices i
                        JOIN enterprise_invoice_extra e USING(company_id,invoice_id) WHERE {where}""",
                    (task["task_id"], utc_now(), *params),
                )
            rows = connection.execute(
                f"""SELECT i.invoice_id,i.issue_date,i.revision,e.currency,e.amount_text,l.source_system,l.source_invoice_id
                     FROM invoices i JOIN enterprise_invoice_extra e USING(company_id,invoice_id)
                     JOIN enterprise_invoice_links l USING(company_id,invoice_id) WHERE {where}
                     ORDER BY i.issue_date,i.invoice_id LIMIT ? OFFSET ?""",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        missing = [{"invoice_id": row["invoice_id"], "source_system": row["source_system"], "source_invoice_id": row["source_invoice_id"], "issue_date": row["issue_date"], "amount": row["amount_text"], "currency": row["currency"], "revision": row["revision"]} for row in rows]
        summary = "企业内部默认策略：空用途进入人工核实；这不是税务规则结论。" if rule_status == "internal_all_blank" else None
        return {"status": "ok", "company_id": company_id, "period": period, "rule_status": rule_status, "policy_summary": summary, "rule": rule, "missing_count": count, "missing": missing, "page": page, "page_size": page_size, "pages": (count + page_size - 1) // page_size, "work_item": task}

    def list_work_items(self, *, user: dict, company_id: str | None = None, period: str | None = None, status: str | None = None, page: int = 1, page_size: int = 50) -> dict:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        clauses = []
        params: list[Any] = []
        if user["is_admin"]:
            if company_id:
                clauses.append("w.company_id=?")
                params.append(company_id)
        elif company_id:
            self._role(user, company_id, {"accountant", "reviewer"})
            clauses.append("w.company_id=?")
            params.append(company_id)
        else:
            clauses.append("""w.company_id IN (SELECT m.company_id FROM enterprise_memberships m
                JOIN user_company_permissions p USING(user_id,company_id)
                WHERE m.user_id=? AND m.active=1 AND m.role IN ('accountant','reviewer') AND p.can_view=1)""")
            params.append(user["user_id"])
        if period:
            clauses.append("w.period=?")
            params.append(_period(period))
        if status:
            clauses.append("w.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        count = int(self.database.connection.execute(f"SELECT COUNT(*) FROM enterprise_work_items w{where}", params).fetchone()[0])
        rows = self.database.connection.execute(
            f"""SELECT w.*,c.name AS company_name,
                       COUNT(ti.invoice_id) AS invoice_count,
                       SUM(x.source_status='active' AND (ti.status='pending' OR ti.status='awaiting_info')) AS pending_count,
                       SUM(x.source_status='active' AND ti.status='awaiting_review') AS review_count,
                       SUM(x.source_status='active' AND ti.status='returned') AS returned_count,
                       SUM(x.source_status='active' AND (ti.status='approved' OR ti.status='waived')) AS resolved_count,
                       SUM(x.source_status='void') AS void_count
                FROM enterprise_work_items w JOIN companies c USING(company_id)
                LEFT JOIN enterprise_task_invoices ti USING(task_id)
                LEFT JOIN enterprise_invoice_extra x ON x.company_id=ti.company_id AND x.invoice_id=ti.invoice_id{where}
                GROUP BY w.task_id ORDER BY w.updated_at DESC,w.task_id LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"status": "ok", "page": page, "page_size": page_size, "total": count, "pages": (count + page_size - 1) // page_size, "items": [dict(row) for row in rows]}

    def work_item(self, *, user: dict, task_id: str, page: int = 1, page_size: int = 50, include_review_basis: bool = True) -> dict:
        task = self.database.connection.execute("SELECT w.*,c.name AS company_name FROM enterprise_work_items w JOIN companies c USING(company_id) WHERE w.task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("工作项不存在。")
        self._role(user, task["company_id"], {"accountant", "reviewer"})
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        total = int(self.database.connection.execute("SELECT COUNT(*) FROM enterprise_task_invoices WHERE task_id=?", (task_id,)).fetchone()[0])
        if task["status"] == "closed":
            invoices = self.database.connection.execute(
                """SELECT invoice_id,issue_date,seller_name,project_name,purpose,remarks,revision,currency,source_status,amount_text,
                          NULL AS unit_price_text,NULL AS quantity_text,review_status AS purpose_review_status,disposition,
                          source_system,source_invoice_id,review_status AS task_status
                   FROM enterprise_task_invoice_snapshots WHERE task_id=? ORDER BY issue_date,invoice_id LIMIT ? OFFSET ?""",
                (task_id, page_size, (page - 1) * page_size),
            ).fetchall()
        else:
            invoices = self.database.connection.execute(
                """SELECT i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.purpose,i.remarks,i.revision,
                          x.currency,x.source_status,x.amount_text,x.unit_price_text,x.quantity_text,x.purpose_review_status,x.disposition,
                          l.source_system,l.source_invoice_id,ti.status AS task_status
                   FROM enterprise_task_invoices ti JOIN invoices i USING(company_id,invoice_id)
                   JOIN enterprise_invoice_extra x USING(company_id,invoice_id) JOIN enterprise_invoice_links l USING(company_id,invoice_id)
                   WHERE ti.task_id=? ORDER BY i.issue_date,i.invoice_id LIMIT ? OFFSET ?""",
                (task_id, page_size, (page - 1) * page_size),
            ).fetchall()
        requests = self.database.connection.execute("SELECT * FROM enterprise_requests WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()
        drafts = self.database.connection.execute("SELECT * FROM enterprise_drafts WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()
        draft_views = []
        for row in drafts:
            draft = self._decode_draft(row)
            current = None
            if draft["invoice_id"] and draft["status"] not in {"confirmed", "rejected", "superseded"}:
                current = self.database.connection.execute("SELECT purpose,remarks,revision FROM invoices WHERE company_id=? AND invoice_id=?",
                    (task["company_id"], draft["invoice_id"])).fetchone()
            draft.update(evaluate_draft_fields(draft, dict(current) if current else None))
            identifiers = list(dict.fromkeys(draft["candidate_invoice_ids"] + ([draft["invoice_id"]] if draft["invoice_id"] else [])))
            if identifiers:
                marks = ",".join("?" for _ in identifiers)
                draft["invoice_refs"] = [dict(item) for item in self.database.connection.execute(
                    f"""SELECT l.invoice_id,l.source_system,l.source_invoice_id,i.seller_name,i.project_name
                        FROM enterprise_invoice_links l JOIN invoices i USING(company_id,invoice_id)
                        WHERE l.company_id=? AND l.invoice_id IN ({marks})""", (task["company_id"], *identifiers)
                ).fetchall()]
            else:
                draft["invoice_refs"] = []
            draft_views.append(draft)
        invoice_views = []
        for row in invoices:
            invoice = dict(row)
            if task["status"] != "closed" and include_review_basis:
                review_snapshot = _review_snapshot(self.database, user, task_id, invoice)
                invoice["review_token"] = _review_token(review_snapshot)
                invoice["review_basis"] = review_snapshot["review_basis"]
                receipt = self.database.connection.execute(
                    """SELECT r.result_json FROM enterprise_confirmation_receipts r JOIN enterprise_drafts d USING(draft_id)
                       WHERE d.task_id=? AND r.invoice_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1""",
                    (task_id, invoice["invoice_id"])).fetchone()
                if receipt:
                    result = json.loads(receipt["result_json"])
                    invoice["review_changes"] = {field: {"before": result.get("before", {}).get(field), "after": invoice[field]}
                        for field in ("purpose", "remarks") if result.get("before", {}).get(field) != invoice[field]}
            invoice_views.append(invoice)
        return {"status": "ok", "task": dict(task), "invoices": {"page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size, "items": invoice_views}, "requests": [dict(row) for row in requests], "drafts": draft_views}

    @staticmethod
    def _decode_draft(row) -> dict:
        value = dict(row)
        value["candidate_invoice_ids"] = json.loads(value.pop("candidate_invoice_ids_json"))
        value["field_context"] = json.loads(value.pop("field_context_json", "{}"))
        value["needs_clarification"] = bool(value["needs_clarification"])
        value["unavailable"] = bool(value.get("unavailable", 0))
        if value.get("superseded_by"):
            value["status"] = "superseded"
        elif value["unavailable"]:
            value["status"] = "unavailable"
        return value

    def create_request(self, *, user: dict, task_id: str, respondent_user_id: str, invoice_ids: list[str], message: str) -> dict:
        task = self.database.connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("工作项不存在。")
        role = self._role(user, task["company_id"], {"accountant"})
        if task["status"] == "closed":
            raise ValueError("已关闭工作项不能新建请求。")
        responder = membership(self.database, respondent_user_id, task["company_id"])
        if responder is None or responder["role"] != "requester":
            raise ValueError("经办人必须是该企业已授权的 requester 账号。")
        ids = list(dict.fromkeys(str(value) for value in invoice_ids if value))
        if not ids:
            raise ValueError("请求至少要包含一张票。")
        allowed = {row[0] for row in self.database.connection.execute("""SELECT ti.invoice_id FROM enterprise_task_invoices ti
            JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
            WHERE ti.task_id=? AND ti.status IN ('pending','awaiting_info','returned') AND x.source_status='active'""", (task_id,)).fetchall()}
        if set(ids) - allowed:
            raise ValueError("请求只能包含当前工作项中尚待办理的发票。")
        request_id = _id("req-")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._role(user, task["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"], modify=True)
            current_task = connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            current_allowed = {row[0] for row in connection.execute("""SELECT ti.invoice_id FROM enterprise_task_invoices ti
                JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE ti.task_id=? AND ti.status IN ('pending','awaiting_info','returned') AND x.source_status='active'""", (task_id,)).fetchall()}
            if current_task is None or current_task["status"] == "closed" or set(ids) - current_allowed:
                raise ValueError("工作项在提交期间变化，请重新读取待办后再建请求。")
            open_requests = connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE task_id=? AND status='open'", (task_id,)).fetchall()
            already_requested = set()
            for existing_request in open_requests:
                already_requested.update(json.loads(existing_request["target_invoice_ids_json"]))
            if set(ids) & already_requested:
                raise ValueError("所选票据已有未回复请求；请先接续该请求或取消它。")
            connection.execute(
                "INSERT INTO enterprise_requests(request_id,task_id,company_id,created_by,respondent_user_id,target_invoice_ids_json,message,status,created_at) VALUES(?,?,?,?,?,?,?,'open',?)",
                (request_id, task_id, task["company_id"], user["user_id"], respondent_user_id, _json(ids), message.strip(), utc_now()),
            )
            now = utc_now()
            connection.executemany("UPDATE enterprise_task_invoices SET status='awaiting_info',updated_at=? WHERE task_id=? AND invoice_id=?", ((now, task_id, invoice_id) for invoice_id in ids))
            connection.execute("UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=? WHERE task_id=?", (now, task_id))
            result = dict(connection.execute("SELECT * FROM enterprise_requests WHERE request_id=?", (request_id,)).fetchone())
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def cancel_request(self, *, user: dict, request_id: str) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            request = connection.execute("SELECT * FROM enterprise_requests WHERE request_id=?", (request_id,)).fetchone()
            if request is None:
                raise ValueError("补充请求不存在。")
            self._role(user, request["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=request["company_id"], modify=True)
            if request["created_by"] != user["user_id"] and not user["is_admin"]:
                raise PermissionError("只能取消自己创建的请求。")
            if request["status"] == "cancelled":
                connection.commit()
                return {"status": "cancelled", "already_cancelled": True}
            now = utc_now()
            connection.execute("UPDATE enterprise_requests SET status='cancelled',cancelled_at=? WHERE request_id=?", (now, request_id))
            for invoice_id in json.loads(request["target_invoice_ids_json"]):
                self._mark_field_decisions(connection, request["task_id"], invoice_id, ["purpose", "remarks"])
                other = connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE task_id=? AND request_id<>? AND status='open'", (request["task_id"], request_id)).fetchall()
                still_requested = any(invoice_id in json.loads(row["target_invoice_ids_json"]) for row in other)
                if not still_requested:
                    connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=? AND status='awaiting_info'", (now, request["task_id"], invoice_id))
            connection.commit()
            return {"status": "cancelled", "request_id": request_id, "cancelled_at": now}
        except Exception:
            connection.rollback()
            raise

    def list_requests(self, *, user: dict, company_id: str | None = None, page: int = 1, page_size: int = 50) -> dict:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        if user["is_admin"]:
            where, args = (" WHERE r.company_id=?", [company_id]) if company_id else ("", [])
        elif company_id:
            current_membership = membership(self.database, user["user_id"], company_id)
            if current_membership is None:
                raise PermissionError("当前账号没有该企业的有效授权。")
            if current_membership["role"] == "requester":
                where, args = (" WHERE r.company_id=? AND r.respondent_user_id=?", [company_id, user["user_id"]])
            else:
                self._role(user, company_id, {"accountant", "reviewer"})
                where, args = (" WHERE r.company_id=?", [company_id])
        else:
            allowed = [row[0] for row in self.database.connection.execute("""SELECT m.company_id FROM enterprise_memberships m
                JOIN user_company_permissions p USING(user_id,company_id)
                WHERE m.user_id=? AND m.active=1 AND m.role IN ('accountant','reviewer') AND p.can_view=1""", (user["user_id"],)).fetchall()]
            clauses = ["(r.respondent_user_id=? AND EXISTS (SELECT 1 FROM enterprise_memberships rm WHERE rm.user_id=r.respondent_user_id AND rm.company_id=r.company_id AND rm.role='requester' AND rm.active=1))"]
            args = [user["user_id"]]
            if allowed:
                marks = ",".join("?" for _ in allowed)
                clauses.append(f"r.company_id IN ({marks})")
                args.extend(allowed)
            where = " WHERE (" + " OR ".join(clauses) + ")"
        total = int(self.database.connection.execute(f"SELECT COUNT(*) FROM enterprise_requests r{where}", args).fetchone()[0])
        rows = self.database.connection.execute(
            f"""SELECT r.*,w.period,w.status AS task_status,c.name AS company_name,u.display_name AS respondent_name
                FROM enterprise_requests r JOIN enterprise_work_items w USING(task_id)
                JOIN companies c USING(company_id) JOIN enterprise_users u ON u.user_id=r.respondent_user_id
                {where} ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
            (*args, page_size, (page - 1) * page_size),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["target_invoice_ids"] = json.loads(value.pop("target_invoice_ids_json"))
            ids = value["target_invoice_ids"]
            if ids:
                marks = ",".join("?" for _ in ids)
                if value["task_status"] == "closed":
                    refs = self.database.connection.execute(
                        f"""SELECT source_system,source_invoice_id,seller_name,project_name FROM enterprise_task_invoice_snapshots
                            WHERE task_id=? AND invoice_id IN ({marks})""", (value["task_id"], *ids)
                    ).fetchall()
                else:
                    refs = self.database.connection.execute(
                        f"""SELECT l.source_system,l.source_invoice_id,i.seller_name,i.project_name
                            FROM enterprise_invoice_links l JOIN invoices i USING(company_id,invoice_id)
                            WHERE l.company_id=? AND l.invoice_id IN ({marks})""", (value["company_id"], *ids)
                    ).fetchall()
                value["target_invoice_labels"] = [f"{ref['source_system']} / {ref['source_invoice_id']} · {ref['seller_name']} · {ref['project_name']}" for ref in refs]
            else:
                value["target_invoice_labels"] = []
            result.append(value)
        return {"status": "ok", "page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size, "items": result}

    def request_detail(self, *, user: dict, request_id: str) -> dict:
        request = self.database.connection.execute(
            """SELECT r.*,c.name AS company_name,w.period FROM enterprise_requests r
               JOIN enterprise_work_items w USING(task_id) JOIN companies c USING(company_id) WHERE r.request_id=?""", (request_id,)
        ).fetchone()
        if request is None:
            raise ValueError("补充请求不存在。")
        is_assigned = request["respondent_user_id"] == user["user_id"]
        if is_assigned:
            current_membership = membership(self.database, user["user_id"], request["company_id"])
            if current_membership is None or current_membership["role"] != "requester":
                raise PermissionError("当前账号已不再拥有此请求的经办人授权。")
        else:
            self._role(user, request["company_id"], {"accountant", "reviewer"})
        value = dict(request)
        value["target_invoice_ids"] = json.loads(value.pop("target_invoice_ids_json"))
        invoices = []
        task = self.database.connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (request["task_id"],)).fetchone()
        for invoice_id in value["target_invoice_ids"]:
            if task and task["status"] == "closed":
                row = self.database.connection.execute("SELECT invoice_id,issue_date,seller_name,project_name,purpose,remarks,revision,currency,amount_text,source_system,source_invoice_id FROM enterprise_task_invoice_snapshots WHERE task_id=? AND invoice_id=?", (request["task_id"], invoice_id)).fetchone()
            else:
                row = self.database.connection.execute(
                    """SELECT i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.purpose,i.remarks,i.revision,
                              x.currency,x.amount_text,l.source_system,l.source_invoice_id
                       FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                       JOIN enterprise_invoice_links l USING(company_id,invoice_id) WHERE i.company_id=? AND i.invoice_id=?""",
                    (request["company_id"], invoice_id),
                ).fetchone()
            if row:
                invoices.append(dict(row))
        responses = self.database.connection.execute("SELECT * FROM enterprise_responses WHERE request_id=? ORDER BY created_at", (request_id,)).fetchall()
        drafts = self.database.connection.execute("SELECT * FROM enterprise_drafts WHERE request_id=? ORDER BY created_at", (request_id,)).fetchall()
        draft_views = []
        for row in drafts:
            draft = self._decode_draft(row)
            identifiers = list(dict.fromkeys(draft["candidate_invoice_ids"] + ([draft["invoice_id"]] if draft["invoice_id"] else [])))
            if identifiers:
                marks = ",".join("?" for _ in identifiers)
                if task and task["status"] == "closed":
                    ref_rows = self.database.connection.execute(
                        f"""SELECT invoice_id,source_system,source_invoice_id,seller_name,project_name
                            FROM enterprise_task_invoice_snapshots WHERE task_id=? AND invoice_id IN ({marks})""", (request["task_id"], *identifiers)
                    ).fetchall()
                else:
                    ref_rows = self.database.connection.execute(
                        f"""SELECT l.invoice_id,l.source_system,l.source_invoice_id,i.seller_name,i.project_name
                            FROM enterprise_invoice_links l JOIN invoices i USING(company_id,invoice_id)
                            WHERE l.company_id=? AND l.invoice_id IN ({marks})""", (request["company_id"], *identifiers)
                    ).fetchall()
                draft["invoice_refs"] = [dict(item) for item in ref_rows]
            else:
                draft["invoice_refs"] = []
            draft_views.append(draft)
        from .reply_attachments import list_reply_attachments
        response_views = []
        for row in responses:
            response = dict(row)
            attachments = list_reply_attachments(self.database, user=user, response_id=row["response_id"])
            response["attachments"] = [{key: item.get(key) for key in ("attachment_id", "filename", "media_type", "processing_status",
                                      "page_count", "last_error_code", "last_error_message", "result_revision")} for item in attachments]
            response_views.append(response)
        return {"request": value, "task_status": task["status"] if task else "unknown", "invoices": invoices, "responses": response_views, "drafts": draft_views}

    def submit_reply(self, *, user: dict, request_id: str, reply_text: str) -> dict:
        reply_text = reply_text.strip()
        if not reply_text:
            raise ValueError("回复内容不能为空。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            request = connection.execute("SELECT * FROM enterprise_requests WHERE request_id=?", (request_id,)).fetchone()
            if request is None or request["respondent_user_id"] != user["user_id"]:
                raise PermissionError("只能回复明确分配给当前账号的请求。")
            self._role(user, request["company_id"], {"requester"})
            if request["status"] == "cancelled":
                raise ValueError("该请求已取消。")
            task = connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (request["task_id"],)).fetchone()
            late = bool(task and task["status"] == "closed")
            response_id = _id("reply-")
            connection.execute(
                "INSERT INTO enterprise_responses(response_id,request_id,submitted_by,reply_text,late,created_at) VALUES(?,?,?,?,?,?)",
                (response_id, request_id, user["user_id"], reply_text, int(late), utc_now()),
            )
            connection.execute("UPDATE enterprise_requests SET status='replied' WHERE request_id=? AND status<>'cancelled'", (request_id,))
            if not late:
                for invoice_id in json.loads(request["target_invoice_ids_json"]):
                    connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=? AND status='awaiting_info'", (utc_now(), request["task_id"], invoice_id))
            connection.commit()
            return {"response_id": response_id, "request_id": request_id, "late": late, "status": "saved"}
        except Exception:
            connection.rollback()
            raise

    def create_conversation_draft(self, *, user: dict, task_id: str, invoice_id: str, purpose: str | None,
                                  remarks: str | None, source_turn_id: str, user_text: str,
                                  clear_fields: list[str] | None = None) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            task = connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("工作项不存在。")
            self._role(user, task["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"], modify=True)
            if task["status"] == "closed":
                raise ValueError("工作项已关闭，请先建立后续工作项。")
            _record_actor_message(connection, user_id=user["user_id"], company_id=task["company_id"], task_id=task_id,
                                  turn_id=source_turn_id, content=user_text)
            invoice = connection.execute("""SELECT i.revision,i.purpose,i.remarks,x.source_status FROM enterprise_task_invoices ti
                JOIN invoices i USING(company_id,invoice_id) JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE ti.task_id=? AND ti.invoice_id=?""", (task_id, invoice_id)).fetchone()
            if invoice is None:
                raise ValueError("这张发票不在当前工作项中。")
            if invoice["source_status"] == "void":
                raise ValueError("该发票已作废，不能创建用途草稿。")
            clear_fields = clear_fields or []
            if not isinstance(clear_fields, list) or len(set(clear_fields)) != len(clear_fields) or set(clear_fields) - {"purpose", "remarks"}:
                raise ValueError("只能明确清空用途或备注。")
            purpose = "" if "purpose" in clear_fields else (purpose.strip() or None if isinstance(purpose, str) else None)
            remarks = "" if "remarks" in clear_fields else (remarks.strip() or None if isinstance(remarks, str) else None)
            if purpose is None and remarks is None:
                raise ValueError("请提供实际用途或备注。")
            existing = connection.execute("""SELECT * FROM enterprise_drafts WHERE task_id=?
                AND status IN ('proposed','clarification','conflict') AND superseded_by IS NULL""", (task_id,)).fetchall()
            for draft in existing:
                if draft["invoice_id"] == invoice_id or invoice_id in json.loads(draft["candidate_invoice_ids_json"]):
                    connection.rollback()
                    return {"status": "existing_draft", "draft": self._decode_draft(draft),
                            "summary": "该票已有待处理草稿，请读取后按版本更正。"}
            draft_id = _id("draft-")
            fields = {field: {"base_value": invoice[field], "base_revision": int(invoice["revision"]),
                              "evidence_quote": user_text[:3000], "source_turn_id": source_turn_id,
                              "mode": "clear" if field in clear_fields else "draft", "history": []}
                      for field, proposed in (("purpose", purpose), ("remarks", remarks)) if proposed is not None}
            connection.execute("""INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,
                candidate_invoice_ids_json,proposed_purpose,proposed_remarks,evidence_quote,status,source_revision,field_context_json,origin,source_turn_id,created_by,created_at)
                VALUES(?,NULL,NULL,?,?,?,?,?,?,?,'proposed',?,?,'chat',?,?,?)""",
                (draft_id, task_id, task["company_id"], invoice_id, _json([invoice_id]), purpose, remarks,
                 user_text[:3000], int(invoice["revision"]), _json(fields), source_turn_id, user["user_id"], utc_now()))
            self._mark_field_decisions(connection, task_id, invoice_id, list(fields))
            result = self._decode_draft(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone())
            connection.commit()
            return {"status": "created", "draft": result, "invoice_written": False, "summary": "草稿已记录，发票尚未修改。"}
        except Exception:
            connection.rollback()
            raise

    def update_draft(self, *, user: dict, draft_id: str, invoice_id: str | None, purpose: str | None, remarks: str | None,
                     expected_draft_revision: int | None = None, source_text: str | None = None,
                     source_turn_id: str | None = None, rebase_fields: dict[str, int] | None = None,
                     clear_fields: list[str] | None = None) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            draft = connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if draft is None:
                raise ValueError("草稿不存在。")
            self._role(user, draft["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=draft["company_id"], modify=True)
            task = connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (draft["task_id"],)).fetchone()
            if task is None or task["status"] == "closed":
                raise ValueError("原工作项已关闭；迟到回复请明确创建后续工作项。")
            _record_actor_message(connection, user_id=user["user_id"], company_id=draft["company_id"], task_id=draft["task_id"],
                                  turn_id=source_turn_id, content=source_text)
            if draft["status"] in {"confirmed", "rejected"}:
                raise ValueError("已确认或已拒绝的草稿不能更改。")
            if type(expected_draft_revision) is not int or expected_draft_revision != draft["draft_revision"]:
                raise ValueError("草稿已被其他操作更新，请刷新后再修改。")
            chosen = invoice_id or draft["invoice_id"]
            if draft["request_id"]:
                request = connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE request_id=?", (draft["request_id"],)).fetchone()
                targets = set(json.loads(request["target_invoice_ids_json"]))
            else:
                targets = {row[0] for row in connection.execute("SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=?", (draft["task_id"],)).fetchall()}
            if chosen not in targets:
                raise ValueError("草稿只能绑定原请求中的发票。")
            clear_fields = clear_fields or []
            if not isinstance(clear_fields, list) or len(set(clear_fields)) != len(clear_fields) or set(clear_fields) - {"purpose", "remarks"}:
                raise ValueError("只能明确清空用途或备注。")
            previous_contexts = json.loads(draft["field_context_json"] or "{}")
            def proposal(field, value):
                if field in clear_fields:
                    return ""
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if value == "" and draft["proposed_" + field] == "" and previous_contexts.get(field, {}).get("mode") == "clear":
                    return ""
                return None
            purpose, remarks = proposal("purpose", purpose), proposal("remarks", remarks)
            if purpose is None and remarks is None:
                raise ValueError("至少要补充一项用途或备注；明确清空请单独选择清空字段。")
            current = connection.execute("""SELECT i.revision,i.purpose,i.remarks,x.source_status FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE i.company_id=? AND i.invoice_id=?""", (draft["company_id"], chosen)).fetchone()
            if current is None:
                raise ValueError("草稿关联的发票不存在。")
            if current["source_status"] == "void":
                raise ValueError("该发票已作废，不能更正用途草稿。")
            rebase_fields = rebase_fields or {}
            if not isinstance(rebase_fields, dict) or set(rebase_fields) - {"purpose", "remarks"}:
                raise ValueError("只能确认用途或备注的当前值。")
            if any(type(revision) is not int or revision != current["revision"] for revision in rebase_fields.values()):
                raise ValueError("票据内容又发生了变化，请刷新后重新核对。")
            fields = json.loads(draft["field_context_json"] or "{}")
            source_conflict = fields.get("_conflict_reason", {}).get("kind") == "source_status"
            required_rebase = {field for field, value in (("purpose", purpose), ("remarks", remarks)) if value is not None}
            if source_conflict and not required_rebase.issubset(rebase_fields):
                raise ValueError("票据来源状态已变化，请查看当前状态后确认需要保留的字段基线。")
            fields.pop("_conflict_reason", None)
            changed_target = chosen != draft["invoice_id"]
            decided_fields = []
            for field, proposed in (("purpose", purpose), ("remarks", remarks)):
                previous = draft["proposed_" + field]
                if changed_target or proposed != previous or field in rebase_fields:
                    decided_fields.append(field)
                if field in rebase_fields and proposed is None:
                    raise ValueError("未提出修改的字段不能确认覆盖。")
                if proposed is None:
                    fields.pop(field, None)
                elif changed_target or proposed != previous or field in rebase_fields:
                    old = fields.get(field, {})
                    history = list(old.get("history", []))
                    if old:
                        history.append({key: old.get(key) for key in ("evidence_quote", "source_turn_id", "proposed_value")})
                    note = source_text or (f"会计查看当前值后确认用草稿覆盖：{proposed}" if field in rebase_fields else f"会计在工作台更正{field}：{proposed}")
                    legacy_unknown = (not old and previous is not None and not changed_target and field not in rebase_fields
                                      and int(draft["source_revision"] or -1) != int(current["revision"]))
                    accept_current = changed_target or field in rebase_fields or (not old and not legacy_unknown)
                    base_value = current[field] if accept_current else old.get("base_value")
                    base_revision = int(current["revision"]) if accept_current else old.get("base_revision", draft["source_revision"])
                    fields[field] = {"base_value": base_value, "base_revision": base_revision,
                        "evidence_quote": note[:3000],
                        "source_turn_id": source_turn_id, "source_recorded": bool(source_text), "proposed_value": proposed,
                        "mode": "clear" if field in clear_fields else "draft", "history": history}
                    if changed_target and proposed == previous and old.get("evidence_source"):
                        fields[field]["evidence_source"] = old["evidence_source"]
                        fields[field]["evidence_quote"] = old.get("evidence_quote", "")
                        fields[field]["source_confirmation"] = {"user_id": user["user_id"], "turn_id": source_turn_id, "at": utc_now()}
                    if legacy_unknown or old.get("base_known") is False and not accept_current:
                        fields[field]["base_known"] = False
                    if field in rebase_fields:
                        fields[field]["base_acceptance"] = {"user_id": user["user_id"], "revision": int(current["revision"]), "at": utc_now()}
                elif field not in fields and int(draft["source_revision"] or -1) == int(current["revision"]):
                    fields[field] = {"base_value": current[field], "base_revision": int(current["revision"]),
                        "evidence_quote": draft["evidence_quote"], "source_turn_id": draft["source_turn_id"], "history": []}
            connection.execute(
                """UPDATE enterprise_drafts SET invoice_id=?,candidate_invoice_ids_json=?,proposed_purpose=?,proposed_remarks=?,
                   needs_clarification=0,unavailable=0,clarification_question='',status='proposed',source_revision=?,
                   field_context_json=?,draft_revision=draft_revision+1 WHERE draft_id=?""",
                (chosen, _json([chosen]), purpose, remarks,
                 int(current["revision"]) if changed_target else draft["source_revision"], _json(fields), draft_id),
            )
            connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=? AND status='awaiting_info'", (utc_now(), draft["task_id"], chosen))
            if changed_target:
                self._mark_field_decisions(connection, draft["task_id"], draft["invoice_id"], decided_fields)
            self._mark_field_decisions(connection, draft["task_id"], chosen, decided_fields)
            connection.commit()
            return dict(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone())
        except Exception:
            connection.rollback()
            raise

    def reject_draft(self, *, user: dict, draft_id: str) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            draft = connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if draft is None:
                raise ValueError("草稿不存在。")
            self._role(user, draft["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=draft["company_id"], modify=True)
            if draft["status"] in {"confirmed", "rejected"}:
                raise ValueError("已确认或已拒绝的草稿不能再次更改。")
            connection.execute("UPDATE enterprise_drafts SET status='rejected',draft_revision=draft_revision+1 WHERE draft_id=?", (draft_id,))
            fields = [field for field in ("purpose", "remarks") if draft["proposed_" + field] is not None]
            for invoice_id in set(json.loads(draft["candidate_invoice_ids_json"]) + ([draft["invoice_id"]] if draft["invoice_id"] else [])):
                self._mark_field_decisions(connection, draft["task_id"], invoice_id, fields or ["purpose", "remarks"])
            if draft["invoice_id"]:
                connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=? AND status='awaiting_info'", (utc_now(), draft["task_id"], draft["invoice_id"]))
            connection.commit()
            return {"status": "rejected", "draft_id": draft_id}
        except Exception:
            connection.rollback()
            raise

    def create_late_reply_followup(self, *, user: dict, task_id: str, draft_ids: list[str]) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            original = connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            if original is None or original["status"] != "closed":
                raise ValueError("仅已关闭工作项可以显式建立迟到回复的后续任务。")
            self._role(user, original["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=original["company_id"], modify=True)
            source_drafts = []
            for draft_id in dict.fromkeys(draft_ids):
                draft = connection.execute(
                    """SELECT d.*,r.late FROM enterprise_drafts d JOIN enterprise_responses r USING(response_id)
                       WHERE d.draft_id=? AND d.task_id=?""", (draft_id, task_id)
                ).fetchone()
                if draft is None or not draft["late"] or draft["status"] in {"confirmed", "rejected"}:
                    raise ValueError("后续任务只能引用原请求的迟到回复草稿；原草稿不会被移动或改写。")
                source_drafts.append(draft)
            if not source_drafts:
                raise ValueError("至少选择一条迟到回复草稿。")
            new_task_id = _id("work-")
            now = utc_now()
            connection.execute(
                "INSERT INTO enterprise_work_items(task_id,company_id,period,status,revision,created_by,created_at,updated_at) VALUES(?,?,?,'open',1,?,?,?)",
                (new_task_id, original["company_id"], original["period"], user["user_id"], now, now),
            )
            target_ids = set()
            for draft in source_drafts:
                ids = json.loads(draft["candidate_invoice_ids_json"])
                if draft["invoice_id"]:
                    ids.append(draft["invoice_id"])
                target_ids.update(ids)
            for invoice_id in sorted(target_ids):
                connection.execute("INSERT OR IGNORE INTO enterprise_task_invoices(task_id,company_id,invoice_id,status,updated_at) VALUES(?,?,?,'pending',?)", (new_task_id, original["company_id"], invoice_id, now))
            new_drafts = []
            for draft in source_drafts:
                draft_id = _id("draft-")
                connection.execute(
                    """INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,
                       proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,status,source_revision,field_context_json,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, draft["response_id"], draft["request_id"], new_task_id, original["company_id"], draft["invoice_id"], draft["candidate_invoice_ids_json"], draft["proposed_purpose"], draft["proposed_remarks"], draft["evidence_quote"], draft["needs_clarification"], draft["unavailable"], draft["clarification_question"], draft["status"], draft["source_revision"], draft["field_context_json"], user["user_id"], now),
                )
                new_drafts.append(draft_id)
            connection.commit()
            return {"status": "created", "task_id": new_task_id, "copied_draft_ids": new_drafts, "original_task_id": task_id}
        except Exception:
            connection.rollback()
            raise

    def save_drafts(self, *, user: dict, response_id: str, items: list[dict],
                    source_revisions: dict[str, int] | None = None,
                    source_values: dict[str, dict] | None = None,
                    decision_versions: dict[str, dict[str, int]] | None = None,
                    stale_fields: list[dict] | None = None,
                    extraction_basis: dict | None = None, run_guard: dict | None = None,
                    guard_result: dict | None = None, attachment_failures: list[dict] | None = None) -> list[dict]:
        response = self.database.connection.execute(
            """SELECT p.*,r.target_invoice_ids_json,r.company_id,r.task_id FROM enterprise_responses p
               JOIN enterprise_requests r USING(request_id) WHERE p.response_id=?""", (response_id,)
        ).fetchone()
        if response is None:
            raise ValueError("回复不存在。")
        self._role(user, response["company_id"], {"accountant"})
        allowed = set(json.loads(response["target_invoice_ids_json"]))
        if source_revisions is None:
            source_revisions = {
                row["invoice_id"]: int(row["revision"])
                for row in self.database.connection.execute(
                    f"SELECT invoice_id,revision FROM invoices WHERE company_id=? AND invoice_id IN ({','.join('?' for _ in allowed) or 'NULL'})",
                    (response["company_id"], *allowed),
                ).fetchall()
            }
        if source_values is None:
            source_values = {}
            for row in self.database.connection.execute(
                f"SELECT invoice_id,revision,purpose,remarks FROM invoices WHERE company_id=? AND invoice_id IN ({','.join('?' for _ in allowed) or 'NULL'})",
                (response["company_id"], *allowed)):
                if source_revisions.get(row["invoice_id"]) == row["revision"]:
                    source_values[row["invoice_id"]] = {"purpose": row["purpose"], "remarks": row["remarks"]}
        prepared = []
        for item in items:
            ids = item.get("candidate_invoice_ids") or ([item.get("invoice_id")] if item.get("invoice_id") else [])
            if not isinstance(ids, list) or any(str(value) not in allowed for value in ids):
                raise ValueError("模型返回了原请求范围之外的票据，草稿未保存。")
            candidates = list(dict.fromkeys(str(value) for value in ids))
            needs_clarification = bool(item.get("needs_clarification"))
            purpose = item.get("purpose")
            remarks = item.get("remarks")
            if purpose is not None:
                purpose = str(purpose).strip() or None
            if remarks is not None:
                remarks = str(remarks).strip() or None
            unavailable = bool(item.get("unavailable"))
            question = str(item.get("clarification_question") or "")[:1000]
            if purpose is None and remarks is None and not unavailable and not needs_clarification:
                needs_clarification = True
                question = question or "目前没有可确认的用途信息，请补充事实或明确说明无法提供。"
            ambiguous = needs_clarification or len(candidates) != 1
            invoice_id = None if ambiguous else candidates[0]
            revision = source_revisions.get(invoice_id) if invoice_id else None
            prepared.append((invoice_id, candidates, purpose, remarks, str(item.get("evidence_quote") or "")[:3000], needs_clarification, ambiguous, unavailable, question, revision, item.get("evidence_source")))
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._role(user, response["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=response["company_id"], modify=True)
            if run_guard:
                run = connection.execute("""SELECT * FROM enterprise_extraction_runs WHERE run_id=? AND response_id=?
                    AND task_id=? AND company_id=? AND created_by=?""", (run_guard["run_id"], response_id,
                    response["task_id"], response["company_id"], user["user_id"])).fetchone()
                if (run is None or run["state"] != "running" or run["cancel_requested"] or
                        run["generation"] != run_guard["generation"] or not run["lease_until"] or run["lease_until"] <= utc_now()):
                    if guard_result is not None:
                        guard_result["accepted"] = False
                    connection.rollback()
                    return []
                mandate_link = connection.execute("SELECT mandate_id FROM enterprise_mandate_extractions WHERE run_id=?", (run_guard["run_id"],)).fetchone()
                if mandate_link:
                    mandate = connection.execute("SELECT status,valid_until FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_link["mandate_id"],)).fetchone()
                    if mandate is None or mandate["status"] != "active" or mandate["valid_until"] <= utc_now():
                        if guard_result is not None:
                            guard_result["accepted"] = False
                        connection.rollback()
                        return []
            current_response = connection.execute("SELECT r.task_id,r.company_id,r.target_invoice_ids_json,r.status AS request_status,w.status FROM enterprise_requests r JOIN enterprise_work_items w USING(task_id) WHERE r.request_id=?", (response["request_id"],)).fetchone()
            if current_response is None:
                raise ValueError("原请求已不存在，草稿未保存。")
            current_allowed = set(json.loads(current_response["target_invoice_ids_json"]))
            if any(set(candidates) - current_allowed for _, candidates, *_ in prepared):
                raise ValueError("请求范围在生成期间变化，草稿未保存。")
            latest = connection.execute("SELECT response_id FROM enterprise_responses WHERE request_id=? ORDER BY rowid DESC LIMIT 1", (response["request_id"],)).fetchone()
            changed_during_run = extraction_basis is not None and (
                (latest and latest["response_id"] != extraction_basis["latest_response"]) or
                (current_response["request_status"] == "cancelled" and extraction_basis["request_status"] != "cancelled") or
                (current_response["status"] == "closed" and extraction_basis["task_status"] != "closed"))
            if changed_during_run:
                for invoice_id, candidates, purpose, remarks, quote, *_ in prepared:
                    fields = [field for field, value in (("purpose", purpose), ("remarks", remarks)) if value is not None]
                    for target_id in candidates or ([invoice_id] if invoice_id else []):
                        connection.execute("""INSERT INTO enterprise_stale_candidates
                            (candidate_id,task_id,response_id,invoice_id,fields_json,candidate_json,created_at)
                            VALUES(?,?,?,?,?,?,?)""", (_id("stale-"), response["task_id"], response_id, target_id,
                            _json(fields), _json({"purpose": purpose, "remarks": remarks, "evidence_quote": quote}), utc_now()))
                        if stale_fields is not None:
                            stale_fields.append({"invoice_id": target_id, "fields": fields})
                connection.execute("UPDATE enterprise_responses SET extraction_status='complete',extraction_outcome='stale',processed_at=? WHERE response_id=?", (utc_now(), response_id))
                if run_guard:
                    connection.execute("UPDATE enterprise_extraction_runs SET state='stale',result_json=?,updated_at=? WHERE run_id=? AND generation=?",
                        (_json({"draft_ids": [], "stale_fields": stale_fields or []}), utc_now(), run_guard["run_id"], run_guard["generation"]))
                connection.commit()
                return []
            response_sequence = connection.execute("SELECT rowid FROM enterprise_responses WHERE response_id=?", (response_id,)).fetchone()[0]
            saved = []
            for invoice_id, candidates, purpose, remarks, quote, needs_clarification, ambiguous, unavailable, question, revision, evidence_source in prepared:
                if invoice_id and decision_versions is not None:
                    basis = decision_versions.get(invoice_id, {})
                    current_versions = {row["field"]: int(row["decision_revision"]) for row in connection.execute(
                        "SELECT field,decision_revision FROM enterprise_field_decisions WHERE task_id=? AND invoice_id=?",
                        (response["task_id"], invoice_id))}
                    proposed = {"purpose": purpose, "remarks": remarks}
                    stale = [field for field, value in proposed.items() if value is not None and
                             current_versions.get(field, 0) != basis.get(field, 0)]
                    if isinstance(evidence_source, dict) and evidence_source.get("type") == "reply_attachment":
                        source = connection.execute("""SELECT sha256,source_revision,result_revision,processing_status
                            FROM enterprise_original_attachments WHERE attachment_id=? AND company_id=?""",
                            (evidence_source.get("attachment_id"), response["company_id"])).fetchone()
                        if (source is None or source["sha256"] != evidence_source.get("sha256") or
                                source["source_revision"] != evidence_source.get("source_revision") or
                                source["result_revision"] != evidence_source.get("result_revision") or
                                source["processing_status"] != evidence_source.get("processing_status")):
                            stale = list(dict.fromkeys(stale + [field for field, value in proposed.items() if value is not None]))
                    if stale:
                        connection.execute("""INSERT INTO enterprise_stale_candidates
                            (candidate_id,task_id,response_id,invoice_id,fields_json,candidate_json,created_at)
                            VALUES(?,?,?,?,?,?,?)""", (_id("stale-"), response["task_id"], response_id, invoice_id,
                            _json(stale), _json({field: proposed[field] for field in stale} | {"evidence_quote": quote}), utc_now()))
                        if stale_fields is not None:
                            stale_fields.append({"invoice_id": invoice_id, "fields": stale})
                        if "purpose" in stale:
                            purpose = None
                        if "remarks" in stale:
                            remarks = None
                        if purpose is None and remarks is None and not ambiguous and not unavailable:
                            continue
                if invoice_id and not ambiguous and not unavailable:
                    unchanged = connection.execute(
                        """SELECT * FROM enterprise_drafts WHERE task_id=? AND invoice_id=? AND status='proposed'
                           AND superseded_by IS NULL AND proposed_purpose IS ? AND proposed_remarks IS ? AND source_revision IS ?
                           ORDER BY created_at DESC LIMIT 1""",
                        (response["task_id"], invoice_id, purpose, remarks, revision),
                    ).fetchone()
                    if unchanged is not None:
                        current_turn = connection.execute("SELECT * FROM enterprise_drafts WHERE response_id=? AND invoice_id=? AND proposed_purpose IS ? AND proposed_remarks IS ? ORDER BY created_at DESC LIMIT 1", (response_id, invoice_id, purpose, remarks)).fetchone()
                        if current_turn is not None:
                            saved.append(dict(current_turn))
                        else:
                            duplicate_id = _id("draft-")
                            connection.execute(
                                """INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,
                                   proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,status,source_revision,created_by,created_at,superseded_by)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,0,0,'','rejected',?,?,?,?)""",
                                (duplicate_id, response_id, response["request_id"], response["task_id"], response["company_id"], invoice_id, _json(candidates), purpose, remarks, quote, revision, user["user_id"], utc_now(), unchanged["draft_id"]),
                            )
                            saved.append(dict(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (duplicate_id,)).fetchone()))
                        continue
                draft_id = _id("draft-")
                stale_context = False
                if invoice_id:
                    current = connection.execute("""SELECT i.revision,i.purpose,i.remarks,x.source_status FROM invoices i
                        JOIN enterprise_invoice_extra x USING(company_id,invoice_id) WHERE i.company_id=? AND i.invoice_id=?""",
                        (response["company_id"], invoice_id)).fetchone()
                    observed = source_values.get(invoice_id, {})
                    proposed = {"purpose": purpose, "remarks": remarks}
                    if current is None or revision is None:
                        stale_context = True
                    elif all(field in observed for field, value in proposed.items() if value is not None):
                        stale_context = (current["source_status"] != observed.get("source_status", current["source_status"]) or
                            any(current[field] != observed[field] for field, value in proposed.items() if value is not None))
                    else:
                        stale_context = int(current["revision"]) != int(revision)
                target_ids = set(candidates or ([invoice_id] if invoice_id else []))
                active_drafts = connection.execute(
                    """SELECT d.*,
                              p.created_at AS response_created_at,p.rowid AS response_sequence
                       FROM enterprise_drafts d LEFT JOIN enterprise_responses p USING(response_id)
                       WHERE d.task_id=? AND d.status IN ('proposed','clarification','conflict') AND d.superseded_by IS NULL""",
                    (response["task_id"],),
                ).fetchall()
                newer = None
                for prior in active_drafts:
                    prior_targets = set(json.loads(prior["candidate_invoice_ids_json"]))
                    if prior["invoice_id"]:
                        prior_targets.add(prior["invoice_id"])
                    prior_time = prior["response_created_at"] or (prior["created_at"] if prior["origin"] == "chat" else "")
                    is_newer = bool(prior_time and (prior_time > response["created_at"] or
                        (prior_time == response["created_at"] and prior["response_sequence"] and prior["response_sequence"] > response_sequence)))
                    if target_ids & prior_targets and is_newer:
                        newer = prior["draft_id"]
                        break
                if newer:
                    connection.execute("""INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,
                        candidate_invoice_ids_json,proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,
                        clarification_question,status,source_revision,created_by,created_at,superseded_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'rejected',?,?,?,?)""",
                        (draft_id, response_id, response["request_id"], response["task_id"], response["company_id"], invoice_id,
                         _json(candidates), purpose, remarks, quote, int(needs_clarification), int(unavailable), question,
                         revision, user["user_id"], utc_now(), newer))
                    saved.append(dict(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()))
                    continue
                for prior in active_drafts:
                    prior_targets = set(json.loads(prior["candidate_invoice_ids_json"]))
                    if prior["invoice_id"]:
                        prior_targets.add(prior["invoice_id"])
                    if target_ids & prior_targets:
                        if invoice_id and prior["invoice_id"] == invoice_id and not ambiguous and not unavailable:
                            incoming = {field for field, value in (("purpose", purpose), ("remarks", remarks)) if value is not None}
                            existing = {field for field in ("purpose", "remarks") if prior["proposed_" + field] is not None}
                            overlap = incoming & existing
                            if not overlap:
                                continue
                            remaining = existing - overlap
                            if remaining:
                                previous_fields = json.loads(prior["field_context_json"] or "{}")
                                for field in overlap:
                                    previous_fields.pop(field, None)
                                connection.execute("""UPDATE enterprise_drafts SET proposed_purpose=?,proposed_remarks=?,
                                    field_context_json=?,draft_revision=draft_revision+1 WHERE draft_id=?""",
                                    (prior["proposed_purpose"] if "purpose" in remaining else None,
                                     prior["proposed_remarks"] if "remarks" in remaining else None,
                                     _json(previous_fields), prior["draft_id"]))
                                continue
                        connection.execute("UPDATE enterprise_drafts SET status='rejected',superseded_by=?,draft_revision=draft_revision+1 WHERE draft_id=?", (draft_id, prior["draft_id"]))
                observed = source_values.get(invoice_id, {})
                fields = {field: {"base_value": observed.get(field), "base_revision": revision, "base_known": field in observed,
                                  "evidence_quote": quote, "source_turn_id": response_id,
                                  "evidence_source": evidence_source, "proposed_value": proposed, "history": []}
                          for field, proposed in (("purpose", purpose), ("remarks", remarks))
                          if proposed is not None}
                connection.execute(
                    """INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,
                       proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,status,source_revision,field_context_json,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, response_id, response["request_id"], response["task_id"], response["company_id"], invoice_id, _json(candidates), purpose, remarks, quote, int(needs_clarification), int(unavailable), question, "conflict" if stale_context else ("clarification" if ambiguous or unavailable else "proposed"), revision, _json(fields), user["user_id"], utc_now()),
                )
                if current_response["status"] != "closed" and (ambiguous or unavailable):
                    for target_id in candidates or current_allowed:
                        connection.execute("UPDATE enterprise_task_invoices SET status='awaiting_info',updated_at=? WHERE task_id=? AND invoice_id=? AND status NOT IN ('awaiting_review','returned')", (utc_now(), response["task_id"], target_id))
                elif current_response["status"] != "closed" and invoice_id:
                    connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=?", (utc_now(), response["task_id"], invoice_id))
                saved.append(dict(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()))
            if current_response["status"] != "closed" and prepared:
                connection.execute("UPDATE enterprise_work_items SET status='in_progress',revision=revision+1,updated_at=? WHERE task_id=?", (utc_now(), response["task_id"]))
            connection.execute(
                "UPDATE enterprise_responses SET extraction_status='complete',extraction_outcome=?,processed_at=? WHERE response_id=?",
                ("partially_applied" if stale_fields and saved else "stale" if stale_fields else "drafts_saved" if saved else "no_new_suggestions", utc_now(), response_id),
            )
            if run_guard:
                run_state = "partially_applied" if (stale_fields and saved) or attachment_failures else "stale" if stale_fields else "completed"
                connection.execute("UPDATE enterprise_extraction_runs SET state=?,result_json=?,updated_at=? WHERE run_id=? AND generation=?",
                    (run_state, _json({"draft_ids": [row["draft_id"] for row in saved], "stale_fields": stale_fields or [],
                                       "attachment_failures": attachment_failures or []}),
                     utc_now(), run_guard["run_id"], run_guard["generation"]))
            connection.commit()
            return [self._decode_draft(row) for row in saved]
        except Exception:
            connection.rollback()
            raise

    def confirm_drafts(self, *, user: dict, draft_ids: list[str], expected_draft_revisions: dict[str, int] | None = None,
                       expected_fields_by_draft: dict[str, dict[str, str | None]] | None = None,
                       fields_by_draft: dict[str, list[str]] | None = None,
                       conditions_by_draft: dict[str, dict] | None = None,
                       source_confirmations_by_draft: dict[str, dict] | None = None,
                       automatic_import: bool = False,
                       conflict_revisions_by_draft: dict[str, int] | None = None) -> dict:
        """Confirm each ticket in its own short transaction; report partial success explicitly."""
        if expected_draft_revisions is not None and not isinstance(expected_draft_revisions, dict):
            raise ValueError("草稿版本必须按草稿编号提供。")
        revisions = expected_draft_revisions or {}
        conditions = expected_fields_by_draft or {}
        fields = fields_by_draft or {}
        typed_conditions = conditions_by_draft or {}
        source_confirmations = source_confirmations_by_draft or {}
        conflict_revisions = conflict_revisions_by_draft or {}
        outcomes = [self._confirm_one(user=user, draft_id=draft_id, expected_draft_revision=revisions.get(draft_id),
                                      expected_fields=conditions.get(draft_id), selected_fields=fields.get(draft_id),
                                      condition=typed_conditions.get(draft_id),
                                      source_confirmations=source_confirmations.get(draft_id), automatic_import=automatic_import,
                                      expected_conflict_revision=conflict_revisions.get(draft_id)) for draft_id in dict.fromkeys(draft_ids)]
        succeeded = sum(item["status"] in {"committed", "duplicate", "no_change"} for item in outcomes)
        failed = len(outcomes) - succeeded
        return {"status": "ok" if failed == 0 else ("partial" if succeeded else "failed"), "confirmed": succeeded, "failed": failed, "items": outcomes}

    def _confirm_one(self, *, user: dict, draft_id: str, expected_draft_revision: int | None = None,
                     expected_fields: dict[str, str | None] | None = None,
                     selected_fields: list[str] | None = None, condition: dict | None = None,
                     source_confirmations: dict | None = None, automatic_import: bool = False,
                     expected_conflict_revision: int | None = None) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            draft = connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if draft is None:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_found"}
            self._role(user, draft["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=draft["company_id"], modify=True)
            if type(expected_draft_revision) is not int:
                connection.rollback()
                return {"draft_id": draft_id, "status": "missing_draft_revision", "summary": "请先查看草稿，再确认对应版本。", "invoice_unchanged": True}
            if draft["origin"] == "import" and draft["superseded_by"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "source_changed", "invoice_unchanged": True,
                        "summary": "来源已有更新的草稿，请查看最新内容。"}
            if selected_fields is not None and (not isinstance(selected_fields, list) or not selected_fields or
                    len(set(selected_fields)) != len(selected_fields) or set(selected_fields) - {"purpose", "remarks"}):
                connection.rollback()
                return {"draft_id": draft_id, "status": "invalid_input", "summary": "请选择用途或备注字段。", "invoice_unchanged": True}
            if condition is not None and not _condition_valid(condition):
                connection.rollback()
                return {"draft_id": draft_id, "status": "unsupported_precondition", "summary": "无法准确处理该条件。", "invoice_unchanged": True}
            partial_key = None
            if selected_fields is not None and draft["invoice_id"]:
                decision_hash = hashlib.sha256(_json({"draft_id": draft_id, "revision": expected_draft_revision,
                                                      "fields": sorted(selected_fields), "condition": condition,
                                                      "expected_fields": expected_fields}).encode()).hexdigest()[:24]
                partial_key = f"{user['user_id']}:{draft['company_id']}:invoice:{draft['invoice_id']}:partial-{decision_hash}"
                prior_partial = connection.execute("""SELECT result_json FROM operations WHERE user_id=? AND company_id=?
                    AND object_type='invoice' AND object_id=? AND idempotency_key=?""",
                    (user["user_id"], draft["company_id"], draft["invoice_id"], partial_key)).fetchone()
                if prior_partial:
                    original = json.loads(prior_partial["result_json"])
                    current = connection.execute("""SELECT i.*,x.source_status FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                        WHERE i.company_id=? AND i.invoice_id=?""", (draft["company_id"], draft["invoice_id"])).fetchone()
                    current = dict(current) if current else None
                    matches = bool(current and current["source_status"] != "void" and all(
                        current[field] == original.get("after", {}).get(field) for field in original.get("confirmed_fields", [])))
                    connection.rollback()
                    return {"draft_id": draft_id, "status": "duplicate" if matches else "duplicate_changed",
                            "result": original, "current": current, "current_matches_receipt": matches,
                            "summary": "该字段此前已确认，当前内容已变化，请查看最新状态。" if not matches else "该字段此前已确认。"}
            prior = connection.execute("SELECT * FROM enterprise_confirmation_receipts WHERE draft_id=?", (draft_id,)).fetchone()
            if prior is not None:
                original = json.loads(prior["result_json"])
                confirmed_revision = original.get("confirmed_draft_revision", draft["draft_revision"] - 1)
                if expected_draft_revision != confirmed_revision:
                    connection.rollback()
                    return {"draft_id": draft_id, "status": "draft_conflict", "summary": "确认版本与原回执不一致。", "invoice_unchanged": True}
                current = connection.execute("""SELECT i.*,x.source_status FROM invoices i
                    JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                    WHERE i.company_id=? AND i.invoice_id=?""", (draft["company_id"], draft["invoice_id"])).fetchone()
                current = dict(current) if current else None
                fields = [field for field in ("purpose", "remarks") if draft["proposed_" + field] is not None]
                matches = current is not None and current["source_status"] != "void" and all(current.get(field) == original.get("after", {}).get(field) for field in fields)
                connection.rollback()
                return {"draft_id": draft_id, "status": "duplicate" if matches else "duplicate_changed",
                        "result": original, "current": current, "current_matches_receipt": matches,
                        "summary": "原操作已执行；当前内容已变化，请核对。" if not matches else "该操作此前已执行。"}
            source = connection.execute("SELECT source_status FROM enterprise_invoice_extra WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            if source and source["source_status"] == "void":
                connection.rollback()
                return {"draft_id": draft_id, "status": "void_invoice", "summary": "该发票已作废，不能确认写入。", "invoice_unchanged": True}
            if expected_draft_revision != draft["draft_revision"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "draft_conflict", "summary": "草稿已更新，请刷新后确认最新内容。", "invoice_unchanged": True}
            if draft["unavailable"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "unavailable", "summary": "经办人明确无法提供信息；草稿保持待处理，须由复核人按企业授权作出处置。"}
            if not draft["invoice_id"] or draft["needs_clarification"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "needs_clarification", "summary": "歧义草稿须先取得澄清，不能确认。"}
            task = connection.execute("SELECT status,period FROM enterprise_work_items WHERE task_id=?", (draft["task_id"],)).fetchone()
            if task is None or task["status"] == "closed":
                connection.rollback()
                return {"draft_id": draft_id, "status": "task_closed", "invoice_unchanged": True}
            current = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            if current is None:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_found"}
            import_conflict = connection.execute("""SELECT c.* FROM enterprise_import_drafts d
                JOIN enterprise_import_conflicts c ON c.conflict_id=d.conflict_id WHERE d.draft_id=?""", (draft_id,)).fetchone()
            if import_conflict and (import_conflict["status"] != "open" or
                    (expected_conflict_revision is not None and current["revision"] != expected_conflict_revision)):
                connection.rollback()
                return {"draft_id": draft_id, "status": "revision_conflict", "invoice_unchanged": True,
                        "summary": "来源冲突或票据内容已变化，请刷新后重新处理。"}
            available_fields = [field for field in ("purpose", "remarks") if draft["proposed_" + field] is not None]
            selected = selected_fields if selected_fields is not None else available_fields
            if not selected or set(selected) - set(available_fields):
                connection.rollback()
                return {"draft_id": draft_id, "status": "draft_conflict", "summary": "所选字段不再属于当前草稿。", "invoice_unchanged": True}
            partial = set(selected) != set(available_fields)
            if condition is not None and not _condition_met(condition, dict(current)):
                connection.rollback()
                return {"draft_id": draft_id, "status": "precondition_not_met",
                        "summary": "当前内容不满足你提出的保存条件，发票没有修改。", "invoice_unchanged": True}
            if expected_fields:
                differences = {field: {"expected": value, "current": current[field]} for field, value in expected_fields.items()
                               if _clean_text(current[field]) != _clean_text(value)}
                if differences:
                    connection.rollback()
                    return {"draft_id": draft_id, "status": "precondition_not_met", "fields": differences,
                            "summary": "当前内容不满足你提出的保存条件，发票没有修改。", "invoice_unchanged": True}
            conflict_reason = json.loads(draft["field_context_json"] or "{}").get("_conflict_reason", {})
            if draft["status"] == "conflict" and conflict_reason.get("kind") != "field_conflict":
                connection.rollback()
                return {"draft_id": draft_id, "status": "revision_conflict", "current_revision": int(current["revision"]),
                        "draft_revision": draft["source_revision"], "current": dict(current),
                        "summary": "来源内容曾发生变化，请查看当前票据后重新处理草稿。", "invoice_unchanged": True}
            selected_draft = self._decode_draft(draft)
            for field in set(available_fields) - set(selected):
                selected_draft["proposed_" + field] = None
            conflicts = evaluate_draft_fields(selected_draft, dict(current))["field_conflicts"]
            if conflicts:
                contexts = json.loads(draft["field_context_json"] or "{}")
                contexts["_conflict_reason"] = {"kind": "field_conflict", "fields": sorted(conflicts)}
                connection.execute("UPDATE enterprise_drafts SET status='conflict',field_context_json=? WHERE draft_id=?", (_json(contexts), draft_id))
                connection.commit()
                return {"draft_id": draft_id, "status": "revision_conflict", "current_revision": int(current["revision"]),
                        "draft_revision": draft["source_revision"], "field_conflicts": conflicts, "invoice_unchanged": True}
            if draft["status"] not in {"proposed", "conflict"}:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_confirmable"}
            proposal_contexts = json.loads(draft["field_context_json"] or "{}")
            if automatic_import:
                automatic = connection.execute("""SELECT p.mode,p.revision,q.auto_mode,q.policy_revision
                    FROM enterprise_import_drafts q JOIN enterprise_imports i USING(import_id)
                    LEFT JOIN enterprise_import_source_policies p ON p.company_id=i.company_id AND p.source_system=i.source_system
                    WHERE q.draft_id=?""", (draft_id,)).fetchone()
                if (not automatic or automatic["auto_mode"] != "trusted_sync" or automatic["mode"] != "trusted_sync"
                        or automatic["revision"] != automatic["policy_revision"]):
                    connection.rollback()
                    return {"draft_id": draft_id, "status": "source_policy_changed", "invoice_unchanged": True,
                            "summary": "来源同步配置已变化，请由会计确认。"}
            for field in selected:
                if not draft["proposed_" + field].strip() and proposal_contexts.get(field, {}).get("mode") != "clear":
                    connection.rollback()
                    return {"draft_id": draft_id, "status": "invalid_value", "summary": "空白内容需要单独明确清空。"}
                evidence = proposal_contexts.get(field, {}).get("evidence_source") or {}
                if evidence.get("type") == "source_document":
                    alias = connection.execute("""SELECT a.*,d.file_hash,d.file_content,d.original_available
                        FROM enterprise_source_aliases a JOIN enterprise_source_documents d USING(document_id)
                        WHERE a.alias_id=? AND a.company_id=? AND a.invoice_id=? AND a.status='linked'""",
                        (evidence.get("alias_id"), draft["company_id"], draft["invoice_id"])).fetchone()
                    latest = connection.execute("""SELECT content_hash FROM enterprise_source_aliases
                        WHERE company_id=? AND source_system=? AND source_invoice_id=? AND status='linked'
                        ORDER BY rowid DESC LIMIT 1""",
                        (draft["company_id"], evidence.get("source_system"), evidence.get("source_invoice_id"))).fetchone()
                    valid = (alias is not None and alias["document_id"] == evidence.get("document_id")
                             and alias["file_hash"] == evidence.get("file_hash") and alias["original_available"]
                             and alias["file_content"] is not None and alias["content_hash"] == evidence.get("content_hash")
                             and latest is not None and latest["content_hash"] == evidence.get("content_hash")
                             and json.loads(alias["source_json"]).get(field) == draft["proposed_" + field]
                             and hashlib.sha256(bytes(alias["file_content"])).hexdigest() == alias["file_hash"])
                    if not valid:
                        connection.rollback()
                        return {"draft_id": draft_id, "status": "source_changed", "invoice_unchanged": True,
                                "summary": "来源票据已变化，请查看最新导入内容后再确认。"}
            policy_decisions = {field: self.autonomy_policy.evaluate(user=user, company_id=draft["company_id"],
                period=task["period"], invoice_id=draft["invoice_id"], field=field, action="confirm", task_id=draft["task_id"])
                for field in selected}
            blocked = [decision for decision in policy_decisions.values() if not decision["action_allowed"]]
            if blocked:
                connection.rollback()
                return {"draft_id": draft_id, "status": "policy_blocked", "summary": blocked[0]["reason"],
                        "policy_decisions": policy_decisions, "invoice_unchanged": True}
            for field, evidence in (source_confirmations or {}).items():
                if field not in selected or not isinstance(evidence, dict) or not evidence.get("confirmed_turn_id") or not evidence.get("prompt"):
                    raise ValueError("确认依据与所选字段不一致。")
                _record_actor_message(connection, user_id=user["user_id"], company_id=draft["company_id"], task_id=draft["task_id"],
                                      turn_id=evidence["confirmed_turn_id"], content=evidence.get("confirmation"))
                proposal_contexts.setdefault(field, {})["source_confirmation"] = evidence
            before = dict(current)
            purpose = _clean_text(draft["proposed_purpose"]) if "purpose" in selected else current["purpose"]
            remarks = _clean_text(draft["proposed_remarks"]) if "remarks" in selected else current["remarks"]
            changed = purpose != current["purpose"] or remarks != current["remarks"]
            if changed:
                connection.execute(
                    "UPDATE invoices SET purpose=?,remarks=?,revision=revision+1 WHERE company_id=? AND invoice_id=? AND revision=?",
                    (purpose, remarks, draft["company_id"], draft["invoice_id"], current["revision"]),
                )
            after = dict(connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone())
            link = connection.execute("SELECT field_owners_json FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            owners = json.loads(link["field_owners_json"] or "{}")
            if "purpose" in selected:
                owners["purpose"] = "source_trusted" if automatic_import else "local_confirmed"
            if "remarks" in selected:
                owners["remarks"] = "source_trusted" if automatic_import else "local_confirmed"
            connection.execute("UPDATE enterprise_invoice_links SET field_owners_json=?,updated_at=? WHERE company_id=? AND invoice_id=?", (_json(owners), utc_now(), draft["company_id"], draft["invoice_id"]))
            connection.execute("UPDATE enterprise_invoice_extra SET purpose_review_status='pending',disposition=NULL,updated_at=? WHERE company_id=? AND invoice_id=?", (utc_now(), draft["company_id"], draft["invoice_id"]))
            task_status = "in_progress" if partial or not _clean_text(purpose) else "needs_review"
            connection.execute("UPDATE enterprise_task_invoices SET status=?,disposition=NULL,updated_at=? WHERE task_id=? AND invoice_id=?", ("pending" if task_status == "in_progress" else "awaiting_review", utc_now(), draft["task_id"], draft["invoice_id"]))
            connection.execute("UPDATE enterprise_work_items SET status=?,revision=revision+1,updated_at=? WHERE task_id=? AND status<>'closed'", (task_status, utc_now(), draft["task_id"]))
            operation_id = partial_key if partial else f"{user['user_id']}:{draft['company_id']}:invoice:{draft['invoice_id']}:{draft_id}"
            result = {"status": "committed" if changed else "no_change", "draft_id": draft_id, "invoice_id": draft["invoice_id"], "before": before, "after": after, "confirmed_by": user["user_id"], "confirmation_mode": "trusted_source_sync" if automatic_import else "accountant", "confirmed_draft_revision": draft["draft_revision"], "confirmed_fields": selected, "task_status": task_status}
            if any(decision["policy_version"] for decision in policy_decisions.values()):
                result["policy_decisions"] = policy_decisions
            payload = {"changes": {field: draft["proposed_" + field] for field in selected}, "draft_id": draft_id}
            confirmed_draft_id = draft_id
            if partial:
                contexts = proposal_contexts
                confirmed_draft_id = _id("draft-")
                connection.execute("""INSERT INTO enterprise_drafts
                    (draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,
                     proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,
                     status,source_revision,field_context_json,draft_revision,origin,source_turn_id,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,0,0,'','confirmed',?,?,?,?,?,?,?)""",
                    (confirmed_draft_id, draft["response_id"], draft["request_id"], draft["task_id"], draft["company_id"], draft["invoice_id"],
                     draft["candidate_invoice_ids_json"], draft["proposed_purpose"] if "purpose" in selected else None,
                     draft["proposed_remarks"] if "remarks" in selected else None, draft["evidence_quote"], draft["source_revision"],
                     _json({field: contexts[field] for field in selected if field in contexts}), draft["draft_revision"],
                     draft["origin"], draft["source_turn_id"], user["user_id"], utc_now()))
                for field in selected:
                    contexts.pop(field, None)
                connection.execute("""UPDATE enterprise_drafts SET proposed_purpose=?,proposed_remarks=?,field_context_json=?,
                    draft_revision=draft_revision+1,status='proposed' WHERE draft_id=?""",
                    (None if "purpose" in selected else draft["proposed_purpose"],
                     None if "remarks" in selected else draft["proposed_remarks"], _json(contexts), draft_id))
                result.update(confirmed_draft_id=confirmed_draft_id, remaining_draft_id=draft_id,
                              remaining_draft_revision=int(draft["draft_revision"]) + 1)
            else:
                connection.execute("UPDATE enterprise_drafts SET status='confirmed',field_context_json=?,draft_revision=draft_revision+1 WHERE draft_id=?", (_json(proposal_contexts), draft_id))
            payload_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
            TaxService._record_operation(connection, user["user_id"], draft["company_id"], draft["invoice_id"], operation_id, payload_hash, result["status"], result, "enterprise_confirm_draft", payload)
            connection.execute("INSERT INTO enterprise_confirmation_receipts(draft_id,company_id,invoice_id,operation_id,confirmed_by,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (confirmed_draft_id, draft["company_id"], draft["invoice_id"], operation_id, user["user_id"], _json(result), utc_now()))
            if import_conflict:
                connection.execute("""UPDATE enterprise_import_conflicts SET status='resolved',resolved_by=?,resolution='accept_source',
                    resolved_at=? WHERE conflict_id=? AND status='open'""", (user["user_id"], utc_now(), import_conflict["conflict_id"]))
            self._mark_field_decisions(connection, draft["task_id"], draft["invoice_id"], selected)
            connection.commit()
            return {"draft_id": draft_id, **result}
        except PermissionError as error:
            connection.rollback()
            return {"draft_id": draft_id, "status": "permission_denied", "summary": str(error), "invoice_unchanged": True}
        except Exception:
            connection.rollback()
            raise

    def review_invoice(self, *, user: dict, task_id: str, invoice_id: str, action: str,
                       review_token: str, comment: str = "") -> dict:
        if action not in {"approve", "return", "waive"}:
            raise ValueError("action 必须是 approve、return 或 waive。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            task = connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("工作项不存在。")
            self._role(user, task["company_id"], {"reviewer"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"])
            link = connection.execute("SELECT * FROM enterprise_task_invoices WHERE task_id=? AND invoice_id=?", (task_id, invoice_id)).fetchone()
            if link is None:
                raise ValueError("发票不属于当前工作项。")
            current = connection.execute("""SELECT i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.purpose,i.remarks,i.revision,
                x.amount_text,x.currency,x.source_status,ti.status AS task_status FROM invoices i
                JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                JOIN enterprise_task_invoices ti USING(company_id,invoice_id)
                WHERE ti.task_id=? AND i.invoice_id=?""", (task_id, invoice_id)).fetchone()
            if current is None:
                raise ValueError("发票不存在。")
            snapshot = _review_snapshot(self.database, user, task_id, dict(current))
            if not isinstance(review_token, str) or not review_token:
                raise ValueError("请先查看当前票据信息，再进行复核。")
            if review_token != _review_token(snapshot):
                connection.rollback()
                return {"status": "review_conflict", "summary": "票据内容或状态已变化，请查看最新信息后重新复核。", "current": snapshot}
            source = connection.execute("SELECT source_status FROM enterprise_invoice_extra WHERE company_id=? AND invoice_id=?", (task["company_id"], invoice_id)).fetchone()
            if source and source["source_status"] == "void":
                raise ValueError("该发票已作废，不需要按有效票据复核。")
            setting = connection.execute("SELECT * FROM enterprise_company_settings WHERE company_id=?", (task["company_id"],)).fetchone()
            if action in {"approve", "waive"} and any(
                    policy["decision"].get("requires_independent_review") and policy["confirmed_by"] == user["user_id"]
                    for policy in snapshot["review_basis"].get("autonomy_policy", {}).values()):
                raise PermissionError("当前办理策略要求由不同于字段提交人的复核人审批。")
            if action == "approve":
                if current is None or not str(current["purpose"] or "").strip():
                    raise ValueError("用途为空，不能批准；如需保留空值，须配置并明确批准允许的处置。")
                pending_import = connection.execute("""SELECT 1 FROM enterprise_import_drafts q
                    JOIN enterprise_drafts d USING(draft_id) WHERE d.task_id=? AND d.invoice_id=?
                    AND d.status IN ('proposed','clarification','conflict') AND d.superseded_by IS NULL LIMIT 1""",
                    (task_id, invoice_id)).fetchone()
                open_conflict = connection.execute("""SELECT 1 FROM enterprise_import_conflicts
                    WHERE company_id=? AND invoice_id=? AND status='open' LIMIT 1""", (task["company_id"], invoice_id)).fetchone()
                if pending_import or open_conflict:
                    connection.rollback()
                    return {"status": "review_not_ready", "summary": "导入内容或来源冲突尚待会计处理，请处理后再复核。"}
                professional = snapshot["review_basis"]["professional"]
                if professional["status"] != "supported":
                    connection.rollback()
                    return {"status": "review_not_ready", "summary": "用途依据或身份尚需处理，请先查看复核要求。",
                            "professional": professional}
                if link["status"] in {"awaiting_review", "pending", "returned"}:
                    submitter_id = _review_submitter(connection, task=task, invoice_id=invoice_id)
                else:
                    raise ValueError("只有待复核或来源更新后待重新核实的票据可以批准。")
                if setting["require_independent_review"] and submitter_id == user["user_id"]:
                    raise PermissionError("启用独立复核时，提交人不能复核自己的修改。")
                new_status, disposition = "approved", None
            elif action == "return":
                if link["status"] != "awaiting_review":
                    raise ValueError("只有待复核票据可以退回。")
                new_status, disposition = "returned", None
            else:
                allowed = json.loads(setting["allowed_dispositions_json"])
                if "waive_missing" not in allowed or link["status"] not in {"pending", "awaiting_info", "returned"}:
                    raise ValueError("该企业未授权 waive_missing 处置，或票据状态不允许处置。")
                if current and str(current["purpose"] or "").strip():
                    raise ValueError("用途已有值，不能按缺用途豁免处置。")
                if not isinstance(comment, str) or not comment.strip():
                    raise ValueError("请说明本次豁免的原因。")
                if setting["require_independent_review"] and _review_submitter(connection, task=task, invoice_id=invoice_id) == user["user_id"]:
                    raise PermissionError("启用独立复核时，提交人不能复核自己的处置。")
                new_status, disposition = "waived", "waive_missing"
            review_id = _id("review-")
            latest_draft = connection.execute("SELECT draft_id FROM enterprise_drafts WHERE task_id=? AND invoice_id=? AND status='confirmed' ORDER BY created_at DESC LIMIT 1", (task_id, invoice_id)).fetchone()
            connection.execute("""INSERT INTO enterprise_reviews(review_id,task_id,company_id,invoice_id,draft_id,reviewer_user_id,
                action,comment,review_token,reviewed_snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (review_id, task_id, task["company_id"], invoice_id, latest_draft["draft_id"] if latest_draft else None,
                 user["user_id"], action, comment.strip(), review_token, _json(snapshot), utc_now()))
            connection.execute("UPDATE enterprise_task_invoices SET status=?,disposition=?,updated_at=? WHERE task_id=? AND invoice_id=?", (new_status, disposition, utc_now(), task_id, invoice_id))
            connection.execute("UPDATE enterprise_invoice_extra SET purpose_review_status=?,disposition=?,updated_at=? WHERE company_id=? AND invoice_id=?", (new_status, disposition, utc_now(), task["company_id"], invoice_id))
            if action == "waive":
                unavailable_drafts = connection.execute("SELECT draft_id,invoice_id,candidate_invoice_ids_json FROM enterprise_drafts WHERE task_id=? AND unavailable=1 AND status='clarification'", (task_id,)).fetchall()
                for draft in unavailable_drafts:
                    candidates = json.loads(draft["candidate_invoice_ids_json"])
                    if draft["invoice_id"] == invoice_id or invoice_id in candidates:
                        candidates = [value for value in candidates if value != invoice_id]
                        if not candidates:
                            connection.execute("UPDATE enterprise_drafts SET candidate_invoice_ids_json='[]',status='rejected' WHERE draft_id=?", (draft["draft_id"],))
                        elif draft["invoice_id"] is None:
                            connection.execute("UPDATE enterprise_drafts SET candidate_invoice_ids_json=? WHERE draft_id=?", (_json(candidates), draft["draft_id"]))
            status = "returned" if new_status == "returned" else "needs_review"
            connection.execute("UPDATE enterprise_work_items SET status=?,revision=revision+1,updated_at=? WHERE task_id=? AND status<>'closed'", (status, utc_now(), task_id))
            connection.commit()
            return {"status": "ok", "review_id": review_id, "invoice_status": new_status, "task_status": status}
        except Exception:
            connection.rollback()
            raise

    def close_work_item(self, *, user: dict, task_id: str) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            task = connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("工作项不存在。")
            self._role(user, task["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"], modify=True)
            if task["status"] == "closed":
                connection.commit()
                return {"status": "closed", "already_closed": True}
            unresolved = connection.execute("""SELECT COUNT(*) FROM enterprise_task_invoices ti
                JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE ti.task_id=? AND x.source_status='active' AND ti.status NOT IN ('approved','waived')""", (task_id,)).fetchone()[0]
            open_requests = connection.execute("SELECT COUNT(*) FROM enterprise_requests WHERE task_id=? AND status='open'", (task_id,)).fetchone()[0]
            unprocessed = connection.execute(
                """SELECT COUNT(*) FROM enterprise_responses p JOIN enterprise_requests r USING(request_id)
                   WHERE r.task_id=? AND p.extraction_status<>'complete'""", (task_id,)
            ).fetchone()[0]
            pending_drafts = connection.execute("SELECT COUNT(*) FROM enterprise_drafts WHERE task_id=? AND status IN ('proposed','clarification','conflict')", (task_id,)).fetchone()[0]
            if unresolved or open_requests or unprocessed or pending_drafts:
                raise ValueError(f"工作项仍有待处理内容：未批准票据 {unresolved} 张，未关闭请求 {open_requests} 项，未处理回复 {unprocessed} 条，未处置草稿 {pending_drafts} 条。")
            now = utc_now()
            connection.execute(
                """INSERT OR REPLACE INTO enterprise_task_invoice_snapshots
                   (task_id,company_id,invoice_id,source_system,source_invoice_id,issue_date,seller_name,project_name,
                    amount_text,currency,source_status,purpose,remarks,revision,review_status,disposition,captured_at)
                   SELECT ti.task_id,ti.company_id,ti.invoice_id,l.source_system,l.source_invoice_id,i.issue_date,i.seller_name,
                          i.project_name,x.amount_text,x.currency,x.source_status,i.purpose,i.remarks,i.revision,
                          CASE WHEN x.source_status='void' THEN 'void' ELSE ti.status END,ti.disposition,?
                   FROM enterprise_task_invoices ti JOIN invoices i USING(company_id,invoice_id)
                   JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                   JOIN enterprise_invoice_links l USING(company_id,invoice_id) WHERE ti.task_id=?""",
                (now, task_id),
            )
            connection.execute("UPDATE enterprise_work_items SET status='closed',revision=revision+1,updated_at=?,closed_at=? WHERE task_id=?", (now, now, task_id))
            connection.commit()
            return {"status": "closed", "task_id": task_id, "closed_at": now}
        except Exception:
            connection.rollback()
            raise

    def export_work_item(self, *, user: dict, task_id: str) -> tuple[str, str]:
        task = self.database.connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("工作项不存在。")
        self._role(user, task["company_id"], {"accountant", "reviewer"})
        if task["status"] != "closed":
            raise ValueError("只有已关闭的工作项可以导出最终结果。")
        snapshots = self.database.connection.execute("SELECT COUNT(*) FROM enterprise_task_invoice_snapshots WHERE task_id=?", (task_id,)).fetchone()[0]
        if int(snapshots) == 0:
            raise ValueError("此历史工作项没有关闭时结果快照，不能用当前已变化的发票数据冒充历史导出。")
        rows = self.database.connection.execute(
            """SELECT c.name AS company_name,w.period,s.issue_date,s.source_system,s.source_invoice_id,
                      s.seller_name,s.project_name,s.amount_text,s.currency,s.source_status,s.purpose,s.remarks,
                      s.review_status AS status,s.disposition
               FROM enterprise_task_invoice_snapshots s JOIN enterprise_work_items w USING(task_id)
               JOIN companies c USING(company_id) WHERE s.task_id=? ORDER BY s.issue_date,s.invoice_id""", (task_id,)
        ).fetchall()
        fields = ["company_name", "period", "issue_date", "source_system", "source_invoice_id", "seller_name", "project_name", "amount", "currency", "source_status", "purpose", "remarks", "review_status", "disposition"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            value = dict(row)
            value["amount"] = value.pop("amount_text")
            value["review_status"] = value.pop("status")
            writer.writerow(value)
        filename = f"enterprise-{task['company_id']}-{task['period']}-{task_id}.csv"
        return filename, "\ufeff" + output.getvalue()

    def list_rules(self, *, user: dict, company_id: str) -> dict:
        self._role(user, company_id, {"accountant", "reviewer"})
        return RuleManager(self.database).list_rules(company_id=company_id)


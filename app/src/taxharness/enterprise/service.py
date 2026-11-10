from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..rule_manager import RuleManager
from ..rules import applicable_rule, condition_execution
from ..services import TaxService
from ..storage import row_dict
from .auth import membership, require_role
from .storage import CURRENCY_DIGITS as _CURRENCY_DIGITS, EnterpriseDatabase, utc_now


ACTIVE_TASKS = ("open", "in_progress", "needs_review", "returned")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _period(value: str) -> str:
    if not _PERIOD_RE.fullmatch(str(value)):
        raise ValueError("期间必须为 YYYY-MM。")
    return str(value)


def invoice_key(company_id: str, source_system: str, source_invoice_id: str) -> str:
    source = "\0".join((company_id, source_system, source_invoice_id)).encode("utf-8")
    return "ei-" + hashlib.sha256(source).hexdigest()[:40]


class EnterpriseService:
    def __init__(self, database: EnterpriseDatabase):
        self.database = database

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
                   AND m.role<>'requester' ORDER BY c.name,c.company_id""",
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
                for request in connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE task_id=? AND status='open'", (active_task["task_id"],)):
                    protected.update(json.loads(request["target_invoice_ids_json"]))
                for draft in connection.execute("SELECT invoice_id,candidate_invoice_ids_json FROM enterprise_drafts WHERE task_id=? AND status IN ('proposed','clarification','conflict')", (active_task["task_id"],)):
                    protected.update(json.loads(draft["candidate_invoice_ids_json"]))
                    if draft["invoice_id"]:
                        protected.add(draft["invoice_id"])
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
            clauses.append("w.company_id IN (SELECT company_id FROM enterprise_memberships WHERE user_id=? AND active=1 AND role IN ('accountant','reviewer'))")
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

    def work_item(self, *, user: dict, task_id: str, page: int = 1, page_size: int = 50) -> dict:
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
        return {"status": "ok", "task": dict(task), "invoices": {"page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size, "items": [dict(row) for row in invoices]}, "requests": [dict(row) for row in requests], "drafts": draft_views}

    @staticmethod
    def _decode_draft(row) -> dict:
        value = dict(row)
        value["candidate_invoice_ids"] = json.loads(value.pop("candidate_invoice_ids_json"))
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
            where, args = ("", [])
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
            allowed = [row[0] for row in self.database.connection.execute("SELECT company_id FROM enterprise_memberships WHERE user_id=? AND active=1 AND role IN ('accountant','reviewer')", (user["user_id"],)).fetchall()]
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
        return {"request": value, "task_status": task["status"] if task else "unknown", "invoices": invoices, "responses": [dict(row) for row in responses], "drafts": draft_views}

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
                                  remarks: str | None, source_turn_id: str, user_text: str) -> dict:
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
            invoice = connection.execute("""SELECT i.revision,x.source_status FROM enterprise_task_invoices ti
                JOIN invoices i USING(company_id,invoice_id) JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE ti.task_id=? AND ti.invoice_id=?""", (task_id, invoice_id)).fetchone()
            if invoice is None:
                raise ValueError("这张发票不在当前工作项中。")
            if invoice["source_status"] == "void":
                raise ValueError("该发票已作废，不能创建用途草稿。")
            purpose = purpose.strip() if isinstance(purpose, str) else None
            remarks = remarks.strip() if isinstance(remarks, str) else None
            if not purpose and not remarks:
                raise ValueError("请提供实际用途或备注。")
            existing = connection.execute("""SELECT * FROM enterprise_drafts WHERE task_id=?
                AND status IN ('proposed','clarification','conflict') AND superseded_by IS NULL""", (task_id,)).fetchall()
            for draft in existing:
                if draft["invoice_id"] == invoice_id or invoice_id in json.loads(draft["candidate_invoice_ids_json"]):
                    connection.rollback()
                    return {"status": "existing_draft", "draft": self._decode_draft(draft),
                            "summary": "该票已有待处理草稿，请读取后按版本更正。"}
            draft_id = _id("draft-")
            connection.execute("""INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,
                candidate_invoice_ids_json,proposed_purpose,proposed_remarks,evidence_quote,status,source_revision,origin,source_turn_id,created_by,created_at)
                VALUES(?,NULL,NULL,?,?,?,?,?,?,?,'proposed',?,'chat',?,?,?)""",
                (draft_id, task_id, task["company_id"], invoice_id, _json([invoice_id]), purpose or None, remarks or None,
                 user_text[:3000], int(invoice["revision"]), source_turn_id, user["user_id"], utc_now()))
            result = self._decode_draft(connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone())
            connection.commit()
            return {"status": "created", "draft": result, "invoice_written": False, "summary": "草稿已记录，发票尚未修改。"}
        except Exception:
            connection.rollback()
            raise

    def update_draft(self, *, user: dict, draft_id: str, invoice_id: str | None, purpose: str | None, remarks: str | None,
                     expected_draft_revision: int | None = None) -> dict:
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
            if draft["status"] in {"confirmed", "rejected"}:
                raise ValueError("已确认或已拒绝的草稿不能更改。")
            if expected_draft_revision is not None and (type(expected_draft_revision) is not int or expected_draft_revision != draft["draft_revision"]):
                raise ValueError("草稿已被其他操作更新，请刷新后再修改。")
            chosen = invoice_id or draft["invoice_id"]
            if draft["request_id"]:
                request = connection.execute("SELECT target_invoice_ids_json FROM enterprise_requests WHERE request_id=?", (draft["request_id"],)).fetchone()
                targets = set(json.loads(request["target_invoice_ids_json"]))
            else:
                targets = {row[0] for row in connection.execute("SELECT invoice_id FROM enterprise_task_invoices WHERE task_id=?", (draft["task_id"],)).fetchall()}
            if chosen not in targets:
                raise ValueError("草稿只能绑定原请求中的发票。")
            purpose = purpose.strip() if isinstance(purpose, str) else None
            remarks = remarks.strip() if isinstance(remarks, str) else None
            if not purpose and not remarks:
                raise ValueError("至少要补充一项用途或备注；空值不能被模型草稿清除。")
            current = connection.execute("""SELECT i.revision,x.source_status FROM invoices i JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
                WHERE i.company_id=? AND i.invoice_id=?""", (draft["company_id"], chosen)).fetchone()
            if current is None:
                raise ValueError("草稿关联的发票不存在。")
            if current["source_status"] == "void":
                raise ValueError("该发票已作废，不能更正用途草稿。")
            connection.execute(
                """UPDATE enterprise_drafts SET invoice_id=?,candidate_invoice_ids_json=?,proposed_purpose=?,proposed_remarks=?,
                   needs_clarification=0,unavailable=0,clarification_question='',status='proposed',source_revision=?,draft_revision=draft_revision+1 WHERE draft_id=?""",
                (chosen, _json([chosen]), purpose or None, remarks or None, int(current["revision"]), draft_id),
            )
            connection.execute("UPDATE enterprise_task_invoices SET status='pending',updated_at=? WHERE task_id=? AND invoice_id=? AND status='awaiting_info'", (utc_now(), draft["task_id"], chosen))
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
                       proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,status,source_revision,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, draft["response_id"], draft["request_id"], new_task_id, original["company_id"], draft["invoice_id"], draft["candidate_invoice_ids_json"], draft["proposed_purpose"], draft["proposed_remarks"], draft["evidence_quote"], draft["needs_clarification"], draft["unavailable"], draft["clarification_question"], draft["status"], draft["source_revision"], user["user_id"], now),
                )
                new_drafts.append(draft_id)
            connection.commit()
            return {"status": "created", "task_id": new_task_id, "copied_draft_ids": new_drafts, "original_task_id": task_id}
        except Exception:
            connection.rollback()
            raise

    def save_drafts(self, *, user: dict, response_id: str, items: list[dict], source_revisions: dict[str, int] | None = None) -> list[dict]:
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
            prepared.append((invoice_id, candidates, purpose, remarks, str(item.get("evidence_quote") or "")[:3000], needs_clarification, ambiguous, unavailable, question, revision))
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._role(user, response["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=response["company_id"], modify=True)
            current_response = connection.execute("SELECT r.task_id,r.company_id,r.target_invoice_ids_json,w.status FROM enterprise_requests r JOIN enterprise_work_items w USING(task_id) WHERE r.request_id=?", (response["request_id"],)).fetchone()
            if current_response is None:
                raise ValueError("原请求已不存在，草稿未保存。")
            current_allowed = set(json.loads(current_response["target_invoice_ids_json"]))
            if any(set(candidates) - current_allowed for _, candidates, *_ in prepared):
                raise ValueError("请求范围在生成期间变化，草稿未保存。")
            saved = []
            for invoice_id, candidates, purpose, remarks, quote, needs_clarification, ambiguous, unavailable, question, revision in prepared:
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
                    current = connection.execute("SELECT revision FROM invoices WHERE company_id=? AND invoice_id=?", (response["company_id"], invoice_id)).fetchone()
                    stale_context = current is None or revision is None or int(current["revision"]) != int(revision)
                target_ids = set(candidates or ([invoice_id] if invoice_id else []))
                active_drafts = connection.execute(
                    """SELECT draft_id,invoice_id,candidate_invoice_ids_json FROM enterprise_drafts
                       WHERE task_id=? AND status IN ('proposed','clarification','conflict') AND superseded_by IS NULL""",
                    (response["task_id"],),
                ).fetchall()
                for prior in active_drafts:
                    prior_targets = set(json.loads(prior["candidate_invoice_ids_json"]))
                    if prior["invoice_id"]:
                        prior_targets.add(prior["invoice_id"])
                    if target_ids & prior_targets:
                        connection.execute("UPDATE enterprise_drafts SET status='rejected',superseded_by=?,draft_revision=draft_revision+1 WHERE draft_id=?", (draft_id, prior["draft_id"]))
                connection.execute(
                    """INSERT INTO enterprise_drafts(draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,
                       proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,clarification_question,status,source_revision,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, response_id, response["request_id"], response["task_id"], response["company_id"], invoice_id, _json(candidates), purpose, remarks, quote, int(needs_clarification), int(unavailable), question, "conflict" if stale_context else ("clarification" if ambiguous or unavailable else "proposed"), revision, user["user_id"], utc_now()),
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
                ("drafts_saved" if prepared else "no_new_suggestions", utc_now(), response_id),
            )
            connection.commit()
            return [self._decode_draft(row) for row in saved]
        except Exception:
            connection.rollback()
            raise

    def confirm_drafts(self, *, user: dict, draft_ids: list[str], expected_draft_revisions: dict[str, int] | None = None) -> dict:
        """Confirm each ticket in its own short transaction; report partial success explicitly."""
        if expected_draft_revisions is not None and not isinstance(expected_draft_revisions, dict):
            raise ValueError("草稿版本必须按草稿编号提供。")
        revisions = expected_draft_revisions or {}
        outcomes = [self._confirm_one(user=user, draft_id=draft_id, expected_draft_revision=revisions.get(draft_id)) for draft_id in dict.fromkeys(draft_ids)]
        succeeded = sum(item["status"] in {"committed", "duplicate", "no_change"} for item in outcomes)
        failed = len(outcomes) - succeeded
        return {"status": "ok" if failed == 0 else ("partial" if succeeded else "failed"), "confirmed": succeeded, "failed": failed, "items": outcomes}

    def _confirm_one(self, *, user: dict, draft_id: str, expected_draft_revision: int | None = None) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            draft = connection.execute("SELECT * FROM enterprise_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if draft is None:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_found"}
            self._role(user, draft["company_id"], {"accountant"})
            TaxService(self.database).require_access(user_id=user["user_id"], company_id=draft["company_id"], modify=True)
            prior = connection.execute("SELECT * FROM enterprise_confirmation_receipts WHERE draft_id=?", (draft_id,)).fetchone()
            if prior is not None:
                connection.rollback()
                return {"draft_id": draft_id, "status": "duplicate", "result": json.loads(prior["result_json"])}
            if expected_draft_revision is not None and (type(expected_draft_revision) is not int or expected_draft_revision != draft["draft_revision"]):
                connection.rollback()
                return {"draft_id": draft_id, "status": "draft_conflict", "summary": "草稿已更新，请刷新后确认最新内容。", "invoice_unchanged": True}
            if draft["unavailable"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "unavailable", "summary": "经办人明确无法提供信息；草稿保持待处理，须由复核人按企业授权作出处置。"}
            if not draft["invoice_id"] or draft["needs_clarification"]:
                connection.rollback()
                return {"draft_id": draft_id, "status": "needs_clarification", "summary": "歧义草稿须先取得澄清，不能确认。"}
            task = connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (draft["task_id"],)).fetchone()
            if task is None or task["status"] == "closed":
                connection.rollback()
                return {"draft_id": draft_id, "status": "task_closed", "invoice_unchanged": True}
            source = connection.execute("SELECT source_status FROM enterprise_invoice_extra WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            if source and source["source_status"] == "void":
                connection.rollback()
                return {"draft_id": draft_id, "status": "void_invoice", "summary": "该发票已作废，不能确认写入。", "invoice_unchanged": True}
            current = connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            if current is None:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_found"}
            if int(current["revision"]) != int(draft["source_revision"] or -1):
                connection.execute("UPDATE enterprise_drafts SET status='conflict' WHERE draft_id=?", (draft_id,))
                connection.commit()
                return {"draft_id": draft_id, "status": "revision_conflict", "current_revision": int(current["revision"]), "draft_revision": draft["source_revision"], "invoice_unchanged": True}
            if draft["status"] not in {"proposed", "conflict"}:
                connection.rollback()
                return {"draft_id": draft_id, "status": "not_confirmable"}
            if draft["proposed_purpose"] is not None and not draft["proposed_purpose"].strip():
                connection.rollback()
                return {"draft_id": draft_id, "status": "invalid_value", "summary": "空用途不能由模型草稿确认。"}
            before = dict(current)
            purpose = draft["proposed_purpose"] if draft["proposed_purpose"] is not None else current["purpose"]
            remarks = draft["proposed_remarks"] if draft["proposed_remarks"] is not None else current["remarks"]
            changed = purpose != current["purpose"] or remarks != current["remarks"]
            if changed:
                connection.execute(
                    "UPDATE invoices SET purpose=?,remarks=?,revision=revision+1 WHERE company_id=? AND invoice_id=? AND revision=?",
                    (purpose, remarks, draft["company_id"], draft["invoice_id"], current["revision"]),
                )
            after = dict(connection.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone())
            link = connection.execute("SELECT field_owners_json FROM enterprise_invoice_links WHERE company_id=? AND invoice_id=?", (draft["company_id"], draft["invoice_id"])).fetchone()
            owners = json.loads(link["field_owners_json"] or "{}")
            if draft["proposed_purpose"] is not None:
                owners["purpose"] = "local_confirmed"
            if draft["proposed_remarks"] is not None:
                owners["remarks"] = "local_confirmed"
            connection.execute("UPDATE enterprise_invoice_links SET field_owners_json=?,updated_at=? WHERE company_id=? AND invoice_id=?", (_json(owners), utc_now(), draft["company_id"], draft["invoice_id"]))
            connection.execute("UPDATE enterprise_invoice_extra SET purpose_review_status='pending',disposition=NULL,updated_at=? WHERE company_id=? AND invoice_id=?", (utc_now(), draft["company_id"], draft["invoice_id"]))
            connection.execute("UPDATE enterprise_task_invoices SET status='awaiting_review',disposition=NULL,updated_at=? WHERE task_id=? AND invoice_id=?", (utc_now(), draft["task_id"], draft["invoice_id"]))
            connection.execute("UPDATE enterprise_work_items SET status='needs_review',revision=revision+1,updated_at=? WHERE task_id=? AND status<>'closed'", (utc_now(), draft["task_id"]))
            operation_id = f"{user['user_id']}:{draft['company_id']}:invoice:{draft['invoice_id']}:{draft_id}"
            result = {"status": "committed" if changed else "no_change", "draft_id": draft_id, "invoice_id": draft["invoice_id"], "before": before, "after": after, "confirmed_by": user["user_id"], "task_status": "needs_review"}
            payload = {"changes": {"purpose": draft["proposed_purpose"], "remarks": draft["proposed_remarks"]}, "draft_id": draft_id}
            payload_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
            TaxService._record_operation(connection, user["user_id"], draft["company_id"], draft["invoice_id"], operation_id, payload_hash, result["status"], result, "enterprise_confirm_draft", payload)
            connection.execute("UPDATE enterprise_drafts SET status='confirmed',draft_revision=draft_revision+1 WHERE draft_id=?", (draft_id,))
            connection.execute("INSERT INTO enterprise_confirmation_receipts(draft_id,company_id,invoice_id,operation_id,confirmed_by,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (draft_id, draft["company_id"], draft["invoice_id"], operation_id, user["user_id"], _json(result), utc_now()))
            connection.commit()
            return {"draft_id": draft_id, **result}
        except PermissionError as error:
            connection.rollback()
            return {"draft_id": draft_id, "status": "permission_denied", "summary": str(error), "invoice_unchanged": True}
        except Exception:
            connection.rollback()
            raise

    def review_invoice(self, *, user: dict, task_id: str, invoice_id: str, action: str, comment: str = "") -> dict:
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
            source = connection.execute("SELECT source_status FROM enterprise_invoice_extra WHERE company_id=? AND invoice_id=?", (task["company_id"], invoice_id)).fetchone()
            if source and source["source_status"] == "void":
                raise ValueError("该发票已作废，不需要按有效票据复核。")
            setting = connection.execute("SELECT * FROM enterprise_company_settings WHERE company_id=?", (task["company_id"],)).fetchone()
            if action == "approve":
                current = connection.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (task["company_id"], invoice_id)).fetchone()
                if current is None or not str(current["purpose"] or "").strip():
                    raise ValueError("用途为空，不能批准；如需保留空值，须配置并明确批准允许的处置。")
                if link["status"] == "awaiting_review":
                    submitter = connection.execute(
                        """SELECT r.confirmed_by FROM enterprise_confirmation_receipts r
                           JOIN enterprise_drafts d USING(draft_id) WHERE r.invoice_id=? AND r.company_id=? AND d.task_id=?
                           ORDER BY r.created_at DESC LIMIT 1""", (invoice_id, task["company_id"], task_id)
                    ).fetchone()
                    submitter_id = submitter["confirmed_by"] if submitter else None
                elif link["status"] in {"pending", "returned"}:
                    submitter = connection.execute(
                        """SELECT i.user_id FROM enterprise_import_rows r JOIN enterprise_imports i USING(import_id)
                           WHERE r.company_id=? AND r.invoice_id=? ORDER BY i.created_at DESC LIMIT 1""", (task["company_id"], invoice_id)
                    ).fetchone()
                    submitter_id = submitter["user_id"] if submitter else task["created_by"]
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
                current = connection.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (task["company_id"], invoice_id)).fetchone()
                if current and str(current["purpose"] or "").strip():
                    raise ValueError("用途已有值，不能按缺用途豁免处置。")
                new_status, disposition = "waived", "waive_missing"
            review_id = _id("review-")
            latest_draft = connection.execute("SELECT draft_id FROM enterprise_drafts WHERE task_id=? AND invoice_id=? AND status='confirmed' ORDER BY created_at DESC LIMIT 1", (task_id, invoice_id)).fetchone()
            connection.execute("INSERT INTO enterprise_reviews(review_id,task_id,company_id,invoice_id,draft_id,reviewer_user_id,action,comment,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (review_id, task_id, task["company_id"], invoice_id, latest_draft["draft_id"] if latest_draft else None, user["user_id"], action, comment.strip(), utc_now()))
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


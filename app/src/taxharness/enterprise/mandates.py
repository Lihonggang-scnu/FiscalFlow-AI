"""A bounded authorization for background preparation of one work item."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from ..errors import PermissionDeniedError, TaxHarnessError
from ..services import TaxService
from .extraction_queue import ExtractionQueue
from .service import EnterpriseService
from .storage import EnterpriseDatabase, utc_now


class WorkMandates:
    def __init__(self, database: EnterpriseDatabase):
        self.database = database
        self.service = EnterpriseService(database)
        database.connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_work_mandates (
            mandate_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, company_id TEXT NOT NULL, period TEXT NOT NULL,
            grantor TEXT NOT NULL, allowed_actions_json TEXT NOT NULL, budget_ref TEXT NOT NULL,
            valid_until TEXT NOT NULL, max_steps_per_run INTEGER NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, last_outcome TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(enterprise_work_mandates)")}
        if "last_checked_at" not in columns:
            database.connection.execute("ALTER TABLE enterprise_work_mandates ADD COLUMN last_checked_at TEXT")
        database.connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS enterprise_one_active_mandate
            ON enterprise_work_mandates(task_id) WHERE status='active'""")
        ExtractionQueue(database)

    def _task(self, user: dict, task_id: str, *, view_only: bool = False):
        task = self.database.connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("工作项不存在。")
        self.service._role(user, task["company_id"], {"accountant", "reviewer"} if view_only else {"accountant"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"], modify=not view_only)
        return task

    @staticmethod
    def _view(row) -> dict:
        value = {key: row[key] for key in ("mandate_id", "task_id", "company_id", "period", "grantor", "valid_until",
                 "max_steps_per_run", "status", "revision", "last_outcome", "created_at", "updated_at")}
        value["allowed_actions"] = json.loads(row["allowed_actions_json"])
        return value

    def list(self, *, user: dict, task_id: str) -> list[dict]:
        self._task(user, task_id, view_only=True)
        rows = self.database.connection.execute("SELECT * FROM enterprise_work_mandates WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()
        return [self._view(row) for row in rows]

    def create(self, *, user: dict, task_id: str, valid_until: str, budget_ref: str, max_steps_per_run: int = 10) -> dict:
        try:
            deadline = datetime.fromisoformat(valid_until)
        except (TypeError, ValueError) as error:
            raise ValueError("委托结束时间格式无效。") from error
        if deadline.tzinfo is None or deadline.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("委托结束时间必须包含时区并晚于当前时间。")
        if type(max_steps_per_run) is not int or not 1 <= max_steps_per_run <= 50 or not budget_ref:
            raise ValueError("请配置有效的运行次数上限和预算。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(user, task_id)
            if task["status"] == "closed":
                raise ValueError("已关闭工作项不能建立持续办理委托。")
            existing = connection.execute("SELECT * FROM enterprise_work_mandates WHERE task_id=? AND status='active'", (task_id,)).fetchone()
            if existing:
                connection.commit()
                return {"status": "existing", "mandate": self._view(existing)}
            mandate_id = "mandate-" + uuid4().hex
            now = utc_now()
            connection.execute("""INSERT INTO enterprise_work_mandates(mandate_id,task_id,company_id,period,grantor,
                allowed_actions_json,budget_ref,valid_until,max_steps_per_run,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)""", (mandate_id, task_id, task["company_id"], task["period"],
                user["user_id"], json.dumps(["read", "extract", "verify", "prepare_review"]), budget_ref,
                deadline.astimezone(UTC).isoformat(), max_steps_per_run, now, now))
            row = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
            connection.commit()
            return {"status": "created", "mandate": self._view(row)}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    def _stop(self, connection, mandate_id: str, status: str, reason: str) -> None:
        connection.execute("""UPDATE enterprise_work_mandates SET status=?,revision=revision+1,last_outcome=?,updated_at=?
            WHERE mandate_id=? AND status='active'""", (status, reason, utc_now(), mandate_id))
        connection.execute("""UPDATE enterprise_extraction_runs SET state='cancelled',cancel_requested=1,
            generation=generation+1,error_code=?,updated_at=? WHERE state IN ('queued','running') AND run_id IN
            (SELECT run_id FROM enterprise_mandate_extractions WHERE mandate_id=?)""", (reason, utc_now(), mandate_id))
        connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
            processing_generation=processing_generation+1,last_error_code=?,updated_at=? WHERE processing_status='processing'
            AND processing_run_id IN (SELECT run_id FROM enterprise_mandate_extractions WHERE mandate_id=?)""", (reason, utc_now(), mandate_id))

    def change(self, *, user: dict, mandate_id: str, action: str, expected_revision: int) -> dict:
        if action not in {"pause", "resume", "revoke"}:
            raise ValueError("不支持的委托操作。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
            if row is None:
                raise ValueError("委托不存在。")
            self._task(user, row["task_id"])
            if row["grantor"] != user["user_id"] and not user.get("is_admin"):
                raise PermissionError("只有委托人或管理员可以更改委托。")
            if type(expected_revision) is not int or row["revision"] != expected_revision:
                raise ValueError("委托状态已变化，请刷新后操作。")
            if action in {"pause", "revoke"}:
                if row["status"] == "active":
                    self._stop(connection, mandate_id, "paused" if action == "pause" else "revoked",
                               "mandate_paused" if action == "pause" else "mandate_revoked")
                elif action == "revoke" and row["status"] == "paused":
                    connection.execute("UPDATE enterprise_work_mandates SET status='revoked',revision=revision+1,last_outcome='mandate_revoked',updated_at=? WHERE mandate_id=?", (utc_now(), mandate_id))
                else:
                    raise ValueError("当前委托状态不能执行此操作。")
            else:
                if row["status"] != "paused" or row["valid_until"] <= utc_now():
                    raise ValueError("委托已结束或不在暂停状态。")
                if connection.execute("SELECT 1 FROM enterprise_work_mandates WHERE task_id=? AND status='active'", (row["task_id"],)).fetchone():
                    raise ValueError("此工作项已有另一个有效委托。")
                connection.execute("UPDATE enterprise_work_mandates SET status='active',revision=revision+1,last_outcome='',updated_at=? WHERE mandate_id=?", (utc_now(), mandate_id))
                connection.execute("""UPDATE enterprise_extraction_runs SET state='queued',cancel_requested=0,lease_until=NULL,
                    error_code=NULL,updated_at=? WHERE state='cancelled' AND error_code IN ('mandate_paused','permission_revoked') AND run_id IN
                    (SELECT run_id FROM enterprise_mandate_extractions WHERE mandate_id=?)""", (utc_now(), mandate_id))
            updated = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
            connection.commit()
            return {"status": "ok", "mandate": self._view(updated)}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    def advance(self, *, mandate_id: str) -> dict:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
        if row is None or row["status"] != "active":
            return {"status": "inactive"}
        actor = connection.execute("SELECT user_id,is_admin,active FROM enterprise_users WHERE user_id=?", (row["grantor"],)).fetchone()
        try:
            if actor is None or not actor["active"]:
                raise PermissionError("委托人已停用。")
            task = self._task(dict(actor), row["task_id"])
        except (PermissionError, PermissionDeniedError):
            connection.execute("BEGIN IMMEDIATE")
            self._stop(connection, mandate_id, "paused", "permission_revoked")
            connection.commit()
            return {"status": "paused", "reason": "permission_revoked"}
        if row["valid_until"] <= utc_now() or task["status"] == "closed":
            connection.execute("BEGIN IMMEDIATE")
            self._stop(connection, mandate_id, "completed" if task["status"] == "closed" else "expired", "work_closed" if task["status"] == "closed" else "mandate_expired")
            connection.commit()
            return {"status": "completed" if task["status"] == "closed" else "expired"}
        pending = connection.execute("""SELECT p.response_id FROM enterprise_requests r JOIN enterprise_responses p USING(request_id)
            WHERE r.task_id=? AND r.status<>'cancelled' AND p.extraction_status<>'complete'
            AND p.rowid=(SELECT MAX(p2.rowid) FROM enterprise_responses p2 WHERE p2.request_id=r.request_id)
            AND NOT EXISTS(SELECT 1 FROM enterprise_mandate_extractions m WHERE m.mandate_id=? AND m.response_id=p.response_id)
            AND NOT EXISTS(SELECT 1 FROM enterprise_extraction_runs x WHERE x.response_id=p.response_id)
            ORDER BY p.created_at,p.rowid LIMIT ?""", (row["task_id"], mandate_id, row["max_steps_per_run"])).fetchall()
        if pending:
            queue = ExtractionQueue(self.database)
            runs = [queue.enqueue(user=dict(actor), response_id=item["response_id"], mandate_id=mandate_id) for item in pending]
            connection.execute("UPDATE enterprise_work_mandates SET last_outcome='extraction_queued',updated_at=? WHERE mandate_id=? AND status='active'", (utc_now(), mandate_id))
            return {"status": "queued", "run": runs[0], "runs": runs, "steps_executed": len(runs), "step_limit": row["max_steps_per_run"]}
        current_run = connection.execute("""SELECT x.run_id,x.state,x.error_code FROM enterprise_extraction_runs x
            JOIN enterprise_responses p USING(response_id) JOIN enterprise_requests r USING(request_id)
            WHERE r.task_id=? AND p.extraction_status<>'complete' AND x.state IN ('queued','running','failed','cancelled','needs_attention')
            AND p.rowid=(SELECT MAX(p2.rowid) FROM enterprise_responses p2 WHERE p2.request_id=r.request_id)
            ORDER BY x.created_at DESC LIMIT 1""", (row["task_id"],)).fetchone()
        if current_run:
            reason = "extraction_" + current_run["state"]
            if row["last_outcome"] != reason:
                connection.execute("UPDATE enterprise_work_mandates SET last_outcome=?,updated_at=? WHERE mandate_id=? AND status='active'", (reason, utc_now(), mandate_id))
            if current_run["state"] in {"failed", "cancelled", "needs_attention"}:
                return {"status": "waiting", "reason": reason, "run_id": current_run["run_id"]}
            return {"status": "working", "reason": reason, "run_id": current_run["run_id"]}
        from .work_projection import project_enterprise_work
        projection = project_enterprise_work(self.database, user=dict(actor), task_id=row["task_id"])
        outcome = ("awaiting_accountant" if projection["awaiting_confirmation"] else
                   "awaiting_reviewer" if projection["awaiting_review"] else
                   "awaiting_information" if projection["needs_input"] or projection["waiting"] else "waiting")
        if row["last_outcome"] != outcome:
            connection.execute("UPDATE enterprise_work_mandates SET last_outcome=?,updated_at=? WHERE mandate_id=? AND status='active'", (outcome, utc_now(), mandate_id))
        return {"status": "waiting", "reason": outcome, "next_actions": projection["next_actions"]}

    def advance_active(self) -> list[dict]:
        rows = self.database.connection.execute("SELECT mandate_id FROM enterprise_work_mandates WHERE status='active' ORDER BY COALESCE(last_checked_at,''),created_at LIMIT 20").fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.advance(mandate_id=row["mandate_id"]))
            except (PermissionError, ValueError, TaxHarnessError) as error:
                results.append({"status": "error", "mandate_id": row["mandate_id"], "reason": str(error)})
            finally:
                self.database.connection.execute("UPDATE enterprise_work_mandates SET last_checked_at=? WHERE mandate_id=?", (utc_now(), row["mandate_id"]))
        return results

"""Durable, cancellable extraction jobs for enterprise replies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import PermissionDeniedError
from ..services import TaxService
from .agent import generate_reply_drafts
from .service import EnterpriseService
from .storage import EnterpriseDatabase, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ExtractionQueue:
    def __init__(self, database: EnterpriseDatabase):
        self.database = database
        self.service = EnterpriseService(database)
        database.connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_extraction_runs (
            run_id TEXT PRIMARY KEY, response_id TEXT NOT NULL, task_id TEXT NOT NULL,
            company_id TEXT NOT NULL, created_by TEXT NOT NULL, input_digest TEXT NOT NULL,
            basis_json TEXT NOT NULL, model_input_json TEXT, model_input_hash TEXT, state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            result_json TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(response_id,input_digest))""")
        database.connection.execute("CREATE INDEX IF NOT EXISTS extraction_runs_queue ON enterprise_extraction_runs(state,created_at)")
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(enterprise_extraction_runs)")}
        if not {"model_input_json", "model_input_hash"}.issubset(columns):
            database.connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in database.connection.execute("PRAGMA table_info(enterprise_extraction_runs)")}
                for name in ("model_input_json", "model_input_hash"):
                    if name not in columns:
                        database.connection.execute(f"ALTER TABLE enterprise_extraction_runs ADD COLUMN {name} TEXT")
                database.connection.commit()
            except Exception:
                database.connection.rollback()
                raise
        database.connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_mandate_extractions (
            mandate_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE, response_id TEXT NOT NULL,
            PRIMARY KEY(mandate_id,response_id))""")

    def _response(self, response_id: str):
        row = self.database.connection.execute("""SELECT p.response_id,p.reply_text,p.extraction_status,r.request_id,r.company_id,
            r.task_id,r.status AS request_status,r.target_invoice_ids_json,w.status AS task_status,w.period
            FROM enterprise_responses p JOIN enterprise_requests r USING(request_id)
            JOIN enterprise_work_items w USING(task_id) WHERE p.response_id=?""", (response_id,)).fetchone()
        if row is None:
            raise ValueError("回复不存在。")
        return row

    def _authorize(self, user: dict, company_id: str) -> None:
        self.service._role(user, company_id, {"accountant"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=company_id, modify=True)

    def enqueue(self, *, user: dict, response_id: str, mandate_id: str | None = None) -> dict:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._response(response_id)
            self._authorize(user, row["company_id"])
            if mandate_id:
                mandate = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
                if (mandate is None or mandate["status"] != "active" or mandate["grantor"] != user["user_id"] or
                        mandate["task_id"] != row["task_id"] or mandate["company_id"] != row["company_id"] or
                        mandate["valid_until"] <= utc_now() or "extract" not in json.loads(mandate["allowed_actions_json"])):
                    raise PermissionError("该工作委托已暂停、撤回或不允许提取。")
                linked = connection.execute("""SELECT x.* FROM enterprise_mandate_extractions m
                    JOIN enterprise_extraction_runs x USING(run_id) WHERE m.mandate_id=? AND m.response_id=?""",
                    (mandate_id, response_id)).fetchone()
                if linked:
                    connection.commit()
                    return self._view(linked)
            latest = connection.execute("SELECT response_id FROM enterprise_responses WHERE request_id=? ORDER BY rowid DESC LIMIT 1", (row["request_id"],)).fetchone()
            if latest and latest["response_id"] != response_id:
                raise ValueError("已有更新的经办人回复，请处理最新回复并结合此前说明。")
            active = connection.execute("""SELECT * FROM enterprise_extraction_runs WHERE response_id=?
                AND state IN ('queued','running') AND cancel_requested=0 ORDER BY created_at DESC LIMIT 1""", (response_id,)).fetchone()
            if active:
                connection.commit()
                return self._view(active)
            basis = {"response_id": response_id, "reply_text": row["reply_text"], "task_id": row["task_id"],
                     "request_status": row["request_status"], "task_status": row["task_status"],
                     "extraction_status": row["extraction_status"],
                     "target_invoice_ids": json.loads(row["target_invoice_ids_json"]),
                     "latest_response": latest["response_id"] if latest else response_id,
                     "field_decisions": self.service.field_decision_versions(row["task_id"]),
                     "actor_id": user["user_id"], "mandate_id": mandate_id}
            digest = hashlib.sha256(_json(basis).encode("utf-8")).hexdigest()
            run_id = "extract-" + uuid4().hex
            now = utc_now()
            connection.execute("""INSERT OR IGNORE INTO enterprise_extraction_runs
                (run_id,response_id,task_id,company_id,created_by,input_digest,basis_json,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (run_id, response_id, row["task_id"], row["company_id"],
                user["user_id"], digest, _json(basis), now, now))
            result = connection.execute("SELECT * FROM enterprise_extraction_runs WHERE response_id=? AND input_digest=?", (response_id, digest)).fetchone()
            if mandate_id:
                connection.execute("INSERT OR IGNORE INTO enterprise_mandate_extractions(mandate_id,run_id,response_id) VALUES(?,?,?)",
                                   (mandate_id, result["run_id"], response_id))
            connection.commit()
            return self._view(result)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _view(row) -> dict:
        result = {key: row[key] for key in ("run_id", "response_id", "task_id", "company_id", "created_by", "state",
                  "generation", "lease_until", "attempts", "error_code", "created_at", "updated_at")}
        result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        return result

    @staticmethod
    def _model_calls(ledger_path, user_id: str, run_id: str) -> list[dict]:
        if ledger_path is None or not Path(ledger_path).is_file():
            return []
        with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as ledger:
            ledger.row_factory = sqlite3.Row
            rows = ledger.execute("""SELECT call_id,turn_id,status,output_json FROM agent_model_calls
                WHERE user_id=? AND turn_id LIKE ? ORDER BY created_at,call_id""", (user_id, f"extract:{run_id}:%")).fetchall()
            return [dict(row) for row in rows]

    def get(self, *, user: dict, run_id: str) -> dict:
        row = self.database.connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("提取任务不存在。")
        self.service._role(user, row["company_id"], {"accountant", "reviewer"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=row["company_id"])
        return self._view(row)

    def list_request(self, *, user: dict, request_id: str) -> list[dict]:
        request = self.database.connection.execute("SELECT company_id FROM enterprise_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None:
            raise ValueError("补充请求不存在。")
        self.service._role(user, request["company_id"], {"accountant", "reviewer"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=request["company_id"])
        rows = self.database.connection.execute("""SELECT x.* FROM enterprise_extraction_runs x
            JOIN enterprise_responses p USING(response_id) WHERE p.request_id=? ORDER BY x.created_at DESC,x.rowid DESC""", (request_id,)).fetchall()
        return [self._view(row) for row in rows]

    def cancel(self, *, user: dict, run_id: str) -> dict:
        row = self.database.connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("提取任务不存在。")
        self._authorize(user, row["company_id"])
        if row["created_by"] != user["user_id"] and not user.get("is_admin"):
            raise PermissionError("只能取消自己提交的提取。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._authorize(user, row["company_id"])
            connection.execute("""UPDATE enterprise_extraction_runs SET state='cancelled',cancel_requested=1,
                generation=generation+1,updated_at=? WHERE run_id=? AND state IN ('queued','running')""", (utc_now(), run_id))
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
                processing_generation=processing_generation+1,last_error_code='run_cancelled',updated_at=?
                WHERE processing_run_id=? AND processing_status='processing'""", (utc_now(), run_id))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return self.get(user=user, run_id=run_id)

    def retry(self, *, user: dict, run_id: str) -> dict:
        row = self.database.connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("提取任务不存在。")
        self._authorize(user, row["company_id"])
        if row["created_by"] != user["user_id"] and not user.get("is_admin"):
            raise PermissionError("只能重试自己提交的提取。")
        self.database.connection.execute("""UPDATE enterprise_extraction_runs SET state='queued',cancel_requested=0,
            lease_until=NULL,error_code=NULL,updated_at=? WHERE run_id=? AND state IN ('failed','cancelled')""", (utc_now(), run_id))
        return self.get(user=user, run_id=run_id)

    def claim(self, *, lease_seconds: int = 600) -> dict | None:
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(UTC)
            row = connection.execute("""SELECT x.* FROM enterprise_extraction_runs x WHERE x.cancel_requested=0 AND
                (x.state='queued' OR (x.state='running' AND x.lease_until<?)) AND NOT EXISTS(
                  SELECT 1 FROM enterprise_extraction_runs other WHERE other.response_id=x.response_id AND other.run_id<>x.run_id
                  AND other.state='running' AND other.cancel_requested=0 AND other.lease_until>=?)
                ORDER BY x.created_at LIMIT 1""", (now.isoformat(), now.isoformat())).fetchone()
            if row is None:
                connection.commit()
                return None
            linked = connection.execute("SELECT mandate_id FROM enterprise_mandate_extractions WHERE run_id=?", (row["run_id"],)).fetchone()
            if linked:
                mandate = connection.execute("SELECT * FROM enterprise_work_mandates WHERE mandate_id=?", (linked["mandate_id"],)).fetchone()
                if mandate is None or mandate["status"] != "active" or mandate["valid_until"] <= now.isoformat():
                    connection.execute("UPDATE enterprise_extraction_runs SET state='cancelled',error_code='mandate_inactive',updated_at=? WHERE run_id=?", (utc_now(), row["run_id"]))
                    connection.commit()
                    return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (row["run_id"],)).fetchone())
            actor = connection.execute("SELECT user_id,is_admin,active FROM enterprise_users WHERE user_id=?", (row["created_by"],)).fetchone()
            try:
                if actor is None or not actor["active"]:
                    raise PermissionError("账号已停用。")
                self._authorize(dict(actor), row["company_id"])
            except (PermissionError, PermissionDeniedError):
                connection.execute("UPDATE enterprise_extraction_runs SET state='cancelled',error_code='permission_revoked',updated_at=? WHERE run_id=?", (utc_now(), row["run_id"]))
                connection.commit()
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (row["run_id"],)).fetchone())
            current = self._response(row["response_id"])
            latest = connection.execute("SELECT response_id FROM enterprise_responses WHERE request_id=? ORDER BY rowid DESC LIMIT 1", (current["request_id"],)).fetchone()
            basis = json.loads(row["basis_json"])
            if basis.get("extraction_status") != "complete" and current["extraction_status"] == "complete":
                connection.execute("""UPDATE enterprise_extraction_runs SET state='completed',result_json=?,updated_at=?
                    WHERE run_id=?""", (_json({"already_extracted": True, "draft_ids": []}), utc_now(), row["run_id"]))
                connection.commit()
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (row["run_id"],)).fetchone())
            if (latest and latest["response_id"] != basis["latest_response"]) or (current["request_status"] == "cancelled" and basis["request_status"] != "cancelled") or (current["task_status"] == "closed" and basis["task_status"] != "closed"):
                connection.execute("UPDATE enterprise_extraction_runs SET state='stale',error_code='scope_changed',updated_at=? WHERE run_id=?", (utc_now(), row["run_id"]))
                connection.commit()
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (row["run_id"],)).fetchone())
            if row["state"] == "running":
                connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
                    processing_generation=processing_generation+1,last_error_code='worker_reclaimed',updated_at=?
                    WHERE processing_run_id=? AND processing_status='processing'""", (utc_now(), row["run_id"]))
            connection.execute("""UPDATE enterprise_extraction_runs SET state='running',generation=generation+1,
                attempts=attempts+1,lease_until=?,updated_at=? WHERE run_id=?""",
                ((now + timedelta(seconds=max(1, lease_seconds))).isoformat(), now.isoformat(), row["run_id"]))
            claimed = connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (row["run_id"],)).fetchone()
            connection.commit()
            return self._view(claimed)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    def run_once(self, *, model=None, vision_model=None, live_authorized: bool = False, budget_path=None, ledger_path=None) -> dict | None:
        if model is None and live_authorized:
            if ledger_path is None:
                raise ValueError("自动提取需要配置已授权的共享预算账本。")
            from .agent import _offpeak_check
            try:
                _offpeak_check()
            except RuntimeError:
                return None
        run = self.claim()
        if run is None or run["state"] != "running":
            return run
        connection = self.database.connection
        if model is None and live_authorized:
            link = connection.execute("SELECT mandate_id FROM enterprise_mandate_extractions WHERE run_id=?", (run["run_id"],)).fetchone()
            mandate = connection.execute("SELECT budget_ref FROM enterprise_work_mandates WHERE mandate_id=?", (link["mandate_id"],)).fetchone() if link else None
            if mandate and Path(mandate["budget_ref"]).resolve() != Path(ledger_path).resolve():
                connection.execute("""UPDATE enterprise_extraction_runs SET state='cancelled',error_code='budget_changed',updated_at=?
                    WHERE run_id=? AND state='running' AND generation=?""", (utc_now(), run["run_id"], run["generation"]))
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())
        actor = dict(connection.execute("SELECT user_id,is_admin,active FROM enterprise_users WHERE user_id=?", (run["created_by"],)).fetchone())
        try:
            calls = self._model_calls(ledger_path, actor["user_id"], run["run_id"])
            unresolved, completed_calls = [], {}
            for item in calls:
                if item["status"] in {"pending", "unknown"}:
                    unresolved.append(item)
                elif item["status"] == "settled":
                    try:
                        output = json.loads(item["output_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        output = {}
                    required = "reply" if item["turn_id"].endswith(":text") else "content"
                    if required not in output:
                        unresolved.append(item)
                    else:
                        completed_calls[item["turn_id"]] = output
            if unresolved:
                connection.execute("""UPDATE enterprise_extraction_runs SET state='needs_attention',error_code='model_call_unresolved',
                    result_json=?,updated_at=? WHERE run_id=? AND state='running' AND generation=?""",
                    (_json({"calls": [{"call_id": item["call_id"], "status": item["status"]} for item in unresolved]}),
                     utc_now(), run["run_id"], run["generation"]))
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())
            class ReplayModel:
                def __init__(self, content):
                    self.content = content
                def invoke(self, *_args, **_kwargs):
                    from types import SimpleNamespace
                    return SimpleNamespace(content=self.content, usage_metadata={})
            from .reply_attachments import list_reply_attachments, process_attachment
            guard = {"run_id": run["run_id"], "generation": run["generation"]}
            attachments = list_reply_attachments(self.database, user=actor, response_id=run["response_id"])
            for index, attachment in enumerate(attachments):
                if attachment["processing_status"] == "processing":
                    connection.execute("""UPDATE enterprise_extraction_runs SET state='queued',lease_until=NULL,updated_at=?
                        WHERE run_id=? AND state='running' AND generation=?""", (utc_now(), run["run_id"], run["generation"]))
                    return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())
                if attachment["processing_status"] == "pending":
                    replay = completed_calls.get(f"extract:{run['run_id']}:attachment:{attachment['attachment_id']}")
                    current_vision_model = ReplayModel(replay["content"]) if replay and "content" in replay else vision_model
                    budget_limit = None
                    if current_vision_model is None and live_authorized:
                        from .agent import _budget_config, _budget_limit
                        budget_limit = _budget_limit(_budget_config(budget_path))
                    attachments[index] = process_attachment(self.database, user=actor, attachment_id=attachment["attachment_id"],
                        model=current_vision_model, live_authorized=live_authorized, budget_ledger_path=ledger_path,
                        budget_usd=budget_limit, run_guard=guard,
                        turn_id=f"extract:{run['run_id']}:attachment:{attachment['attachment_id']}")
                latest_run = connection.execute("SELECT state,generation FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone()
                if latest_run["state"] != "running" or latest_run["generation"] != run["generation"]:
                    return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())
            response = self._response(run["response_id"])
            if attachments and not response["reply_text"].strip() and not any(item["processing_status"] == "complete" for item in attachments):
                connection.execute("""UPDATE enterprise_extraction_runs SET state='failed',error_code='attachments_unavailable',updated_at=?
                    WHERE run_id=? AND state='running' AND generation=?""", (utc_now(), run["run_id"], run["generation"]))
                return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())
            replay = completed_calls.get(f"extract:{run['run_id']}:text")
            current_model = ReplayModel(replay["reply"]) if replay and "reply" in replay else model
            generate_reply_drafts(self.database, user=actor, response_id=run["response_id"], model=current_model,
                live_authorized=live_authorized, budget_path=budget_path, ledger_path=ledger_path,
                run_guard=guard, attachment_observations=attachments)
        except Exception as error:
            state = "cancelled" if isinstance(error, (PermissionError, PermissionDeniedError)) else "failed"
            connection.execute("""UPDATE enterprise_extraction_runs SET state=?,error_code=?,updated_at=?
                WHERE run_id=? AND state='running' AND generation=?""", (state, type(error).__name__, utc_now(), run["run_id"], run["generation"]))
        return self._view(connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run["run_id"],)).fetchone())

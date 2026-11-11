from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from httpx import HTTPError as HttpError
from starlette.concurrency import run_in_threadpool

from ..errors import PermissionDeniedError, TaxHarnessError
from ..agent_store import AgentStore
from ..conversation_history import companies_used
from ..preferences import PreferenceManager, PreferenceStore
from ..provider import provider_config
from ..rule_manager import RuleManager
from ..services import TaxService
from .agent import _budget_config, _budget_limit, generate_reply_drafts
from .auth import (
    SESSION_COOKIE, admin_count, create_first_admin, create_user, current_user, login,
    refresh_csrf, require_csrf, reset_password, revoke_session, set_session_cookie,
)
from .imports import IdentityReviewConflict, ImportConflictChanged, ImportManager
from .professional import (ProfessionalConflict, available_sources, correct_fact, list_facts,
                           preview_method, publish_method, record_fact, review_basis, withdraw_fact)
from .service import EnterpriseService
from .storage import EnterpriseDatabase, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE = PROJECT_ROOT / "artifacts" / "enterprise_delivery_20261027" / "enterprise.sqlite3"
DEFAULT_LEDGER = PROJECT_ROOT / "artifacts" / "agent" / "agent.sqlite3"
INDEX_FILE = Path(__file__).resolve().parent / "static" / "index.html"


def create_app(*, database_path: str | Path | None = None, agent_store_path: str | Path | None = None, model=None,
               assistant_model=None, vision_model=None, cookie_secure: bool | None = None) -> FastAPI:
    from .extraction_http import extraction_lifespan
    app = FastAPI(title="FiscalFlow 企业票据工作台", version="2.0.0", docs_url="/api/docs", redoc_url=None, lifespan=extraction_lifespan)
    app.state.enterprise_database = str(Path(database_path or os.getenv("TAXHARNESS_ENTERPRISE_DATABASE", str(DEFAULT_DATABASE))).expanduser().resolve())
    app.state.agent_store = str(Path(agent_store_path or os.getenv("TAXHARNESS_AGENT_STORE", str(DEFAULT_LEDGER))).expanduser().resolve())
    app.state.enterprise_model = model
    app.state.enterprise_assistant_model = assistant_model
    app.state.enterprise_vision_model = vision_model
    app.state.assistant_state_dir = Path(app.state.enterprise_database).parent / "assistant"
    app.state.cookie_secure = bool(cookie_secure if cookie_secure is not None else os.getenv("TAXHARNESS_ENTERPRISE_COOKIE_SECURE", "false").lower() == "true")

    def get_database():
        with EnterpriseDatabase(app.state.enterprise_database) as database:
            yield database

    def get_actor(request: Request, database: EnterpriseDatabase = Depends(get_database)):
        return current_user(database, request)

    def get_mutating_actor(request: Request, state=Depends(get_actor)):
        user, session = state
        require_csrf(request, session)
        return user

    def fail(error: Exception):
        if isinstance(error, HTTPException):
            raise error
        if isinstance(error, (PermissionError, PermissionDeniedError)):
            raise HTTPException(status_code=403, detail=str(error)) from error
        if isinstance(error, (ValueError, TaxHarnessError)):
            raise HTTPException(status_code=400, detail=str(error)) from error
        if isinstance(error, (IdentityReviewConflict, ImportConflictChanged)):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, ProfessionalConflict):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, RuntimeError):
            raise HTTPException(status_code=503, detail=str(error)) from error
        if isinstance(error, sqlite3.OperationalError):
            raise HTTPException(status_code=503, detail="企业数据库暂时忙或不可用；请先刷新读取当前状态，再决定是否重试。") from error
        if isinstance(error, HttpError):
            raise HTTPException(status_code=502, detail="模型服务未返回可确认结果；本轮已按未知预留记账，不会自动重发。") from error
        if isinstance(error, sqlite3.IntegrityError):
            raise HTTPException(status_code=409, detail=f"数据库约束冲突：{error}") from error
        raise error

    def admin(user: dict):
        if not user["is_admin"]:
            raise HTTPException(status_code=403, detail="只有系统管理员能执行此操作。")

    def preferences(database: EnterpriseDatabase) -> PreferenceManager:
        path = Path(app.state.enterprise_database).parent / "preferences.sqlite3"
        return PreferenceManager(PreferenceStore(path), database)

    @app.get("/", response_class=HTMLResponse)
    def home():
        if not INDEX_FILE.is_file():
            return HTMLResponse("企业工作台尚未安装。", status_code=503)
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health(database: EnterpriseDatabase = Depends(get_database)):
        enterprise_live = os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true"
        provider_live = os.getenv("TAXHARNESS_LIVE_MODEL_ENABLED", "false").lower() == "true"
        budget_path = Path(os.getenv("TAXHARNESS_ENTERPRISE_BUDGET", str(Path(app.state.enterprise_database).parent / "budget.json")))
        budget_status = "unavailable"
        if budget_path.is_file():
            try:
                budget_status = json.loads(budget_path.read_text(encoding="utf-8")).get("status", "invalid")
            except (OSError, json.JSONDecodeError):
                budget_status = "invalid"
        return {"status": "ok", "service": "enterprise", "database_ready": True, "schema_version": database.get_meta("enterprise_schema_version"), "model": {"model": "deepseek-flash", "enterprise_enabled": enterprise_live, "provider_enabled": provider_live, "budget_status": budget_status, "live_call_enabled": enterprise_live and provider_live and budget_status == "active"}}

    @app.get("/api/setup/status")
    def setup_status(database: EnterpriseDatabase = Depends(get_database)):
        return {"admin_configured": bool(admin_count(database))}

    @app.post("/api/setup/admin")
    def setup_admin(payload: dict = Body(...), database: EnterpriseDatabase = Depends(get_database)):
        try:
            user = create_first_admin(database, username=payload.get("username", ""), display_name=payload.get("display_name", ""), password=payload.get("password", ""))
            pair = login(database, username=payload.get("username", ""), password=payload.get("password", ""))
            if pair is None:
                raise RuntimeError("首启管理员创建成功但登录失败。")
            session_user, token, csrf = pair
            response = JSONResponse({"status": "ok", "user": session_user, "csrf_token": csrf}, status_code=200)
            set_session_cookie(response, token, secure=app.state.cookie_secure)
            return response
        except Exception as error:
            fail(error)

    @app.post("/api/login")
    def login_route(payload: dict = Body(...), database: EnterpriseDatabase = Depends(get_database)):
        pair = login(database, username=payload.get("username", ""), password=payload.get("password", ""))
        if pair is None:
            raise HTTPException(status_code=401, detail="用户名或密码不正确。")
        user, token, csrf = pair
        response = JSONResponse({"status": "ok", "user": user, "csrf_token": csrf}, status_code=200)
        set_session_cookie(response, token, secure=app.state.cookie_secure)
        return response

    @app.get("/api/session")
    def session_route(request: Request, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        user, session = state
        csrf = refresh_csrf(database, session, request)
        companies = EnterpriseService(database).list_companies(user)
        return {"user": user, "csrf_token": csrf, "companies": companies}

    @app.post("/api/logout")
    def logout_route(request: Request, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        revoke_session(database, request)
        response = Response(content='{"status":"ok"}', media_type="application/json")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/companies")
    def list_companies(state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        return {"items": EnterpriseService(database).list_companies(state[0])}

    @app.post("/api/companies")
    def create_company(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).create_company(actor=actor, name=payload.get("name", ""), currency=payload.get("default_currency", "CNY"), require_independent_review=payload.get("require_independent_review", True))
        except Exception as error:
            fail(error)

    @app.put("/api/companies/{company_id}/settings")
    def update_company_settings(company_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).update_company_settings(actor=actor, company_id=company_id, default_currency=payload.get("default_currency", "CNY"), require_independent_review=payload.get("require_independent_review", True), allowed_dispositions=payload.get("allowed_dispositions", []), default_purpose_policy=payload.get("default_purpose_policy", "all_blank"))
        except Exception as error:
            fail(error)

    @app.get("/api/companies/{company_id}")
    def company_detail(company_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .auth import require_role
            require_role(database, state[0], company_id, {"accountant", "reviewer"})
            row = database.connection.execute("SELECT * FROM enterprise_company_settings WHERE company_id=?", (company_id,)).fetchone()
            if row is None:
                raise ValueError("企业不存在。")
            result = dict(row)
            result["allowed_dispositions"] = json.loads(result.pop("allowed_dispositions_json"))
            result["require_independent_review"] = bool(result["require_independent_review"])
            return result
        except Exception as error:
            fail(error)

    @app.get("/api/companies/{company_id}/members")
    def company_members(company_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            svc = EnterpriseService(database)
            svc._role(state[0], company_id, {"accountant", "reviewer"})
            rows = database.connection.execute(
                """SELECT u.user_id,u.username,u.display_name,m.role FROM enterprise_memberships m
                   JOIN enterprise_users u USING(user_id) WHERE m.company_id=? AND m.active=1 AND u.active=1 ORDER BY m.role,u.display_name""",
                (company_id,),
            ).fetchall()
            return {"items": [dict(row) for row in rows]}
        except Exception as error:
            fail(error)

    @app.get("/api/admin/users")
    def users(actor=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor[0])
        return {"items": EnterpriseService(database).list_users()}

    @app.post("/api/admin/users")
    def create_user_route(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor)
        try:
            return create_user(database, username=payload.get("username", ""), display_name=payload.get("display_name", ""), password=payload.get("password", ""), is_admin=bool(payload.get("is_admin", False)))
        except Exception as error:
            fail(error)

    @app.put("/api/admin/membership")
    def membership_route(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor)
        try:
            return EnterpriseService(database).create_membership(user_id=payload["user_id"], company_id=payload["company_id"], role=payload["role"], active=bool(payload.get("active", True)))
        except Exception as error:
            fail(error)

    @app.patch("/api/admin/users/{user_id}")
    def set_user_active(user_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor)
        try:
            return EnterpriseService(database).set_user_active(user_id, bool(payload.get("active", True)))
        except Exception as error:
            fail(error)

    @app.post("/api/admin/users/{user_id}/password")
    def reset_user_password(user_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor)
        try:
            return reset_password(database, admin_user_id=actor["user_id"], target_user_id=user_id, password=payload.get("password", ""))
        except Exception as error:
            fail(error)

    @app.get("/api/import-source-policy")
    def import_source_policy(company_id: str, source_system: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).source_policy(user=state[0], company_id=company_id, source_system=source_system)
        except Exception as error:
            fail(error)

    @app.post("/api/import-source-policy")
    def update_import_source_policy(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).set_source_policy(user=actor, company_id=payload.get("company_id", ""),
                source_system=payload.get("source_system", ""), mode=payload.get("mode", ""), expected_revision=payload.get("expected_revision"))
        except Exception as error:
            fail(error)

    @app.post("/api/invoices/import/columns")
    async def import_columns(company_id: str = Form(...), file: UploadFile = File(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        content = await file.read(32 * 1024 * 1024 + 1)
        if len(content) > 32 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="单个文件最大支持 32 MiB。")
        try:
            return ImportManager(database).scan_columns(user=actor, company_id=company_id, filename=file.filename or "import.csv", content=content)
        except Exception as error:
            fail(error)

    @app.post("/api/invoices/import/preview")
    async def import_preview(company_id: str = Form(...), source_system: str = Form(...), mapping_json: str = Form("{}"), file: UploadFile = File(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        content = await file.read(32 * 1024 * 1024 + 1)
        if len(content) > 32 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="单个文件最大支持 32 MiB。")
        try:
            mapping = json.loads(mapping_json)
            if not isinstance(mapping, dict):
                raise ValueError("字段映射必须是 JSON 对象。")
            return ImportManager(database).preview(user=actor, company_id=company_id, source_system=source_system, filename=file.filename or "import.csv", content=content, mapping=mapping)
        except Exception as error:
            fail(error)

    @app.post("/api/imports/{preview_id}/commit")
    def import_commit(preview_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).commit(user=actor, preview_id=preview_id)
        except Exception as error:
            fail(error)

    @app.get("/api/identity-reviews")
    def identity_reviews(company_id: str, status: str = "pending", page: int = 1, page_size: int = 50,
                         state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).list_identity_reviews(user=state[0], company_id=company_id,
                                                                  status=status, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/identity-reviews/{identity_review_id}")
    def identity_review_detail(identity_review_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).identity_review_detail(user=state[0], identity_review_id=identity_review_id)
        except Exception as error:
            fail(error)

    @app.get("/api/identity-reviews/{identity_review_id}/original")
    def identity_review_original(identity_review_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            original = ImportManager(database).identity_review_original(user=state[0], identity_review_id=identity_review_id)
            if original is None:
                raise HTTPException(status_code=404, detail="这条历史导入没有保存原始文件。")
            filename, _media_type, file_hash, content = original
            return Response(content=content, media_type="application/octet-stream", headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                "X-Content-SHA256": file_hash,
                "X-Content-Type-Options": "nosniff",
            })
        except Exception as error:
            fail(error)

    @app.get("/api/source-documents/{document_id}/original")
    def source_document_original(document_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            original = ImportManager(database).source_document_original(user=state[0], document_id=document_id)
            if original is None:
                raise HTTPException(status_code=404, detail="这条历史导入没有保存原始文件。")
            filename, _media_type, file_hash, content = original
            return Response(content=content, media_type="application/octet-stream", headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                "X-Content-SHA256": file_hash,
                "X-Content-Type-Options": "nosniff",
            })
        except Exception as error:
            fail(error)

    @app.post("/api/identity-reviews/{identity_review_id}/resolve")
    def identity_review_resolve(identity_review_id: str, payload: dict = Body(...),
                                actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).resolve_identity_review(
                user=actor, identity_review_id=identity_review_id, action=payload.get("action", ""),
                expected_revision=payload.get("expected_revision"), command_id=payload.get("command_id", ""),
                invoice_id=payload.get("invoice_id"), reason=payload.get("reason", ""),
            )
        except Exception as error:
            fail(error)

    @app.get("/api/professional/review-basis")
    def professional_review_basis(company_id: str, period: str, invoice_ids: list[str] | None = Query(None),
                                  state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return review_basis(database, user=state[0], company_id=company_id, period=period, invoice_ids=invoice_ids)
        except Exception as error:
            fail(error)

    @app.get("/api/professional/sources")
    def professional_sources(company_id: str, period: str, state=Depends(get_actor),
                            database: EnterpriseDatabase = Depends(get_database)):
        try:
            return available_sources(database, user=state[0], company_id=company_id, period=period,
                                     attachment_root=app.state.assistant_state_dir / "uploads")
        except Exception as error:
            fail(error)

    @app.get("/api/professional/facts")
    def professional_facts(company_id: str, period: str, invoice_id: str | None = None,
                           state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return list_facts(database, user=state[0], company_id=company_id, period=period, invoice_id=invoice_id)
        except Exception as error:
            fail(error)

    @app.get("/api/professional/facts/{fact_id}/source")
    def professional_fact_source(fact_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            actor = state[0]
            fact = database.connection.execute("SELECT * FROM enterprise_professional_facts WHERE fact_id=?", (fact_id,)).fetchone()
            if fact is None or fact["source_type"] != "assistant_attachment":
                raise ValueError("该事实没有可直接打开的对话附件。")
            EnterpriseService(database)._role(actor, fact["company_id"], {"accountant", "reviewer"})
            TaxService(database).require_access(user_id=actor["user_id"], company_id=fact["company_id"])
            stored = database.connection.execute("""SELECT * FROM enterprise_original_attachments
                WHERE attachment_id=? AND company_id=?""", (fact["source_id"], fact["company_id"])).fetchone()
            if stored:
                content = bytes(stored["original_content"])
                if hashlib.sha256(content).hexdigest() != fact["source_version"].split(":", 1)[0]:
                    raise ValueError("原始附件不可用或已发生变化。")
                return Response(content=content, media_type=stored["media_type"],
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(stored["filename"])})
            attachment = database.connection.execute("""SELECT * FROM enterprise_assistant_attachments
                WHERE attachment_id=? AND company_id=?""", (fact["source_id"], fact["company_id"])).fetchone()
            if attachment is None:
                raise ValueError("原始附件当前不可用。")
            root = (app.state.assistant_state_dir / "uploads").resolve()
            path = (root / attachment["stored_name"]).resolve()
            if path.parent != root or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != fact["source_version"]:
                raise ValueError("原始附件不可用或已发生变化。")
            return FileResponse(path, media_type=attachment["mime"], filename=attachment["filename"], content_disposition_type="inline")
        except Exception as error:
            fail(error)

    @app.post("/api/professional/facts")
    def professional_fact_create(payload: dict = Body(...), actor=Depends(get_mutating_actor),
                                 database: EnterpriseDatabase = Depends(get_database)):
        try:
            return record_fact(database, user=actor, company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), fact_type=payload.get("fact_type", ""),
                fact_text=payload.get("fact_text", ""), source_type=payload.get("source_type", ""),
                source_id=payload.get("source_id", ""), source_quote=payload.get("source_quote", ""),
                invoice_ids=payload.get("invoice_ids", []), command_id=payload.get("command_id", ""),
                attachment_root=app.state.assistant_state_dir / "uploads")
        except Exception as error:
            fail(error)

    @app.patch("/api/professional/facts/{fact_id}")
    def professional_fact_correct(fact_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor),
                                  database: EnterpriseDatabase = Depends(get_database)):
        try:
            return correct_fact(database, user=actor, fact_id=fact_id, invoice_id=payload.get("invoice_id", ""),
                expected_revision=payload.get("expected_revision"), command_id=payload.get("command_id", ""),
                fact_text=payload.get("fact_text", ""), reason=payload.get("reason", ""))
        except Exception as error:
            fail(error)

    @app.post("/api/professional/facts/{fact_id}/withdraw")
    def professional_fact_withdraw(fact_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor),
                                   database: EnterpriseDatabase = Depends(get_database)):
        try:
            return withdraw_fact(database, user=actor, fact_id=fact_id, invoice_id=payload.get("invoice_id", ""),
                expected_revision=payload.get("expected_revision"), command_id=payload.get("command_id", ""),
                reason=payload.get("reason", ""))
        except Exception as error:
            fail(error)

    @app.post("/api/professional/methods/preview")
    def professional_method_preview(payload: dict = Body(...), state=Depends(get_actor),
                                    database: EnterpriseDatabase = Depends(get_database)):
        try:
            return preview_method(database, user=state[0], company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), method_name=payload.get("method_name", ""),
                criteria=payload.get("criteria", {}))
        except Exception as error:
            fail(error)

    @app.post("/api/professional/methods/publish")
    def professional_method_publish(payload: dict = Body(...), actor=Depends(get_mutating_actor),
                                    database: EnterpriseDatabase = Depends(get_database)):
        try:
            return publish_method(database, user=actor, company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), method_name=payload.get("method_name", ""),
                criteria=payload.get("criteria", {}), expected_version=payload.get("expected_version"),
                preview_token=payload.get("preview_token", ""), command_id=payload.get("command_id", ""))
        except Exception as error:
            fail(error)

    @app.get("/api/autonomy-policy")
    def autonomy_current(company_id: str, period: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .autonomy_policy import AutonomyPolicyService
            return AutonomyPolicyService(database).current(user=state[0], company_id=company_id, period=period)
        except Exception as error:
            fail(error)

    @app.post("/api/autonomy-policy/preview")
    def autonomy_preview(payload: dict = Body(...), state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .autonomy_policy import AutonomyPolicyService
            return AutonomyPolicyService(database).preview(user=state[0], company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), policy=payload.get("policy", {}))
        except Exception as error:
            fail(error)

    @app.post("/api/autonomy-policy/publish")
    def autonomy_publish(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .autonomy_policy import AutonomyPolicyService
            return AutonomyPolicyService(database).publish(user=actor, company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), expected_version=payload.get("expected_version"),
                preview=payload.get("preview", {}), operation_id=payload.get("operation_id"))
        except Exception as error:
            fail(error)

    @app.post("/api/autonomy-policy/action")
    def autonomy_action(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .autonomy_policy import AutonomyPolicyService
            service = AutonomyPolicyService(database)
            action = payload.get("action")
            if action not in {"pause", "resume", "revoke"}:
                raise ValueError("请选择暂停、恢复或撤回。")
            return getattr(service, action)(user=actor, company_id=payload.get("company_id", ""),
                period=payload.get("period", ""), expected_version=payload.get("expected_version"),
                operation_id=payload.get("operation_id"))
        except Exception as error:
            fail(error)

    @app.get("/api/imports")
    def imports_list(company_id: str, page: int = 1, page_size: int = 50, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            EnterpriseService(database)._role(state[0], company_id, {"accountant", "reviewer"})
            page_size = max(1, min(page_size, 100))
            total = database.connection.execute("SELECT COUNT(*) FROM enterprise_imports WHERE company_id=?", (company_id,)).fetchone()[0]
            rows = database.connection.execute("SELECT * FROM enterprise_imports WHERE company_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (company_id, page_size, (max(1, page) - 1) * page_size)).fetchall()
            return {"total": total, "items": [dict(row) for row in rows]}
        except Exception as error:
            fail(error)

    @app.get("/api/imports/conflicts")
    def conflicts(company_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            EnterpriseService(database)._role(state[0], company_id, {"accountant", "reviewer"})
            rows = database.connection.execute("""SELECT c.*,i.revision AS current_revision,
                CASE c.field_name WHEN 'purpose' THEN i.purpose WHEN 'remarks' THEN i.remarks END AS live_value
                FROM enterprise_import_conflicts c JOIN invoices i USING(company_id,invoice_id)
                WHERE c.company_id=? AND c.status='open' ORDER BY c.created_at""", (company_id,)).fetchall()
            return {"items": [dict(row) for row in rows]}
        except Exception as error:
            fail(error)

    @app.post("/api/imports/conflicts/{conflict_id}/resolve")
    def conflict_resolve(conflict_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).resolve_conflict(user=actor, conflict_id=conflict_id, resolution=payload.get("resolution", ""),
                                                           expected_revision=payload.get("expected_revision"))
        except Exception as error:
            fail(error)

    @app.get("/api/finance/summary")
    def finance_summary(company_id: str, period: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).finance_summary(user=state[0], company_id=company_id, period=period)
        except Exception as error:
            fail(error)

    @app.get("/api/invoices")
    def invoice_page(company_id: str, period: str, page: int = 1, page_size: int = 50, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).invoice_page(user=state[0], company_id=company_id, period=period, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/missing")
    def list_missing(company_id: str, period: str, page: int = 1, page_size: int = 100, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).list_missing(user=state[0], company_id=company_id, period=period, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items")
    def work_items(company_id: str | None = None, period: str | None = None, status: str | None = None, page: int = 1, page_size: int = 50, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).list_work_items(user=state[0], company_id=company_id, period=period, status=status, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}")
    def work_item_detail(task_id: str, page: int = 1, page_size: int = 50, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).work_item(user=state[0], task_id=task_id, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/projection")
    def work_item_projection(task_id: str, include_review_basis: bool = True, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .work_projection import project_enterprise_work
            return project_enterprise_work(database, user=state[0], task_id=task_id, include_review_basis=include_review_basis)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/handover")
    def work_item_handover(task_id: str, include_projection: bool = False, detail_page: int | None = None,
                           state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .handover import build_handover
            return build_handover(database, user=state[0], task_id=task_id, include_projection=include_projection, detail_page=detail_page)
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/requests")
    def create_request_route(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).create_request(user=actor, task_id=task_id, respondent_user_id=payload["respondent_user_id"], invoice_ids=payload["invoice_ids"], message=payload.get("message", ""))
        except Exception as error:
            fail(error)

    @app.post("/api/requests/{request_id}/cancel")
    def cancel_request(request_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).cancel_request(user=actor, request_id=request_id)
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/close")
    def close_work_item(task_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).close_work_item(user=actor, task_id=task_id)
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/review")
    def review_invoice(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).review_invoice(user=actor, task_id=task_id, invoice_id=payload["invoice_id"],
                action=payload["action"], review_token=payload.get("review_token"), comment=payload.get("comment", ""))
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/export")
    def export_work_item(task_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            filename, content = EnterpriseService(database).export_work_item(user=actor, task_id=task_id)
            return StreamingResponse(iter([content.encode("utf-8")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/export")
    def download_work_item(task_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            filename, content = EnterpriseService(database).export_work_item(user=state[0], task_id=task_id)
            return StreamingResponse(iter([content.encode("utf-8")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except Exception as error:
            fail(error)

    @app.get("/api/requests")
    def requests(company_id: str | None = None, page: int = 1, page_size: int = 50, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).list_requests(user=state[0], company_id=company_id, page=page, page_size=page_size)
        except Exception as error:
            fail(error)

    @app.get("/api/requests/{request_id}")
    def request_detail(request_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).request_detail(user=state[0], request_id=request_id)
        except Exception as error:
            fail(error)

    @app.post("/api/requests/{request_id}/reply")
    def request_reply(request_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).submit_reply(user=actor, request_id=request_id, reply_text=payload.get("reply_text", ""))
        except Exception as error:
            fail(error)

    @app.post("/api/requests/{request_id}/reply-with-files")
    async def request_reply_with_files(request_id: str, reply_text: str = Form(""), idempotency_key: str = Form(...),
                                       files: list[UploadFile] = File(default=[]), actor=Depends(get_mutating_actor),
                                       database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .reply_attachments import submit_reply_with_attachments
            if len(files) > 10:
                raise ValueError("一次最多上传 10 个文件。")
            uploads = []
            for file in files:
                content = await file.read(32 * 1024 * 1024 + 1)
                uploads.append((file.filename or "上传文件", content))
            return submit_reply_with_attachments(database, user=actor, request_id=request_id, reply_text=reply_text,
                                                 files=uploads, idempotency_key=idempotency_key)
        except Exception as error:
            fail(error)

    @app.get("/api/responses/{response_id}/attachments")
    def response_attachments(response_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .reply_attachments import list_reply_attachments
            items = list_reply_attachments(database, user=state[0], response_id=response_id)
            return {"items": [{key: item.get(key) for key in ("attachment_id", "filename", "media_type", "processing_status",
                            "page_count", "last_error_code", "last_error_message", "result_revision")} for item in items]}
        except Exception as error:
            fail(error)

    @app.get("/api/attachments/{attachment_id}/original")
    def original_attachment(attachment_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .reply_attachments import get_attachment
            attachment = get_attachment(database, user=state[0], attachment_id=attachment_id, include_original=True)
            content = attachment["original_content"]
            if hashlib.sha256(content).hexdigest() != attachment["sha256"]:
                raise ValueError("附件原件校验失败，请联系管理员恢复。")
            return Response(content=content, media_type=attachment["media_type"],
                            headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(attachment["filename"])})
        except Exception as error:
            fail(error)

    @app.post("/api/attachments/{attachment_id}/retry")
    def retry_original_attachment(attachment_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .extraction_queue import ExtractionQueue
            from .reply_attachments import get_attachment, reset_attachment_for_retry, retry_attachment
            attachment = get_attachment(database, user=actor, attachment_id=attachment_id)
            is_reply = attachment["source_kind"] == "reply"
            EnterpriseService(database)._role(actor, attachment["company_id"], {"accountant"} if is_reply else {"accountant", "reviewer"})
            TaxService(database).require_access(user_id=actor["user_id"], company_id=attachment["company_id"], modify=is_reply)
            if attachment["processing_status"] == "complete":
                return {"status": "complete", "attachment": attachment}
            if attachment["source_kind"] == "reply":
                reset_attachment_for_retry(database, user=actor, attachment_id=attachment_id)
                queue = ExtractionQueue(database)
                prior = database.connection.execute("""SELECT x.*,m.mandate_id FROM enterprise_extraction_runs x
                    LEFT JOIN enterprise_mandate_extractions m USING(run_id) WHERE x.response_id=? ORDER BY x.created_at DESC LIMIT 1""",
                    (attachment["response_id"],)).fetchone()
                if prior and prior["state"] in {"failed", "cancelled"} and prior["created_by"] == actor["user_id"] and not prior["mandate_id"]:
                    run = queue.retry(user=actor, run_id=prior["run_id"])
                else:
                    run = queue.enqueue(user=actor, response_id=attachment["response_id"])
                return {"status": "queued", "run": run}
            live_enabled = os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true"
            budget_path = os.getenv("TAXHARNESS_ENTERPRISE_BUDGET")
            budget_limit = _budget_limit(_budget_config(budget_path)) if app.state.enterprise_vision_model is None and live_enabled else None
            result = retry_attachment(database, user=actor, attachment_id=attachment_id, model=app.state.enterprise_vision_model,
                live_authorized=live_enabled, budget_ledger_path=app.state.agent_store, budget_usd=budget_limit)
            return {"status": result["processing_status"], "attachment": result}
        except Exception as error:
            fail(error)

    @app.post("/api/responses/{response_id}/drafts")
    def response_drafts(response_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            live_enabled = os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true"
            return generate_reply_drafts(database, user=actor, response_id=response_id, model=app.state.enterprise_model, live_authorized=live_enabled, ledger_path=app.state.agent_store)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/assistant/sessions")
    def assistant_sessions(task_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            actor = state[0]
            EnterpriseService(database).work_item(user=actor, task_id=task_id, page_size=1)
            rows = database.connection.execute("""SELECT session_id,title,created_at,updated_at FROM enterprise_assistant_sessions
                WHERE task_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 100""", (task_id, actor["user_id"])).fetchall()
            return {"status": "ok", "sessions": [dict(row) for row in rows]}
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/assistant/history")
    def assistant_history(task_id: str, session_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            actor = state[0]
            detail = EnterpriseService(database).work_item(user=actor, task_id=task_id, page_size=1)
            if not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", session_id):
                raise ValueError("对话编号格式无效。")
            linked = database.connection.execute("SELECT 1 FROM enterprise_assistant_sessions WHERE session_id=? AND task_id=? AND user_id=?",
                                                 (session_id, task_id, actor["user_id"])).fetchone()
            if linked is None:
                raise PermissionError("没有该工作项的对话记录。")
            store = AgentStore(app.state.assistant_state_dir / "agent.sqlite3")
            session = store.session(session_id, actor["user_id"])
            if session is not None and session["current_company_id"] != detail["task"]["company_id"]:
                raise PermissionError("该对话属于其他企业，请在原企业打开。")
            turns = store.list_turns(session_id, actor["user_id"], limit=100) if session else []
            for company_id in companies_used(session, turns) if session else []:
                TaxService(database).require_access(user_id=actor["user_id"], company_id=company_id)
                EnterpriseService(database)._role(actor, company_id, {"accountant", "reviewer"})
            attachments = [dict(row) for row in database.connection.execute("""SELECT attachment_id,turn_id,filename,mime FROM enterprise_assistant_attachments
                WHERE session_id=? AND task_id=? AND user_id=? ORDER BY created_at""", (session_id, task_id, actor["user_id"])).fetchall()
            ]
            originals = [dict(row) for row in database.connection.execute("""SELECT attachment_id,associated_turn_id AS turn_id,filename,
                media_type AS mime,processing_status,last_error_message FROM enterprise_original_attachments
                WHERE source_kind='assistant_turn' AND session_id=? AND task_id=? AND user_id=? ORDER BY created_at""",
                (session_id, task_id, actor["user_id"])).fetchall()]
            attachments.extend(originals)
            return {"status": "ok", "turns": [{**{key: item.get(key) for key in ("request_text", "output_text", "status", "created_at")},
                "attachments": [attachment for attachment in attachments if attachment["turn_id"] == item["turn_id"]]} for item in reversed(turns)],
                "pending_attachments": [attachment for attachment in originals if not attachment["turn_id"]]}
        except Exception as error:
            fail(error)

    def run_assistant(task_id: str, message: str, requested_session: str | None, actor: dict,
                      database: EnterpriseDatabase, files: list[tuple[str, bytes]] | None = None,
                      upload_key: str | None = None) -> dict:
            detail = EnterpriseService(database).work_item(user=actor, task_id=task_id, page_size=1)
            company_id = detail["task"]["company_id"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("请输入要办理的内容。")
            session_id = requested_session or "enterprise-chat-" + uuid4().hex
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", session_id):
                raise ValueError("对话编号格式无效。")
            model = app.state.enterprise_assistant_model
            live_enabled = os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true"
            existing = database.connection.execute("SELECT user_id,company_id,task_id FROM enterprise_assistant_sessions WHERE session_id=?", (session_id,)).fetchone()
            if existing and (existing["user_id"] != actor["user_id"] or existing["company_id"] != company_id or existing["task_id"] != task_id):
                raise PermissionError("该对话属于其他用户或工作项。")
            now = utc_now()
            database.connection.execute("""INSERT OR IGNORE INTO enterprise_assistant_sessions(session_id,user_id,company_id,task_id,title,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""", (session_id, actor["user_id"], company_id, task_id, message.strip()[:80], now, now))
            linked = database.connection.execute("SELECT user_id,company_id,task_id FROM enterprise_assistant_sessions WHERE session_id=?", (session_id,)).fetchone()
            if linked["user_id"] != actor["user_id"] or linked["company_id"] != company_id or linked["task_id"] != task_id:
                raise PermissionError("该对话属于其他用户或工作项。")
            database.connection.execute("UPDATE enterprise_assistant_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            attachment_ids = []
            batch_key = None
            if files:
                from .reply_attachments import _normalize_uploads, _response_payload_hash, persist_original_attachment
                batch_key = str(upload_key or uuid4().hex).strip()
                if not batch_key or len(batch_key) > 160:
                    raise ValueError("附件操作标识无效。")
                uploads = _normalize_uploads(files)
                payload_hash = _response_payload_hash(message.strip(), uploads)
                connection = database.connection
                connection.execute("""INSERT OR IGNORE INTO enterprise_assistant_upload_batches
                    (session_id,user_id,upload_key,payload_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                    (session_id, actor["user_id"], batch_key, payload_hash, now, now))
                batch = connection.execute("""SELECT * FROM enterprise_assistant_upload_batches
                    WHERE session_id=? AND user_id=? AND upload_key=?""", (session_id, actor["user_id"], batch_key)).fetchone()
                if batch["payload_hash"] != payload_hash:
                    raise ValueError("同一次附件操作的内容已变化，请重新发送。")
                source_id = "batch-" + hashlib.sha256((session_id + "|" + batch_key).encode()).hexdigest()
                for index, upload in enumerate(uploads):
                    attachment = persist_original_attachment(database, user=actor, session_id=session_id, task_id=task_id,
                        source_id=source_id, upload_key=f"{batch_key}:{index}", filename=upload["filename"], content=upload["content"])
                    attachment_ids.append(attachment["attachment_id"])
                if batch["state"] == "completed":
                    previous = AgentStore(app.state.assistant_state_dir / "agent.sqlite3").get_turn(batch["turn_id"], actor["user_id"])
                    return {"status": previous["status"] if previous else "ok", "summary": "已返回此前的处理结果。",
                            "reply": previous["output_text"] if previous else "请在历史对话中查看结果。",
                            "session_id": session_id, "turn_id": batch["turn_id"], "attachments": attachment_ids, "duplicate": True}
                if batch["state"] in {"running", "needs_attention"}:
                    return {"status": "needs_attention", "summary": "此前处理正在进行或结果需要核对；附件已保留，请查看历史记录。",
                            "session_id": session_id, "attachments": attachment_ids}
            if model is None and not live_enabled:
                if attachment_ids:
                    return {"status": "attachment_pending", "summary": "附件已保存；对话模型尚未开启，可稍后继续。",
                            "session_id": session_id, "attachments": attachment_ids}
                raise RuntimeError("企业对话模型尚未启用。")
            if model is None or (files and app.state.enterprise_vision_model is None and live_enabled):
                budget_path = os.getenv("TAXHARNESS_ENTERPRISE_BUDGET", str(Path(app.state.enterprise_database).parent / "budget.json"))
                budget_limit = _budget_limit(_budget_config(budget_path))
            else:
                budget_limit = None
            if batch_key:
                claimed = database.connection.execute("""UPDATE enterprise_assistant_upload_batches SET state='running',updated_at=?
                    WHERE session_id=? AND user_id=? AND upload_key=? AND state='pending'""",
                    (utc_now(), session_id, actor["user_id"], batch_key)).rowcount
                if not claimed:
                    return {"status": "needs_attention", "summary": "此前处理正在进行，请查看历史记录。", "session_id": session_id, "attachments": attachment_ids}
                from .reply_attachments import process_attachment
                for attachment_id in attachment_ids:
                    process_attachment(database, user=actor, attachment_id=attachment_id, user_text=message,
                        model=app.state.enterprise_vision_model, live_authorized=live_enabled,
                        budget_ledger_path=app.state.agent_store, budget_usd=budget_limit)
            rows = database.connection.execute("""SELECT attachment_id,extracted_json FROM enterprise_assistant_attachments
                WHERE task_id=? AND user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 5""",
                (task_id, actor["user_id"])).fetchall()
            observations = []
            for row in reversed(rows):
                value = json.loads(row["extracted_json"])
                text = value.get("pdf_text", "")
                value["pdf_text"] = text[:12000]
                value["text_truncated"] = len(text) > 12000
                observations.append({"attachment_id": row["attachment_id"], **value})
            original_rows = database.connection.execute("""SELECT attachment_id,filename,processing_status,last_error_message,prepared_json,recognized_json
                FROM enterprise_original_attachments WHERE source_kind='assistant_turn' AND task_id=? AND user_id=?
                ORDER BY created_at DESC,rowid DESC LIMIT 5""", (task_id, actor["user_id"])).fetchall()
            for row in reversed(original_rows):
                prepared = json.loads(row["prepared_json"] or "{}")
                text = prepared.get("text", "")
                observations.append({"attachment_id": row["attachment_id"], "filename": row["filename"],
                    "processing_status": row["processing_status"], "error": row["last_error_message"],
                    "page_count": prepared.get("page_count"), "pdf_text": text[:12000], "text_truncated": len(text) > 12000,
                    **json.loads(row["recognized_json"] or "{}")})
            from .conversation import EnterpriseAgentRuntime
            root = app.state.assistant_state_dir
            root.mkdir(parents=True, exist_ok=True)
            runtime = EnterpriseAgentRuntime(database_path=app.state.enterprise_database, agent_store_path=root / "agent.sqlite3",
                checkpoint_path=root / "checkpoint.sqlite3", preference_store_path=root / "preferences.sqlite3",
                budget_ledger_path=app.state.agent_store, active_task_id=task_id)
            try:
                result = runtime.run_turn(session_id=session_id, user_id=actor["user_id"], company_id=company_id,
                    message=message.strip(), model=model, live_authorized=model is None, budget_usd=budget_limit,
                    attachment_observations=observations)
            except Exception:
                if batch_key:
                    database.connection.execute("""UPDATE enterprise_assistant_upload_batches SET state='needs_attention',updated_at=?
                        WHERE session_id=? AND user_id=? AND upload_key=?""", (utc_now(), session_id, actor["user_id"], batch_key))
                raise
            finally:
                runtime.close()
            if batch_key:
                database.connection.execute("BEGIN IMMEDIATE")
                try:
                    for attachment_id in attachment_ids:
                        database.connection.execute("UPDATE enterprise_original_attachments SET associated_turn_id=? WHERE attachment_id=?",
                                                    (result.get("turn_id"), attachment_id))
                    database.connection.execute("""UPDATE enterprise_assistant_upload_batches SET state=?,turn_id=?,updated_at=?
                        WHERE session_id=? AND user_id=? AND upload_key=?""", ("completed" if result.get("turn_id") else "needs_attention",
                        result.get("turn_id"), utc_now(), session_id, actor["user_id"], batch_key))
                    database.connection.commit()
                except Exception:
                    database.connection.rollback()
                    raise
            elif result.get("turn_id"):
                for attachment_id in attachment_ids:
                    database.connection.execute("UPDATE enterprise_assistant_attachments SET turn_id=? WHERE attachment_id=?", (result["turn_id"], attachment_id))
            return {"status": result.get("status"), "summary": result.get("summary", ""), "reply": result.get("reply", ""),
                    "session_id": session_id, "turn_id": result.get("turn_id"), "attachments": attachment_ids}

    @app.post("/api/work-items/{task_id}/assistant/turn")
    def assistant_turn(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return run_assistant(task_id, payload.get("message"), payload.get("session_id"), actor, database)
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/assistant/turn-with-files")
    async def assistant_turn_with_files(task_id: str, message: str = Form(...), session_id: str = Form(""), upload_key: str = Form(""),
                                        files: list[UploadFile] = File(...), actor=Depends(get_mutating_actor),
                                        database: EnterpriseDatabase = Depends(get_database)):
        try:
            EnterpriseService(database).work_item(user=actor, task_id=task_id, page_size=1)
            if len(files) > 10:
                raise ValueError("一次最多上传 10 个文件。")
            payload = []
            for file in files:
                content = await file.read(32 * 1024 * 1024 + 1)
                if len(content) > 32 * 1024 * 1024:
                    raise ValueError("单个文件最大支持 32 MiB。")
                payload.append((file.filename or "上传文件", content))
            return await run_in_threadpool(run_assistant, task_id, message, session_id or None, actor, database, payload, upload_key or None)
        except Exception as error:
            fail(error)

    @app.get("/api/work-items/{task_id}/assistant/attachments/{attachment_id}")
    def assistant_attachment(task_id: str, attachment_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            actor = state[0]
            EnterpriseService(database).work_item(user=actor, task_id=task_id, page_size=1)
            original = database.connection.execute("SELECT task_id FROM enterprise_original_attachments WHERE attachment_id=? AND source_kind='assistant_turn'", (attachment_id,)).fetchone()
            if original:
                if original["task_id"] != task_id:
                    raise PermissionError("没有该附件的访问权限。")
                from .reply_attachments import get_attachment
                attachment = get_attachment(database, user=actor, attachment_id=attachment_id, include_original=True)
                content = attachment["original_content"]
                if hashlib.sha256(content).hexdigest() != attachment["sha256"]:
                    raise ValueError("附件原件校验失败，请联系管理员恢复。")
                return Response(content=content, media_type=attachment["media_type"],
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(attachment["filename"])})
            row = database.connection.execute("""SELECT * FROM enterprise_assistant_attachments
                WHERE attachment_id=? AND task_id=? AND user_id=?""", (attachment_id, task_id, actor["user_id"])).fetchone()
            if row is None:
                raise PermissionError("没有该附件的访问权限。")
            path = app.state.assistant_state_dir / "uploads" / row["stored_name"]
            return FileResponse(path, media_type=row["mime"], filename=row["filename"], content_disposition_type="inline")
        except Exception as error:
            fail(error)

    @app.post("/api/drafts/confirm")
    def confirm_drafts(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).confirm_drafts(user=actor, draft_ids=payload.get("draft_ids", []),
                expected_draft_revisions=payload.get("expected_draft_revisions"),
                fields_by_draft=payload.get("fields_by_draft"), conditions_by_draft=payload.get("conditions_by_draft"))
        except Exception as error:
            fail(error)

    @app.put("/api/drafts/{draft_id}")
    def update_draft(draft_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            source_text = "工作台表单：" + json.dumps({key: payload.get(key) for key in ("purpose", "remarks", "clear_fields") if key in payload}, ensure_ascii=False, sort_keys=True)
            return EnterpriseService(database).update_draft(user=actor, draft_id=draft_id, invoice_id=payload.get("invoice_id"),
                purpose=payload.get("purpose"), remarks=payload.get("remarks"), expected_draft_revision=payload.get("expected_draft_revision"),
                rebase_fields=payload.get("rebase_fields"), clear_fields=payload.get("clear_fields"),
                source_text=source_text, source_turn_id="form-" + uuid4().hex)
        except Exception as error:
            fail(error)

    @app.post("/api/drafts/{draft_id}/reject")
    def reject_draft(draft_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).reject_draft(user=actor, draft_id=draft_id)
        except Exception as error:
            fail(error)

    @app.post("/api/work-items/{task_id}/late-followup")
    def late_followup(task_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).create_late_reply_followup(user=actor, task_id=task_id, draft_ids=payload.get("draft_ids", []))
        except Exception as error:
            fail(error)

    @app.get("/api/rules")
    def rules(company_id: str, state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).list_rules(user=state[0], company_id=company_id)
        except Exception as error:
            fail(error)

    @app.post("/api/rules")
    def create_rule(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            company_id = payload.get("company_id", "")
            from .auth import require_role
            require_role(database, actor, company_id, {"accountant"})
            rule = payload.get("rule")
            if not isinstance(rule, dict):
                raise ValueError("rule 必须是结构化 JSON 对象。")
            rule["company_id"] = company_id
            staging = Path(app.state.enterprise_database).parent / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="enterprise-rule-", suffix=".json", dir=staging)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump([rule], stream, ensure_ascii=False)
                return RuleManager(database).import_file(path)
            finally:
                Path(path).unlink(missing_ok=True)
        except Exception as error:
            fail(error)

    @app.put("/api/rules/{rule_id}/{version}/status")
    def set_rule_status(rule_id: str, version: str, payload: dict = Body(...), company_id: str = Query(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            from .auth import require_role
            owned = database.connection.execute("SELECT company_id FROM rules WHERE rule_id=? AND version=?", (rule_id, version)).fetchone()
            if owned is None:
                raise ValueError("规则版本不存在。")
            actual_company = owned["company_id"]
            if actual_company is None:
                admin(actor)
            else:
                if actual_company != company_id:
                    raise PermissionError("规则所属企业与请求企业不一致。")
                require_role(database, actor, actual_company, {"accountant"})
            return RuleManager(database).set_status(rule_id=rule_id, version=version, status=payload.get("status", ""))
        except Exception as error:
            fail(error)

    @app.get("/api/preferences")
    def preference_list(company_id: str | None = None, actor=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return preferences(database).list(user_id=actor[0]["user_id"], company_id=company_id)
        except Exception as error:
            fail(error)

    @app.get("/api/preferences/catalog")
    def preference_catalog_route(state=Depends(get_actor)):
        from ..preferences import preference_catalog
        return {"items": preference_catalog()}

    @app.post("/api/preferences")
    def preference_create(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            if payload.get("company_id"):
                from .auth import require_role
                require_role(database, actor, payload["company_id"], {"accountant", "reviewer"})
            return preferences(database).create(user_id=actor["user_id"], key=payload["key"], value=payload["value"], scope=payload.get("scope", "user"), company_id=payload.get("company_id"))
        except Exception as error:
            fail(error)

    @app.put("/api/preferences/{preference_id}")
    def preference_update(preference_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return preferences(database).update(user_id=actor["user_id"], preference_id=preference_id, value=payload["value"], expected_revision=payload.get("expected_revision"))
        except Exception as error:
            fail(error)

    @app.delete("/api/preferences/{preference_id}")
    def preference_delete(preference_id: str, expected_revision: int | None = None, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return preferences(database).delete(user_id=actor["user_id"], preference_id=preference_id, expected_revision=expected_revision)
        except Exception as error:
            fail(error)

    @app.get("/api/diagnostics")
    def diagnostics(state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        user = state[0]
        admin(user)
        counts = {table: int(database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("companies", "enterprise_users", "enterprise_memberships", "invoices", "enterprise_work_items", "enterprise_import_conflicts", "enterprise_requests", "enterprise_drafts", "enterprise_reviews")}
        pending = database.connection.execute("SELECT status,COUNT(*) AS count FROM enterprise_work_items GROUP BY status").fetchall()
        budget_path = Path(os.getenv("TAXHARNESS_ENTERPRISE_BUDGET", str(Path(app.state.enterprise_database).parent / "budget.json")))
        budget = {"available": False}
        if budget_path.is_file():
            try:
                raw = json.loads(budget_path.read_text(encoding="utf-8"))
                budget = {key: raw.get(key) for key in ("status", "currency", "new_limit_cny", "model", "paid_calls_started")}
                budget["available"] = True
            except (OSError, json.JSONDecodeError):
                budget = {"available": False, "invalid": True}
        return {"status": "ok", "database_path": app.state.enterprise_database, "schema_version": database.get_meta("enterprise_schema_version"), "counts": counts, "work_item_statuses": [dict(row) for row in pending], "budget": budget, "live_provider_call": False}

    @app.post("/api/admin/backup")
    def backup(actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        admin(actor)
        import datetime
        from .backup import create_backup
        backup_dir = Path(app.state.enterprise_database).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            name = "enterprise-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8] + ".zip"
            return create_backup(database, database_path=app.state.enterprise_database, destination=backup_dir / name)
        except Exception as error:
            fail(error)

    from .delivery_http import install_delivery_routes
    from .extraction_http import install_extraction_routes
    install_delivery_routes(app, get_actor, get_mutating_actor, get_database, fail)
    install_extraction_routes(app, get_actor, get_mutating_actor, get_database, fail)
    return app


app = create_app()


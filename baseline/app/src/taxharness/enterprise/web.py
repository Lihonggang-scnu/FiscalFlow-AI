from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from httpx import HTTPError as HttpError

from ..errors import TaxHarnessError
from ..preferences import PreferenceManager, PreferenceStore
from ..provider import provider_config
from ..rule_manager import RuleManager
from .agent import generate_reply_drafts
from .auth import (
    SESSION_COOKIE, admin_count, create_first_admin, create_user, current_user, login,
    refresh_csrf, require_csrf, reset_password, revoke_session, set_session_cookie,
)
from .imports import ImportManager
from .service import EnterpriseService
from .storage import EnterpriseDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE = PROJECT_ROOT / "artifacts" / "enterprise_delivery_20261027" / "enterprise.sqlite3"
DEFAULT_LEDGER = PROJECT_ROOT / "artifacts" / "agent" / "agent.sqlite3"
INDEX_FILE = Path(__file__).resolve().parent / "static" / "index.html"


def create_app(*, database_path: str | Path | None = None, agent_store_path: str | Path | None = None, model=None, cookie_secure: bool | None = None) -> FastAPI:
    app = FastAPI(title="TaxAgentHarness Enterprise", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    app.state.enterprise_database = str(Path(database_path or os.getenv("TAXHARNESS_ENTERPRISE_DATABASE", str(DEFAULT_DATABASE))).expanduser().resolve())
    app.state.agent_store = str(Path(agent_store_path or os.getenv("TAXHARNESS_AGENT_STORE", str(DEFAULT_LEDGER))).expanduser().resolve())
    app.state.enterprise_model = model
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
        if isinstance(error, PermissionError):
            raise HTTPException(status_code=403, detail=str(error)) from error
        if isinstance(error, (ValueError, TaxHarnessError)):
            raise HTTPException(status_code=400, detail=str(error)) from error
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
    def session_route(state=Depends(get_actor), database: EnterpriseDatabase = Depends(get_database)):
        user, session = state
        csrf = refresh_csrf(database, session)
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
            rows = database.connection.execute("SELECT * FROM enterprise_import_conflicts WHERE company_id=? AND status='open' ORDER BY created_at", (company_id,)).fetchall()
            return {"items": [dict(row) for row in rows]}
        except Exception as error:
            fail(error)

    @app.post("/api/imports/conflicts/{conflict_id}/resolve")
    def conflict_resolve(conflict_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return ImportManager(database).resolve_conflict(user=actor, conflict_id=conflict_id, resolution=payload.get("resolution", ""))
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
            return EnterpriseService(database).review_invoice(user=actor, task_id=task_id, invoice_id=payload["invoice_id"], action=payload["action"], comment=payload.get("comment", ""))
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

    @app.post("/api/responses/{response_id}/drafts")
    def response_drafts(response_id: str, actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            live_enabled = os.getenv("TAXHARNESS_ENTERPRISE_LIVE_MODEL_ENABLED", "false").lower() == "true"
            return generate_reply_drafts(database, user=actor, response_id=response_id, model=app.state.enterprise_model, live_authorized=live_enabled, ledger_path=app.state.agent_store)
        except Exception as error:
            fail(error)

    @app.post("/api/drafts/confirm")
    def confirm_drafts(payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).confirm_drafts(user=actor, draft_ids=payload.get("draft_ids", []))
        except Exception as error:
            fail(error)

    @app.put("/api/drafts/{draft_id}")
    def update_draft(draft_id: str, payload: dict = Body(...), actor=Depends(get_mutating_actor), database: EnterpriseDatabase = Depends(get_database)):
        try:
            return EnterpriseService(database).update_draft(user=actor, draft_id=draft_id, invoice_id=payload.get("invoice_id"), purpose=payload.get("purpose"), remarks=payload.get("remarks"))
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
        backup_dir = Path(app.state.enterprise_database).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = "enterprise-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3"
        path = backup_dir / name
        destination = sqlite3.connect(str(path))
        try:
            database.connection.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            destination.close()
        if result != "ok":
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="备份完整性检查失败。")
        return {"status": "ok", "filename": name, "path": str(path), "integrity_check": result}

    return app


app = create_app()


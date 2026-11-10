from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request, Response

from .storage import EnterpriseDatabase, hash_token, issue_session, session_csrf, utc_now


SESSION_COOKIE = "tax_enterprise_session"
PASSWORD_N = 1 << 14
PASSWORD_R = 8
PASSWORD_P = 1


def _password_hash(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("密码长度必须为 12 到 1024 个字符。")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=PASSWORD_N, r=PASSWORD_R, p=PASSWORD_P, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(PASSWORD_N, PASSWORD_R, PASSWORD_P, base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(derived).decode())


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_text, expected_text = stored.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(expected_text.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def admin_count(database: EnterpriseDatabase) -> int:
    return int(database.connection.execute("SELECT COUNT(*) FROM enterprise_users WHERE is_admin=1 AND active=1").fetchone()[0])


def create_first_admin(database: EnterpriseDatabase, *, username: str, display_name: str, password: str) -> dict:
    if admin_count(database):
        raise ValueError("管理员已设置，不能重复执行首启设置。")
    username = username.strip().lower()
    if len(username) < 3 or len(username) > 100:
        raise ValueError("用户名长度必须为 3 到 100 个字符。")
    user_id = "usr-" + uuid4().hex
    now = utc_now()
    database.connection.execute("BEGIN IMMEDIATE")
    try:
        if admin_count(database):
            raise ValueError("管理员已设置，不能重复执行首启设置。")
        database.connection.execute(
            "INSERT INTO enterprise_users(user_id,username,display_name,password_hash,is_admin,active,created_at) VALUES(?,?,?,?,1,1,?)",
            (user_id, username, display_name.strip() or username, _password_hash(password), now),
        )
        database.connection.execute("INSERT INTO users(user_id,name) VALUES(?,?)", (user_id, display_name.strip() or username))
        database.connection.commit()
    except Exception:
        database.connection.rollback()
        raise
    return {"user_id": user_id, "username": username, "display_name": display_name.strip() or username, "is_admin": True}


def create_user(database: EnterpriseDatabase, *, username: str, display_name: str, password: str, is_admin: bool = False) -> dict:
    username = username.strip().lower()
    if len(username) < 3 or len(username) > 100:
        raise ValueError("用户名长度必须为 3 到 100 个字符。")
    user_id = "usr-" + uuid4().hex
    name = display_name.strip() or username
    now = utc_now()
    database.connection.execute(
        "INSERT INTO enterprise_users(user_id,username,display_name,password_hash,is_admin,active,created_at) VALUES(?,?,?,?,?,1,?)",
        (user_id, username, name, _password_hash(password), int(is_admin), now),
    )
    database.connection.execute("INSERT INTO users(user_id,name) VALUES(?,?)", (user_id, name))
    return {"user_id": user_id, "username": username, "display_name": name, "is_admin": bool(is_admin)}


def reset_password(database: EnterpriseDatabase, *, admin_user_id: str, target_user_id: str, password: str) -> dict:
    encoded = _password_hash(password)
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        admin = connection.execute("SELECT is_admin,active FROM enterprise_users WHERE user_id=?", (admin_user_id,)).fetchone()
        if admin is None or not admin["active"] or not admin["is_admin"]:
            raise PermissionError("只有当前启用的系统管理员能重置账号密码。")
        target = connection.execute("SELECT user_id,username FROM enterprise_users WHERE user_id=? AND active=1", (target_user_id,)).fetchone()
        if target is None:
            raise ValueError("目标账号不存在或已停用。")
        connection.execute("UPDATE enterprise_users SET password_hash=? WHERE user_id=?", (encoded, target_user_id))
        connection.execute("UPDATE enterprise_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (utc_now(), target_user_id))
        connection.commit()
        return {"status": "updated", "user_id": target_user_id, "username": target["username"], "sessions_revoked": True}
    except Exception:
        connection.rollback()
        raise


def login(database: EnterpriseDatabase, *, username: str, password: str) -> tuple[dict, str, str] | None:
    row = database.connection.execute(
        "SELECT * FROM enterprise_users WHERE username=? COLLATE NOCASE AND active=1", (username.strip(),)
    ).fetchone()
    if row is None or not _verify_password(password, row["password_hash"]):
        return None
    token, csrf = issue_session(database, row["user_id"])
    return ({"user_id": row["user_id"], "username": row["username"], "display_name": row["display_name"], "is_admin": bool(row["is_admin"])}, token, csrf)


def current_user(database: EnterpriseDatabase, request: Request) -> tuple[dict, sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="请先登录。")
    row = database.connection.execute(
        """SELECT s.*,u.username,u.display_name,u.is_admin,u.active
           FROM enterprise_sessions s JOIN enterprise_users u ON u.user_id=s.user_id
           WHERE s.token_hash=? AND s.revoked_at IS NULL""",
        (hash_token(token),),
    ).fetchone()
    if row is None or not row["active"] or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录。")
    user = {"user_id": row["user_id"], "username": row["username"], "display_name": row["display_name"], "is_admin": bool(row["is_admin"])}
    return user, row


def require_csrf(request: Request, session: sqlite3.Row) -> None:
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(hash_token(token), session["csrf_hash"]):
        raise HTTPException(status_code=403, detail="请求校验已过期，请刷新页面后重试。")


def set_session_cookie(response: Response, token: str, *, secure: bool = False) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite="strict", max_age=8 * 60 * 60, path="/")


def revoke_session(database: EnterpriseDatabase, request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        database.connection.execute("UPDATE enterprise_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (utc_now(), hash_token(token)))


def refresh_csrf(database: EnterpriseDatabase, session: sqlite3.Row, request: Request) -> str:
    csrf = session_csrf(request.cookies[SESSION_COOKIE])
    if not hmac.compare_digest(hash_token(csrf), session["csrf_hash"]):
        database.connection.execute("UPDATE enterprise_sessions SET csrf_hash=? WHERE token_hash=?", (hash_token(csrf), session["token_hash"]))
    return csrf


def assign_membership(database: EnterpriseDatabase, *, user_id: str, company_id: str, role: str, active: bool = True) -> None:
    if role not in {"accountant", "reviewer", "requester"}:
        raise ValueError("角色必须是 accountant、reviewer 或 requester。")
    if database.connection.execute("SELECT 1 FROM enterprise_users WHERE user_id=? AND active=1", (user_id,)).fetchone() is None:
        raise ValueError("用户不存在或已停用。")
    if database.connection.execute("SELECT 1 FROM companies WHERE company_id=?", (company_id,)).fetchone() is None:
        raise ValueError("企业不存在。")
    database.connection.execute(
        """INSERT INTO enterprise_memberships(user_id,company_id,role,active,assigned_at) VALUES(?,?,?,?,?)
           ON CONFLICT(user_id,company_id) DO UPDATE SET role=excluded.role,active=excluded.active,assigned_at=excluded.assigned_at""",
        (user_id, company_id, role, int(active), utc_now()),
    )
    can_view = int(active and role != "requester")
    can_modify = int(active and role == "accountant")
    database.connection.execute(
        """INSERT INTO user_company_permissions(user_id,company_id,can_view,can_modify) VALUES(?,?,?,?)
           ON CONFLICT(user_id,company_id) DO UPDATE SET can_view=excluded.can_view,can_modify=excluded.can_modify""",
        (user_id, company_id, can_view, can_modify),
    )


def membership(database: EnterpriseDatabase, user_id: str, company_id: str) -> dict | None:
    row = database.connection.execute(
        """SELECT m.role,m.active,u.is_admin,u.active AS user_active FROM enterprise_memberships m
           JOIN enterprise_users u ON u.user_id=m.user_id WHERE m.user_id=? AND m.company_id=?""",
        (user_id, company_id),
    ).fetchone()
    if row is not None and row["active"] and row["user_active"]:
        return {"role": row["role"], "is_admin": bool(row["is_admin"])}
    admin = database.connection.execute("SELECT is_admin,active FROM enterprise_users WHERE user_id=?", (user_id,)).fetchone()
    return {"role": "admin", "is_admin": True} if admin and admin["is_admin"] and admin["active"] else None


def require_role(database: EnterpriseDatabase, user: dict, company_id: str, allowed: set[str]) -> dict:
    value = membership(database, user["user_id"], company_id)
    if value is None:
        raise PermissionError("当前账号没有该企业的有效授权。")
    role = value["role"]
    if value["is_admin"]:
        return {**value, "role": "admin"}
    if role not in allowed:
        raise PermissionError("当前角色不能执行此操作。")
    return value


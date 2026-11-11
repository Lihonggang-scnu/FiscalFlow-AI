"""Durable originals and retryable multimodal processing for enterprise replies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..errors import PermissionDeniedError
from ..services import TaxService
from .auth import membership, require_role
from .multimodal import extract_document, prepare_document
from .storage import EnterpriseDatabase, utc_now


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_REPLY_FILES = 10
MAX_REPLY_BYTES = 64 * 1024 * 1024
METHOD_VERSION = "enterprise-multimodal-v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _filename(value: str) -> str:
    return Path(str(value).replace("\\", "/")).name[:200] or "上传文件"


def detect_media_type(filename: str, content: bytes) -> tuple[str, str, str]:
    """Validate upload size and identify its type from bytes, not the supplied MIME."""
    if not isinstance(content, bytes):
        content = bytes(content)
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("文件为空或超过 32 MiB。")
    if content.startswith(b"%PDF-"):
        media_type = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24 and content[12:16] == b"IHDR":
        media_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise ValueError("仅支持 PDF、PNG、JPEG、GIF 或 WebP 图片。")
    name = _filename(filename)
    return name, media_type, hashlib.sha256(content).hexdigest()


def _normalize_uploads(files: Iterable[tuple[str, bytes]]) -> list[dict[str, Any]]:
    normalized = []
    total_size = 0
    for filename, content in files:
        content = bytes(content)
        name, media_type, sha256 = detect_media_type(filename, content)
        total_size += len(content)
        normalized.append({"filename": name, "media_type": media_type, "sha256": sha256, "content": content})
        if len(normalized) > MAX_REPLY_FILES:
            raise ValueError(f"一次最多附加 {MAX_REPLY_FILES} 个文件。")
    if total_size > MAX_REPLY_BYTES:
        raise ValueError("一次回复的附件总大小不能超过 64 MiB。")
    return normalized


def _response_payload_hash(reply_text: str, files: list[dict[str, Any]]) -> str:
    payload = {"reply_text": reply_text, "files": [{"filename": item["filename"], "sha256": item["sha256"]} for item in files]}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _require_requester(database: EnterpriseDatabase, user: dict, request) -> None:
    if request is None or request["respondent_user_id"] != user["user_id"]:
        raise PermissionError("只能访问明确分配给当前账号的经办回复。")
    current = membership(database, user["user_id"], request["company_id"])
    if current is None or current["role"] != "requester":
        raise PermissionError("当前账号已不再拥有此请求的经办人授权。")


def _authorize_request(database: EnterpriseDatabase, user: dict, request, *, response_user_id: str | None = None) -> None:
    if request is None:
        raise PermissionError("请求不存在或当前账号无权访问。")
    if request["respondent_user_id"] == user["user_id"]:
        _require_requester(database, user, request)
        return
    if response_user_id == user["user_id"]:
        _require_requester(database, user, request)
        return
    require_role(database, user, request["company_id"], {"accountant", "reviewer"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=request["company_id"])


def _authorize_attachment(database: EnterpriseDatabase, user: dict, row) -> None:
    if row["source_kind"] == "reply":
        request = database.connection.execute("SELECT * FROM enterprise_requests WHERE request_id=?", (row["request_id"],)).fetchone()
        response = database.connection.execute("SELECT submitted_by FROM enterprise_responses WHERE response_id=?", (row["response_id"],)).fetchone()
        _authorize_request(database, user, request, response_user_id=response["submitted_by"] if response else None)
        return

    session = database.connection.execute("""SELECT user_id,company_id,task_id FROM enterprise_assistant_sessions
        WHERE session_id=?""", (row["session_id"],)).fetchone()
    if session is None or session["user_id"] != user["user_id"] or session["company_id"] != row["company_id"] or session["task_id"] != row["task_id"]:
        raise PermissionError("当前账号无权访问此会话附件。")
    require_role(database, user, row["company_id"], {"accountant", "reviewer"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=row["company_id"])


def _attachment_view(row, *, include_original: bool = False) -> dict[str, Any]:
    value = {key: row[key] for key in row.keys() if key != "original_content"}
    value["prepared"] = json.loads(value.pop("prepared_json") or "null")
    value["recognized"] = json.loads(value.pop("recognized_json") or "null")
    value["source_ref"] = {
        "attachment_id": value["attachment_id"],
        "source_revision": value["source_revision"],
        "sha256": value["sha256"],
        "media_type": value["media_type"],
        "page_count": value["page_count"],
        "processing_status": value["processing_status"],
        "result_revision": value["result_revision"],
    }
    if include_original:
        value["original_content"] = bytes(row["original_content"])
    return value


def _insert_original(database: EnterpriseDatabase, *, source_kind: str, source_id: str, response_id: str | None,
                     request_id: str | None, session_id: str | None, task_id: str | None, company_id: str,
                     user_id: str, upload_key: str, filename: str, media_type: str, sha256: str, content: bytes) -> dict[str, Any]:
    existing = database.connection.execute("""SELECT * FROM enterprise_original_attachments
        WHERE source_kind=? AND source_id=? AND upload_key=?""", (source_kind, source_id, upload_key)).fetchone()
    if existing is not None:
        if existing["user_id"] != user_id or existing["sha256"] != sha256 or existing["filename"] != filename:
            raise ValueError("同一附件重试标识已用于不同文件。")
        return {**_attachment_view(existing), "duplicate": True}
    now = utc_now()
    attachment_id = _id("att-")
    database.connection.execute("""INSERT INTO enterprise_original_attachments
        (attachment_id,source_kind,source_id,response_id,request_id,session_id,task_id,company_id,user_id,upload_key,
         filename,media_type,sha256,size,original_content,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (attachment_id, source_kind, source_id, response_id, request_id, session_id, task_id, company_id, user_id,
         upload_key, filename, media_type, sha256, len(content), content, now, now))
    row = database.connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
    return {**_attachment_view(row), "duplicate": False}


def submit_reply_with_attachments(database: EnterpriseDatabase, *, user: dict, request_id: str,
                                  reply_text: str, files: Iterable[tuple[str, bytes]],
                                  idempotency_key: str) -> dict[str, Any]:
    """Atomically save a handler's text, reply and original files before parsing."""
    reply_text = str(reply_text or "").strip()
    uploads = _normalize_uploads(files)
    if not reply_text and not uploads:
        raise ValueError("请填写回复内容或附加资料。")
    idempotency_key = str(idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValueError("请提供长度不超过 200 个字符的上传重试标识。")
    payload_hash = _response_payload_hash(reply_text, uploads)
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        request = connection.execute("SELECT * FROM enterprise_requests WHERE request_id=?", (request_id,)).fetchone()
        _require_requester(database, user, request)
        previous = connection.execute("""SELECT * FROM enterprise_responses
            WHERE request_id=? AND submitted_by=? AND idempotency_key=?""",
            (request_id, user["user_id"], idempotency_key)).fetchone()
        if previous is not None:
            if previous["payload_hash"] != payload_hash:
                raise ValueError("同一上传重试标识对应的回复内容已变化，请使用新的标识。")
            rows = connection.execute("""SELECT * FROM enterprise_original_attachments
                WHERE source_kind='reply' AND source_id=? ORDER BY upload_key""", (previous["response_id"],)).fetchall()
            connection.commit()
            return {"response_id": previous["response_id"], "request_id": request_id, "status": "saved",
                    "duplicate": True, "attachments": [_attachment_view(row) for row in rows]}
        if request["status"] == "cancelled":
            raise ValueError("该请求已取消。")
        task = connection.execute("SELECT status FROM enterprise_work_items WHERE task_id=?", (request["task_id"],)).fetchone()
        late = bool(task and task["status"] == "closed")
        response_id = _id("reply-")
        connection.execute("""INSERT INTO enterprise_responses
            (response_id,request_id,submitted_by,reply_text,idempotency_key,payload_hash,late,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (response_id, request_id, user["user_id"], reply_text, idempotency_key, payload_hash, int(late), utc_now()))
        connection.execute("UPDATE enterprise_requests SET status='replied' WHERE request_id=? AND status<>'cancelled'", (request_id,))
        if not late:
            for invoice_id in json.loads(request["target_invoice_ids_json"]):
                connection.execute("""UPDATE enterprise_task_invoices SET status='pending',updated_at=?
                    WHERE task_id=? AND invoice_id=? AND status='awaiting_info'""",
                    (utc_now(), request["task_id"], invoice_id))
        attachments = []
        for index, item in enumerate(uploads):
            attachments.append(_insert_original(database, source_kind="reply", source_id=response_id,
                response_id=response_id, request_id=request_id, session_id=None, task_id=request["task_id"],
                company_id=request["company_id"], user_id=user["user_id"], upload_key=f"{idempotency_key}:{index}",
                filename=item["filename"], media_type=item["media_type"], sha256=item["sha256"], content=item["content"]))
        connection.commit()
        return {"response_id": response_id, "request_id": request_id, "late": late, "status": "saved",
                "duplicate": False, "attachments": attachments}
    except Exception:
        connection.rollback()
        raise


def persist_original_attachment(database: EnterpriseDatabase, *, user: dict, session_id: str, task_id: str,
                                source_id: str, upload_key: str, filename: str, content: bytes) -> dict[str, Any]:
    """Persist an assistant-turn original before parsing; upload_key makes retries idempotent."""
    name, media_type, sha256 = detect_media_type(filename, content)
    if not upload_key or len(upload_key) > 200:
        raise ValueError("请提供长度不超过 200 个字符的上传重试标识。")
    session = database.connection.execute("""SELECT user_id,company_id,task_id FROM enterprise_assistant_sessions
        WHERE session_id=?""", (session_id,)).fetchone()
    if session is None or session["user_id"] != user["user_id"] or session["task_id"] != task_id:
        raise PermissionError("当前账号无权向此会话添加附件。")
    require_role(database, user, session["company_id"], {"accountant", "reviewer"})
    TaxService(database).require_access(user_id=user["user_id"], company_id=session["company_id"])
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Recheck the session and role after opening the write transaction.
        session = connection.execute("""SELECT user_id,company_id,task_id FROM enterprise_assistant_sessions
            WHERE session_id=?""", (session_id,)).fetchone()
        if session is None or session["user_id"] != user["user_id"] or session["task_id"] != task_id:
            raise PermissionError("当前账号无权向此会话添加附件。")
        require_role(database, user, session["company_id"], {"accountant", "reviewer"})
        TaxService(database).require_access(user_id=user["user_id"], company_id=session["company_id"])
        result = _insert_original(database, source_kind="assistant_turn", source_id=source_id, response_id=None,
            request_id=None, session_id=session_id, task_id=task_id, company_id=session["company_id"],
            user_id=user["user_id"], upload_key=upload_key, filename=name, media_type=media_type,
            sha256=sha256, content=bytes(content))
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def get_attachment(database: EnterpriseDatabase, *, user: dict, attachment_id: str,
                   include_original: bool = False) -> dict[str, Any]:
    row = database.connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
    if row is None:
        raise ValueError("附件不存在。")
    _authorize_attachment(database, user, row)
    return _attachment_view(row, include_original=include_original)


def list_reply_attachments(database: EnterpriseDatabase, *, user: dict, response_id: str) -> list[dict[str, Any]]:
    response = database.connection.execute("""SELECT r.submitted_by,q.* FROM enterprise_responses r
        JOIN enterprise_requests q USING(request_id) WHERE r.response_id=?""", (response_id,)).fetchone()
    if response is None:
        raise ValueError("回复不存在。")
    _authorize_request(database, user, response, response_user_id=response["submitted_by"])
    rows = database.connection.execute("""SELECT * FROM enterprise_original_attachments
        WHERE source_kind='reply' AND source_id=? ORDER BY upload_key""", (response_id,)).fetchall()
    return [_attachment_view(row) for row in rows]


def _reply_text(database: EnterpriseDatabase, row) -> str:
    if row["response_id"]:
        response = database.connection.execute("SELECT reply_text FROM enterprise_responses WHERE response_id=?", (row["response_id"],)).fetchone()
        return response["reply_text"] if response else ""
    return ""


def _prepared_metadata(document: dict[str, Any]) -> dict[str, Any]:
    pages = document.get("pages") or [{"page": index, "text": ""} for index in range(1, document["page_count"] + 1)]
    return {"filename": document["filename"], "mime": document["mime"], "sha256": document["sha256"],
            "size": document["size"], "page_count": document["page_count"], "text": document.get("text", ""),
            "pages": pages}


def _run_valid(database: EnterpriseDatabase, run_guard: dict | None) -> bool:
    if not run_guard:
        return True
    run = database.connection.execute("SELECT * FROM enterprise_extraction_runs WHERE run_id=?", (run_guard["run_id"],)).fetchone()
    if (run is None or run["state"] != "running" or run["cancel_requested"] or
            run["generation"] != run_guard["generation"] or not run["lease_until"] or run["lease_until"] <= utc_now()):
        return False
    link = database.connection.execute("SELECT mandate_id FROM enterprise_mandate_extractions WHERE run_id=?", (run_guard["run_id"],)).fetchone()
    if link:
        mandate = database.connection.execute("SELECT status,valid_until FROM enterprise_work_mandates WHERE mandate_id=?", (link["mandate_id"],)).fetchone()
        if mandate is None or mandate["status"] != "active" or mandate["valid_until"] <= utc_now():
            return False
    return True


def _write_processing_result(database: EnterpriseDatabase, *, user: dict, attachment_id: str, generation: int,
                             status: str, prepared: dict[str, Any] | None = None,
                             recognized: dict[str, Any] | None = None, page_count: int | None = None,
                             error_code: str | None = None, error_message: str | None = None,
                             run_guard: dict | None = None) -> bool:
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute("""SELECT * FROM enterprise_original_attachments
            WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
            (attachment_id, generation)).fetchone()
        if current is None:
            connection.rollback()
            return False
        if not _run_valid(database, run_guard):
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
                processing_generation=processing_generation+1,last_error_code='run_cancelled',updated_at=? WHERE attachment_id=?""", (utc_now(), attachment_id))
            connection.commit()
            return False
        try:
            _authorize_attachment(database, user, current)
        except (PermissionError, PermissionDeniedError):
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status='cancelled',processing_run_id=NULL,
                processing_generation=processing_generation+1,last_error_code='permission_revoked',
                last_error_message='处理权限已撤回。',updated_at=? WHERE attachment_id=?""", (utc_now(), attachment_id))
            connection.commit()
            return False
        if status == "complete":
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status='complete',processing_run_id=NULL,
                prepared_json=?,recognized_json=?,page_count=?,result_revision=result_revision+1,
                last_error_code=NULL,last_error_message=NULL,processed_at=?,updated_at=?
                WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
                (_json(prepared), _json(recognized), page_count, utc_now(), utc_now(), attachment_id, generation))
        elif prepared is not None:
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status=?,processing_run_id=NULL,prepared_json=?,
                page_count=?,last_error_code=?,last_error_message=?,processed_at=?,updated_at=?
                WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
                (status, _json(prepared), page_count, error_code, error_message, utc_now(), utc_now(), attachment_id, generation))
        else:
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status=?,processing_run_id=NULL,
                last_error_code=?,last_error_message=?,processed_at=?,updated_at=?
                WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
                (status, error_code, error_message, utc_now(), utc_now(), attachment_id, generation))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def process_attachment(database: EnterpriseDatabase, *, user: dict, attachment_id: str,
                       user_text: str | None = None, model=None, live_authorized: bool = False,
                       budget_ledger_path: str | Path | None = None, budget_usd: float | None = None,
                       session_id: str | None = None, turn_id: str | None = None,
                       run_guard: dict | None = None) -> dict[str, Any]:
    """Claim, parse and recognize outside write transactions; stale generations cannot overwrite newer work."""
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ValueError("附件不存在。")
        _authorize_attachment(database, user, row)
        if not _run_valid(database, run_guard):
            raise PermissionError("整理任务已停止或授权失效。")
        if row["processing_status"] == "complete":
            connection.commit()
            return _attachment_view(row)
        if row["processing_status"] == "processing":
            raise ValueError("附件正在处理中。")
        ledger = Path(budget_ledger_path) if budget_ledger_path else None
        if model is None and live_authorized and ledger and ledger.exists():
            with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as accounting:
                has_calls = accounting.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_model_calls'").fetchone()
                unresolved = accounting.execute("""SELECT 1 FROM agent_model_calls WHERE status IN ('pending','unknown')
                    AND (turn_id LIKE ? OR turn_id LIKE ?) LIMIT 1""",
                    (f"attachment-{attachment_id}-g%", f"%:attachment:{attachment_id}")).fetchone() if has_calls else None
            if unresolved:
                raise RuntimeError("此前的附件识别费用尚未确认，请核对后再重试。")
        generation = int(row["processing_generation"]) + 1
        connection.execute("""UPDATE enterprise_original_attachments SET processing_status='processing',processing_run_id=?,
            processing_generation=?,attempt_count=attempt_count+1,last_error_code=NULL,last_error_message=NULL,
            started_at=?,processed_at=NULL,updated_at=? WHERE attachment_id=?""",
            ((run_guard or {}).get("run_id"), generation, utc_now(), utc_now(), attachment_id))
        original = bytes(row["original_content"])
        response_id = row["response_id"]
        source_id = row["source_id"]
        response_text = _reply_text(database, row) if user_text is None else str(user_text)
        actual_session_id = session_id or row["session_id"] or response_id or source_id
        actual_turn_id = turn_id or f"attachment-{attachment_id}-g{generation}"
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    prepared_document = None
    stage = "prepare"
    try:
        prepared_document = prepare_document(row["filename"], original)
        prepared = _prepared_metadata(prepared_document)
        # Save parsed pages before the model call, so text remains available if recognition is disabled or fails.
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute("""SELECT * FROM enterprise_original_attachments
                WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
                (attachment_id, generation)).fetchone()
            if current is None:
                connection.rollback()
                return get_attachment(database, user=user, attachment_id=attachment_id)
            _authorize_attachment(database, user, current)
            if not _run_valid(database, run_guard):
                connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
                    processing_generation=processing_generation+1,last_error_code='run_cancelled',updated_at=? WHERE attachment_id=?""", (utc_now(), attachment_id))
                connection.commit()
                return get_attachment(database, user=user, attachment_id=attachment_id)
            connection.execute("""UPDATE enterprise_original_attachments SET prepared_json=?,page_count=?,updated_at=?
                WHERE attachment_id=? AND processing_generation=? AND processing_status='processing'""",
                (_json(prepared), prepared["page_count"], utc_now(), attachment_id, generation))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        stage = "recognition"
        if model is None and not live_authorized:
            raise RuntimeError("model_disabled")
        recognized = extract_document(prepared_document, user_text=response_text, user_id=user["user_id"],
            session_id=actual_session_id, turn_id=actual_turn_id, budget_ledger_path=budget_ledger_path,
            budget_usd=budget_usd, model=model)
        recognized = {"method_version": METHOD_VERSION, **recognized}
        if not _write_processing_result(database, user=user, attachment_id=attachment_id, generation=generation,
                status="complete", prepared=prepared, recognized=recognized, page_count=prepared["page_count"], run_guard=run_guard):
            return get_attachment(database, user=user, attachment_id=attachment_id)
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) == "model_disabled":
            error_code, message = "model_disabled", "多模态模型当前未开启；原件和已提取的分页文本已保留，可稍后重试。"
        elif isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold():
            error_code, message = "processing_timeout", "文件处理或模型调用超时，可稍后重试。"
        elif stage == "prepare":
            error_code, message = "invalid_document", str(error)
        elif isinstance(error, ValueError):
            error_code, message = "recognition_failed", str(error)
        else:
            error_code, message = "recognition_failed", type(error).__name__
        _write_processing_result(database, user=user, attachment_id=attachment_id, generation=generation,
            status="failed", prepared=_prepared_metadata(prepared_document) if prepared_document else None,
            page_count=prepared_document["page_count"] if prepared_document else None,
            error_code=error_code, error_message=message[:500], run_guard=run_guard)
    return get_attachment(database, user=user, attachment_id=attachment_id)


def cancel_attachment_processing(database: EnterpriseDatabase, *, user: dict, attachment_id: str) -> dict[str, Any]:
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ValueError("附件不存在。")
        _authorize_attachment(database, user, row)
        if row["processing_status"] in {"complete", "failed", "cancelled"}:
            connection.commit()
            return _attachment_view(row)
        generation = int(row["processing_generation"]) + 1
        connection.execute("""UPDATE enterprise_original_attachments SET processing_status='cancelled',processing_run_id=NULL,
            processing_generation=?,last_error_code='cancelled',last_error_message='处理已取消。',processed_at=?,updated_at=?
            WHERE attachment_id=?""", (generation, utc_now(), utc_now(), attachment_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return get_attachment(database, user=user, attachment_id=attachment_id)


def retry_attachment(database: EnterpriseDatabase, *, user: dict, attachment_id: str, **options) -> dict[str, Any]:
    """Retry a failed, cancelled or pending extraction on the same immutable original."""
    return process_attachment(database, user=user, attachment_id=attachment_id, **options)


def reset_attachment_for_retry(database: EnterpriseDatabase, *, user: dict, attachment_id: str) -> dict[str, Any]:
    connection = database.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        if row is None:
            raise ValueError("附件不存在。")
        _authorize_attachment(database, user, row)
        if row["processing_status"] == "processing":
            raise ValueError("附件正在处理中，请先停止当前整理。")
        if row["processing_status"] in {"failed", "cancelled"}:
            connection.execute("""UPDATE enterprise_original_attachments SET processing_status='pending',processing_run_id=NULL,
                processing_generation=processing_generation+1,last_error_code=NULL,last_error_message=NULL,updated_at=?
                WHERE attachment_id=?""", (utc_now(), attachment_id))
        updated = connection.execute("SELECT * FROM enterprise_original_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        connection.commit()
        return _attachment_view(updated)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def attachment_source(database: EnterpriseDatabase, *, user: dict, attachment_id: str) -> dict[str, Any]:
    """Return the source locator, per-page text, result candidates and current processing state."""
    return get_attachment(database, user=user, attachment_id=attachment_id)

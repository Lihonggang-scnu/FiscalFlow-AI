"""Keep uploaded conversation documents and their extracted observations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class ConversationAttachments:
    def __init__(self, agent_store: str | Path):
        self.path = Path(agent_store).with_name("chainlit-attachments.sqlite3")
        self.root = self.path.parent / "chainlit-attachments"

    def add(self, *, session_id: str, user_id: str, company_id: str, task_id: str | None,
            document: dict, extracted: dict, content: bytes) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        attachment_id = "attachment-" + uuid4().hex
        suffix = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
                  "image/gif": ".gif", "image/webp": ".webp"}[document["mime"]]
        stored_name = attachment_id + suffix
        (self.root / stored_name).write_bytes(content)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_attachments (
                attachment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
                company_id TEXT NOT NULL, task_id TEXT, turn_id TEXT, filename TEXT NOT NULL,
                mime TEXT NOT NULL, stored_name TEXT NOT NULL, extracted_json TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            db.execute("INSERT INTO conversation_attachments VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                attachment_id, session_id, user_id, company_id, task_id, None, document["filename"],
                document["mime"], stored_name, json.dumps(extracted, ensure_ascii=False), datetime.now(UTC).isoformat()))
        return attachment_id

    def attach_turn(self, attachment_ids: list[str], turn_id: str, task_id: str | None) -> None:
        if not attachment_ids:
            return
        with sqlite3.connect(self.path) as db:
            db.executemany("UPDATE conversation_attachments SET turn_id=?,task_id=COALESCE(task_id,?) WHERE attachment_id=?",
                           [(turn_id, task_id, attachment_id) for attachment_id in attachment_ids])

    def observations(self, *, session_id: str, user_id: str, company_id: str, task_id: str | None) -> list[dict]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT attachment_id,extracted_json FROM conversation_attachments
                WHERE user_id=? AND company_id=? AND (
                  (session_id=? AND (task_id IS NULL OR task_id=? OR ? IS NULL)) OR
                  (task_id IS NOT NULL AND task_id=?))
                ORDER BY created_at DESC,rowid DESC LIMIT 5""", (user_id, company_id, session_id, task_id, task_id, task_id)).fetchall()
        result = []
        for row in reversed(rows):
            value = json.loads(row["extracted_json"])
            text = value.get("pdf_text", "")
            value["pdf_text"] = text[:12000]
            value["text_truncated"] = len(text) > 12000
            result.append({"attachment_id": row["attachment_id"], **value})
        return result

    def history(self, *, session_id: str, user_id: str) -> dict[str, list[dict]]:
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT turn_id,filename,mime,stored_name FROM conversation_attachments
                WHERE session_id=? AND user_id=? AND turn_id IS NOT NULL ORDER BY created_at,rowid""",
                (session_id, user_id)).fetchall()
        result: dict[str, list[dict]] = {}
        for row in rows:
            file_path = self.root / row["stored_name"]
            if file_path.is_file():
                result.setdefault(row["turn_id"], []).append({"filename": row["filename"], "mime": row["mime"], "path": file_path})
        return result

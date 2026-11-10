from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..storage import TaxDatabase


CURRENCY_DIGITS = {code: 2 for code in ("CNY", "GBP", "USD", "EUR", "CHF", "CAD", "AUD", "NZD", "HKD", "SGD")}


ENTERPRISE_SCHEMA = """
CREATE TABLE IF NOT EXISTS enterprise_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(user_id) REFERENCES enterprise_users(user_id)
);
CREATE TABLE IF NOT EXISTS enterprise_assistant_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS enterprise_assistant_session_owner ON enterprise_assistant_sessions(user_id,task_id,updated_at);
CREATE TABLE IF NOT EXISTS enterprise_assistant_attachments (
    attachment_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    turn_id TEXT,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    extracted_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS enterprise_assistant_attachment_session ON enterprise_assistant_attachments(session_id,user_id,created_at);
CREATE TABLE IF NOT EXISTS enterprise_memberships (
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('accountant','reviewer','requester')),
    active INTEGER NOT NULL DEFAULT 1,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id,company_id),
    FOREIGN KEY(user_id) REFERENCES enterprise_users(user_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS enterprise_company_settings (
    company_id TEXT PRIMARY KEY,
    default_currency TEXT NOT NULL DEFAULT 'CNY',
    default_purpose_policy TEXT NOT NULL DEFAULT 'all_blank' CHECK(default_purpose_policy IN ('all_blank','none')),
    require_independent_review INTEGER NOT NULL DEFAULT 1,
    allowed_dispositions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS enterprise_invoice_links (
    company_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_invoice_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL UNIQUE,
    source_hash TEXT NOT NULL,
    source_data_json TEXT NOT NULL,
    field_owners_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(company_id,source_system,source_invoice_id),
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id)
);
CREATE TABLE IF NOT EXISTS enterprise_document_identities (
    company_id TEXT NOT NULL,
    invoice_code TEXT NOT NULL DEFAULT '',
    invoice_number TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    seller_tax_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(company_id,invoice_code,invoice_number),
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id)
);
CREATE TABLE IF NOT EXISTS enterprise_invoice_extra (
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_status TEXT NOT NULL DEFAULT 'active',
    amount_text TEXT NOT NULL DEFAULT '0.00',
    unit_price_text TEXT,
    quantity_text TEXT,
    purpose_review_status TEXT NOT NULL DEFAULT 'pending',
    disposition TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(company_id,invoice_id),
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id)
);
CREATE TABLE IF NOT EXISTS enterprise_import_previews (
    preview_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_imports (
    import_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_import_rows (
    import_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    source_system TEXT NOT NULL,
    source_invoice_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT,
    content_hash TEXT NOT NULL,
    source_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(import_id,row_number)
);
CREATE TABLE IF NOT EXISTS enterprise_import_issues (
    issue_id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    source_invoice_id TEXT,
    issue_code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_import_conflicts (
    conflict_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    current_value TEXT,
    prior_source_value TEXT,
    incoming_source_value TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_by TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS enterprise_work_items (
    task_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    period TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'purpose_review',
    status TEXT NOT NULL CHECK(status IN ('open','in_progress','needs_review','returned','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    assigned_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS enterprise_work_list ON enterprise_work_items(company_id,period,status,updated_at);
CREATE TABLE IF NOT EXISTS enterprise_task_invoices (
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','awaiting_info','awaiting_review','returned','approved','waived')),
    origin TEXT NOT NULL DEFAULT 'explicit' CHECK(origin IN ('explicit','rule_missing','source_change')),
    disposition TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id,invoice_id),
    FOREIGN KEY(task_id) REFERENCES enterprise_work_items(task_id),
    FOREIGN KEY(company_id,invoice_id) REFERENCES invoices(company_id,invoice_id)
);
CREATE TABLE IF NOT EXISTS enterprise_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    respondent_user_id TEXT NOT NULL,
    target_invoice_ids_json TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','replied','cancelled')),
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    FOREIGN KEY(task_id) REFERENCES enterprise_work_items(task_id)
);
CREATE INDEX IF NOT EXISTS enterprise_request_owner ON enterprise_requests(respondent_user_id,status,created_at);
CREATE TABLE IF NOT EXISTS enterprise_responses (
    response_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    late INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'pending' CHECK(extraction_status IN ('pending','complete','failed')),
    extraction_outcome TEXT,
    processed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES enterprise_requests(request_id)
);
CREATE TABLE IF NOT EXISTS enterprise_drafts (
    draft_id TEXT PRIMARY KEY,
    response_id TEXT,
    request_id TEXT,
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT,
    candidate_invoice_ids_json TEXT NOT NULL DEFAULT '[]',
    proposed_purpose TEXT,
    proposed_remarks TEXT,
    evidence_quote TEXT NOT NULL DEFAULT '',
    needs_clarification INTEGER NOT NULL DEFAULT 0,
    unavailable INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    clarification_question TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','clarification','confirmed','conflict','rejected')),
    source_revision INTEGER,
    draft_revision INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'request',
    source_turn_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(response_id) REFERENCES enterprise_responses(response_id),
    FOREIGN KEY(request_id) REFERENCES enterprise_requests(request_id),
    FOREIGN KEY(task_id) REFERENCES enterprise_work_items(task_id)
);
CREATE INDEX IF NOT EXISTS enterprise_drafts_task ON enterprise_drafts(task_id,status,created_at);
CREATE TABLE IF NOT EXISTS enterprise_reviews (
    review_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    draft_id TEXT,
    reviewer_user_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('approve','return','waive')),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES enterprise_work_items(task_id)
);
CREATE TABLE IF NOT EXISTS enterprise_confirmation_receipts (
    draft_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    confirmed_by TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise_task_invoice_snapshots (
    task_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_invoice_id TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    seller_name TEXT NOT NULL,
    project_name TEXT NOT NULL,
    amount_text TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_status TEXT NOT NULL DEFAULT 'active',
    purpose TEXT,
    remarks TEXT,
    revision INTEGER NOT NULL,
    review_status TEXT NOT NULL,
    disposition TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(task_id,invoice_id)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EnterpriseDatabase(TaxDatabase):
    """The competition schema plus additive enterprise tables in a separate database."""

    def __init__(self, path: str | Path):
        # FastAPI's synchronous dependency and route can run on different pool threads
        # within one request; each request still owns its own SQLite connection.
        super().__init__(path, check_same_thread=False)
        self.connection.executescript(ENTERPRISE_SCHEMA)
        extra_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_invoice_extra)")}
        if "amount_text" not in extra_columns:
            self.connection.execute("ALTER TABLE enterprise_invoice_extra ADD COLUMN amount_text TEXT NOT NULL DEFAULT '0.00'")
        if "source_status" not in extra_columns:
            self.connection.execute("ALTER TABLE enterprise_invoice_extra ADD COLUMN source_status TEXT NOT NULL DEFAULT 'active'")
        receipt_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_confirmation_receipts)")}
        if "confirmed_by" not in receipt_columns:
            self.connection.execute("ALTER TABLE enterprise_confirmation_receipts ADD COLUMN confirmed_by TEXT NOT NULL DEFAULT ''")
        draft_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_drafts)")}
        if "unavailable" not in draft_columns:
            self.connection.execute("ALTER TABLE enterprise_drafts ADD COLUMN unavailable INTEGER NOT NULL DEFAULT 0")
        if "superseded_by" not in draft_columns:
            self.connection.execute("ALTER TABLE enterprise_drafts ADD COLUMN superseded_by TEXT")
        if "draft_revision" not in draft_columns:
            self.connection.execute("ALTER TABLE enterprise_drafts ADD COLUMN draft_revision INTEGER NOT NULL DEFAULT 1")
        if "origin" not in draft_columns:
            self.connection.execute("ALTER TABLE enterprise_drafts ADD COLUMN origin TEXT NOT NULL DEFAULT 'request'")
        if "source_turn_id" not in draft_columns:
            self.connection.execute("ALTER TABLE enterprise_drafts ADD COLUMN source_turn_id TEXT")
        draft_info = {row[1]: row for row in self.connection.execute("PRAGMA table_info(enterprise_drafts)")}
        if draft_info["response_id"][3] or draft_info["request_id"][3]:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                draft_info = {row[1]: row for row in self.connection.execute("PRAGMA table_info(enterprise_drafts)")}
                if draft_info["response_id"][3] or draft_info["request_id"][3]:
                    self.connection.execute("""CREATE TABLE enterprise_drafts_new (
                        draft_id TEXT PRIMARY KEY,response_id TEXT,request_id TEXT,task_id TEXT NOT NULL,company_id TEXT NOT NULL,
                        invoice_id TEXT,candidate_invoice_ids_json TEXT NOT NULL DEFAULT '[]',proposed_purpose TEXT,proposed_remarks TEXT,
                        evidence_quote TEXT NOT NULL DEFAULT '',needs_clarification INTEGER NOT NULL DEFAULT 0,unavailable INTEGER NOT NULL DEFAULT 0,
                        superseded_by TEXT,clarification_question TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','clarification','confirmed','conflict','rejected')),
                        source_revision INTEGER,draft_revision INTEGER NOT NULL DEFAULT 1,origin TEXT NOT NULL DEFAULT 'request',source_turn_id TEXT,
                        created_by TEXT NOT NULL,created_at TEXT NOT NULL,
                        FOREIGN KEY(response_id) REFERENCES enterprise_responses(response_id),FOREIGN KEY(request_id) REFERENCES enterprise_requests(request_id),
                        FOREIGN KEY(task_id) REFERENCES enterprise_work_items(task_id))""")
                    fields = "draft_id,response_id,request_id,task_id,company_id,invoice_id,candidate_invoice_ids_json,proposed_purpose,proposed_remarks,evidence_quote,needs_clarification,unavailable,superseded_by,clarification_question,status,source_revision,draft_revision,origin,source_turn_id,created_by,created_at"
                    self.connection.execute(f"INSERT INTO enterprise_drafts_new({fields}) SELECT {fields} FROM enterprise_drafts")
                    self.connection.execute("DROP TABLE enterprise_drafts")
                    self.connection.execute("ALTER TABLE enterprise_drafts_new RENAME TO enterprise_drafts")
                    self.connection.execute("CREATE INDEX enterprise_drafts_task ON enterprise_drafts(task_id,status,created_at)")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        response_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_responses)")}
        if "extraction_status" not in response_columns:
            self.connection.execute("ALTER TABLE enterprise_responses ADD COLUMN extraction_status TEXT NOT NULL DEFAULT 'pending'")
        if "extraction_outcome" not in response_columns:
            self.connection.execute("ALTER TABLE enterprise_responses ADD COLUMN extraction_outcome TEXT")
        if "processed_at" not in response_columns:
            self.connection.execute("ALTER TABLE enterprise_responses ADD COLUMN processed_at TEXT")
        task_invoice_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_task_invoices)")}
        if "origin" not in task_invoice_columns:
            self.connection.execute("ALTER TABLE enterprise_task_invoices ADD COLUMN origin TEXT NOT NULL DEFAULT 'rule_missing'")
        snapshot_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_task_invoice_snapshots)")}
        if "source_status" not in snapshot_columns:
            self.connection.execute("ALTER TABLE enterprise_task_invoice_snapshots ADD COLUMN source_status TEXT NOT NULL DEFAULT 'active'")
        settings_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(enterprise_company_settings)")}
        if "default_purpose_policy" not in settings_columns:
            self.connection.execute("ALTER TABLE enterprise_company_settings ADD COLUMN default_purpose_policy TEXT NOT NULL DEFAULT 'all_blank'")
        self.connection.execute("INSERT INTO meta(key,value) VALUES('enterprise_schema_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(database: EnterpriseDatabase, user_id: str, *, hours: int = 8) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    database.connection.execute(
        "INSERT INTO enterprise_sessions(token_hash,user_id,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
        (hash_token(token), user_id, hash_token(csrf), now.isoformat(), (now + timedelta(hours=hours)).isoformat()),
    )
    return token, csrf


"""Freeze approved work into a versioned, partly deliverable package."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from typing import Any
from uuid import uuid4

from ..services import TaxService
from .service import EnterpriseService, _REVIEW_FIELDS, _review_snapshot
from .storage import EnterpriseDatabase, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _csv(rows: list[dict], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cleaned = {}
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            cleaned[key] = value
        writer.writerow(cleaned)
    return output.getvalue().encode("utf-8-sig")


class DeliveryService:
    def __init__(self, database: EnterpriseDatabase):
        self.database = database
        self.service = EnterpriseService(database)
        database.connection.execute("""CREATE TABLE IF NOT EXISTS enterprise_deliveries (
            delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, company_id TEXT NOT NULL, version INTEGER NOT NULL,
            created_by TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
            archive_blob BLOB, archive_sha256 TEXT, status TEXT NOT NULL DEFAULT 'generated', created_at TEXT NOT NULL,
            UNIQUE(task_id,version), UNIQUE(created_by,company_id,request_key))""")
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(enterprise_deliveries)")}
        if "archive_blob" not in columns:
            database.connection.execute("ALTER TABLE enterprise_deliveries ADD COLUMN archive_blob BLOB")
        if "archive_sha256" not in columns:
            database.connection.execute("ALTER TABLE enterprise_deliveries ADD COLUMN archive_sha256 TEXT")

    def _task(self, user: dict, task_id: str, *, create: bool = False):
        task = self.database.connection.execute("SELECT * FROM enterprise_work_items WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("工作项不存在。")
        self.service._role(user, task["company_id"], {"accountant"} if create else {"accountant", "reviewer"})
        TaxService(self.database).require_access(user_id=user["user_id"], company_id=task["company_id"], modify=create)
        return task

    def preview(self, *, user: dict, task_id: str, invoice_ids: list[str] | None = None) -> dict:
        connection = self.database.connection
        task = self._task(user, task_id)
        rows = connection.execute("""SELECT i.invoice_id,i.issue_date,i.seller_name,i.project_name,i.purpose,i.remarks,i.revision,
            x.amount_text,x.currency,x.source_status,ti.status AS task_status,l.source_system,l.source_invoice_id
            FROM enterprise_task_invoices ti JOIN invoices i USING(company_id,invoice_id)
            JOIN enterprise_invoice_extra x USING(company_id,invoice_id)
            JOIN enterprise_invoice_links l USING(company_id,invoice_id)
            WHERE ti.task_id=? ORDER BY i.issue_date,i.invoice_id""", (task_id,)).fetchall()
        if invoice_ids is not None and (not isinstance(invoice_ids, list) or any(not isinstance(item, str) for item in invoice_ids)):
            raise ValueError("请选择发票编号列表。")
        selected = set(invoice_ids) if invoice_ids is not None else None
        if invoice_ids is not None and len(selected) != len(invoice_ids):
            raise ValueError("请选择不重复的发票。")
        known = {row["invoice_id"] for row in rows}
        if selected is not None and not selected.issubset(known):
            raise ValueError("交付范围包含不属于此工作项的发票。")
        approved, open_items, excluded = [], [], []
        for source in rows:
            invoice = dict(source)
            if invoice["source_status"] == "void":
                excluded.append({"invoice_id": invoice["invoice_id"], "reason": "invoice_void"})
                continue
            review = connection.execute("""SELECT * FROM enterprise_reviews WHERE task_id=? AND invoice_id=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1""", (task_id, invoice["invoice_id"])).fetchone()
            snapshot = json.loads(review["reviewed_snapshot_json"]) if review and review["reviewed_snapshot_json"] else None
            current_matches = False
            if snapshot and review["action"] in {"approve", "waive"} and invoice["task_status"] in {"approved", "waived"}:
                current_snapshot = _review_snapshot(self.database, user, task_id, invoice)
                fields = [field for field in _REVIEW_FIELDS if field != "task_status"]
                current_matches = (all(snapshot.get(field) == current_snapshot.get(field) for field in fields)
                                   and snapshot.get("review_basis") == current_snapshot.get("review_basis"))
            ready = bool(review and review["action"] in {"approve", "waive"} and invoice["task_status"] in {"approved", "waived"} and current_matches)
            if ready and (selected is None or invoice["invoice_id"] in selected):
                approved.append({"invoice": invoice, "review_id": review["review_id"], "review_action": review["action"],
                                 "reviewer_user_id": review["reviewer_user_id"], "reviewed_snapshot": snapshot})
            elif ready:
                excluded.append({"invoice_id": invoice["invoice_id"], "reason": "not_selected"})
            else:
                reason = ("review_basis_changed" if review and review["action"] in {"approve", "waive"}
                          and invoice["task_status"] in {"approved", "waived"} and not current_matches else invoice["task_status"])
                open_items.append({"invoice_id": invoice["invoice_id"], "source_invoice_id": invoice["source_invoice_id"],
                                   "reason": reason, "task_status": invoice["task_status"]})
        if selected is not None and selected - {item["invoice"]["invoice_id"] for item in approved}:
            raise ValueError("所选发票包含尚未批准或批准依据已变化的项目。")
        return {"task_id": task_id, "company_id": task["company_id"], "period": task["period"],
                "approved": approved, "open_items": open_items, "excluded": excluded}

    def create(self, *, user: dict, task_id: str, request_key: str, invoice_ids: list[str] | None = None) -> dict:
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 160:
            raise ValueError("请提供本次交付的操作标识。")
        if invoice_ids is not None and (not isinstance(invoice_ids, list) or any(not isinstance(item, str) for item in invoice_ids)):
            raise ValueError("请选择发票编号列表。")
        connection = self.database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(user, task_id, create=True)
            scope = {"task_id": task_id, "invoice_ids": sorted(invoice_ids) if invoice_ids is not None else None}
            request_hash = hashlib.sha256(_json(scope).encode()).hexdigest()
            prior = connection.execute("SELECT * FROM enterprise_deliveries WHERE created_by=? AND company_id=? AND request_key=?",
                                       (user["user_id"], task["company_id"], request_key)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    connection.rollback()
                    return {"status": "idempotency_conflict"}
                connection.rollback()
                return {"status": "duplicate", "delivery_id": prior["delivery_id"], "version": prior["version"]}
            snapshot = self.preview(user=user, task_id=task_id, invoice_ids=invoice_ids)
            if not snapshot["approved"]:
                raise ValueError("当前没有可交付的已批准票据。")
            changes, evidence = [], []
            for item in snapshot["approved"]:
                invoice_id = item["invoice"]["invoice_id"]
                receipts = connection.execute("""SELECT r.operation_id,r.result_json FROM enterprise_confirmation_receipts r
                    JOIN enterprise_drafts d USING(draft_id) WHERE d.task_id=? AND r.invoice_id=? ORDER BY r.created_at,r.rowid""", (task_id, invoice_id)).fetchall()
                for receipt in receipts:
                    value = json.loads(receipt["result_json"])
                    for field in ("purpose", "remarks"):
                        before, after = value.get("before", {}).get(field), value.get("after", {}).get(field)
                        if before != after:
                            changes.append({"invoice_id": invoice_id, "field": field, "before": before, "after": after,
                                            "operation_id": receipt["operation_id"]})
                aliases = connection.execute("""SELECT a.alias_id,a.source_system,a.source_invoice_id,a.locator_json,
                    d.document_id,d.filename,d.file_hash,d.original_available FROM enterprise_source_aliases a
                    JOIN enterprise_source_documents d ON d.document_id=a.document_id
                    WHERE a.company_id=? AND a.invoice_id=? AND a.status='linked' ORDER BY a.alias_id""", (task["company_id"], invoice_id)).fetchall()
                sources = []
                for alias in aliases:
                    source = dict(alias)
                    source["locator"] = json.loads(source.pop("locator_json") or "{}")
                    source["original_available"] = bool(source["original_available"])
                    sources.append(source)
                evidence.append({"invoice_id": invoice_id, "sources": sources,
                                 "review_basis": item["reviewed_snapshot"].get("review_basis")})
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM enterprise_deliveries WHERE task_id=?", (task_id,)).fetchone()[0]
            delivery_id = "delivery-" + uuid4().hex
            from .release_manifest import release_manifest
            release = release_manifest(self.database, company_id=task["company_id"], period=task["period"])
            snapshot.update(delivery_id=delivery_id, version=version, created_at=utc_now(), created_by=user["user_id"],
                            changes=changes, evidence=evidence, release_manifest=release)
            _, archive = self._render(snapshot)
            connection.execute("""INSERT INTO enterprise_deliveries
                (delivery_id,task_id,company_id,version,created_by,request_key,request_hash,snapshot_json,archive_blob,archive_sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (delivery_id, task_id, task["company_id"], version, user["user_id"],
                request_key, request_hash, _json(snapshot), archive, hashlib.sha256(archive).hexdigest(), snapshot["created_at"]))
            connection.commit()
            return {"status": "generated", "delivery_id": delivery_id, "version": version,
                    "approved_count": len(snapshot["approved"]), "open_count": len(snapshot["open_items"])}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _render(snapshot: dict) -> tuple[str, bytes]:
        invoices = [item["invoice"] for item in snapshot["approved"]]
        files = {
            "approved_items.csv": _csv(invoices, ["invoice_id", "source_system", "source_invoice_id", "issue_date", "seller_name", "project_name", "amount_text", "currency", "purpose", "remarks"]),
            "changes.csv": _csv(snapshot["changes"], ["invoice_id", "field", "before", "after", "operation_id"]),
            "open_items.csv": _csv(snapshot["open_items"], ["invoice_id", "source_invoice_id", "reason", "task_status"]),
            "review_records.json": json.dumps(snapshot["approved"], ensure_ascii=False, indent=2).encode("utf-8"),
            "evidence_index.json": json.dumps(snapshot["evidence"], ensure_ascii=False, indent=2).encode("utf-8"),
            "runtime_manifest.json": json.dumps(snapshot["release_manifest"], ensure_ascii=False, indent=2).encode("utf-8"),
            "readme.md": ("# 票据用途核实交付包\n\n本版本保留生成时已经复核的结果；后续修改会产生新的办理和交付版本。\n\n"
                "- approved_items.csv：已批准票据、来源编号、金额、币种、用途及备注。\n"
                "- changes.csv：本工作项保存过的字段变化和操作编号。\n"
                "- open_items.csv：整个工作项中仍需办理或重新复核的票据，即使本次只选择交付部分已批准票据，也会列出全部未完成事项。\n"
                "- review_records.json：复核决定及当时看到的内容和依据。\n"
                "- evidence_index.json：来源位置、原件可用状态及复核依据。\n"
                "- runtime_manifest.json：生成交付包时的软件、模型配置和复核方法版本。\n"
                "- delivery_manifest.json：交付范围、版本和文件摘要。\n\n金额按原币种展示，不自动换算。原件可在具有授权的工作台中查看。\n").encode("utf-8"),
        }
        manifest = {key: snapshot[key] for key in ("delivery_id", "version", "task_id", "company_id", "period", "created_at", "excluded")}
        manifest.update(approved_count=len(invoices), approved_invoice_ids=[invoice["invoice_id"] for invoice in invoices],
                        open_count=len(snapshot["open_items"]), open_items_scope="entire_work_item", created_by=snapshot["created_by"],
                        files={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
        files["delivery_manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, files[name])
        return f"{snapshot['period']}-delivery-v{snapshot['version']}.zip", output.getvalue()

    def download(self, *, user: dict, delivery_id: str) -> tuple[str, bytes]:
        row = self.database.connection.execute("SELECT * FROM enterprise_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if row is None:
            raise ValueError("交付版本不存在。")
        self._task(user, row["task_id"])
        if row["archive_blob"] is None:
            raise ValueError("此历史交付包的原始文件不可用。")
        archive = bytes(row["archive_blob"])
        if hashlib.sha256(archive).hexdigest() != row["archive_sha256"]:
            raise ValueError("交付包校验失败，请联系管理员恢复原文件。")
        snapshot = json.loads(row["snapshot_json"])
        return f"{snapshot['period']}-delivery-v{snapshot['version']}.zip", archive

    def list(self, *, user: dict, task_id: str) -> list[dict]:
        self._task(user, task_id)
        rows = self.database.connection.execute("SELECT * FROM enterprise_deliveries WHERE task_id=? ORDER BY version DESC", (task_id,)).fetchall()
        result = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            result.append({"delivery_id": row["delivery_id"], "version": row["version"], "status": row["status"],
                           "created_at": row["created_at"], "approved_count": len(snapshot["approved"]),
                           "open_count": len(snapshot["open_items"])})
        return result

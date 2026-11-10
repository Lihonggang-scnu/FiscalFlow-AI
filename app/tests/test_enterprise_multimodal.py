from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from taxharness.agent_store import AgentStore
from taxharness.enterprise import multimodal


SAMPLES = Path(__file__).resolve().parents[2] / "datasets/china_invoice_guard"


class CaptureModel:
    def __init__(self):
        self.messages = None
        self.http_client = self

    def invoke(self, messages, **kwargs):
        self.messages = messages
        return AIMessage(content=json.dumps({"documents": [{"invoice_number": "24312000000055556666", "amount": "24.00"}]}),
                         usage_metadata={"input_tokens": 1200, "output_tokens": 30, "total_tokens": 1230})

    def close(self):
        pass


def test_pdf_and_user_text_are_sent_together_and_corrupt_image_is_rejected():
    pdf = SAMPLES / "07_行程发票.pdf"
    document = multimodal.prepare_document(pdf.name, pdf.read_bytes())
    assert document["page_count"] == 1 and "24312000000055556666" in document["text"]
    assert document["images"][0]["content"].startswith(b"\x89PNG")
    model = CaptureModel()
    result = multimodal.extract_document(document, user_text="这张是客户拜访，先不要保存。", user_id="U001", session_id="s", turn_id="t", model=model)
    assert result["documents"][0]["amount"] == "24.00"
    blocks = model.messages[1].content
    assert "客户拜访" in blocks[0]["text"] and any(block["type"] == "image_url" for block in blocks)
    bad = SAMPLES / "06_扫描件.png"
    with pytest.raises(ValueError, match="PNG 图片不完整"):
        multimodal.prepare_document(bad.name, bad.read_bytes())


def test_image_bytes_do_not_enter_budget_log(tmp_path, monkeypatch):
    pdf = SAMPLES / "07_行程发票.pdf"
    document = multimodal.prepare_document(pdf.name, pdf.read_bytes())
    model = CaptureModel()
    monkeypatch.setattr(multimodal, "build_live_model", lambda **_: model)
    ledger = tmp_path / "budget.sqlite3"
    result = multimodal.extract_document(document, user_text="只读取金额。", user_id="U001", session_id="s", turn_id="t", budget_ledger_path=ledger, budget_usd=1)
    assert result["documents"][0]["invoice_number"] == "24312000000055556666"
    store = AgentStore(ledger)
    with store._connect() as db:
        row = db.execute("SELECT input_json,status FROM agent_model_calls").fetchone()
        assert row["status"] == "settled" and "data:image" not in row["input_json"] and document["sha256"] in row["input_json"]


def test_uncertain_buyer_and_seller_remain_candidates_not_facts():
    result = multimodal._mark_uncertain_fields({"seller_name": "示例公司", "buyer_name": "另一家公司", "amount": "24.00",
        "tax_amount": "1.36", "uncertain_fields": ["seller_name 与 buyer_name 无法明确区分", "amount 是价税合计"]})
    assert result["seller_name"] is None and result["buyer_name"] is None
    assert result["uncertain_candidates"] == {"seller_name": "示例公司", "buyer_name": "另一家公司"}
    assert result["amount"] == "24.00"
    tax = multimodal._mark_uncertain_fields({"amount": "24.00", "tax_amount": "1.36", "uncertain_fields": ["tax_amount 无法看清"]})
    assert tax["amount"] == "24.00" and tax["tax_amount"] is None

from pathlib import Path

from scripts.live_semantics_matrix import _check
from taxharness.storage import initialize_database


ROOT = Path(__file__).resolve().parents[1]


def _turn(reply, calls=None):
    return {"status": "ok", "reply": reply, "current_company_id": "C001",
            "messages": [{"type": "ai", "data": {"tool_calls": calls or []}}]}


def test_semantic_oracle_rejects_mixed_entities_and_wrong_missing_list(tmp_path):
    database = tmp_path / "business.sqlite3"
    initialize_database(database, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    swapped = _check("same_id_two_companies", database, [_turn("甲企业 F002 金额1000.00元；乙企业 F002 金额800.00元。")])
    assert swapped["company_amount_mapping"] is False
    ambiguous = _check("same_id_two_companies", database, [_turn("两家金额分别为800.00和1000.00元。")])
    assert ambiguous["company_amount_mapping"] is None
    extra = _check("readonly_query", database, [_turn("金额9622.06元，缺用途：F002、F003、F004、F006、F008、F009。")])
    assert extra["missing_set"] is False
    correct = _check("readonly_query", database, [_turn("金额9622.06元，缺用途：F002、F003、F004、F006、F009。")])
    assert correct["missing_set"] is True


def test_semantic_oracle_detects_false_compliance_payment_and_month(tmp_path):
    database = tmp_path / "business.sqlite3"
    initialize_database(database, ROOT / "data/02_示例数据.xlsx", ROOT / "data/03_业务规则.docx.txt")
    scope = _check("out_of_scope", database, [_turn("该月不在比赛范围，但所有发票全部合规。")])
    assert scope["explained_scope"] is False
    payment = _check("no_payment_inference", database, [_turn("差额就是未付款，不能只从备注判断。")])
    assert payment["rejects_inference"] is False
    rejection = _check("no_payment_inference", database, [_turn("关于“差额就是未付款”的推断：不能成立，不能因金额不同就判断为欠款。")])
    assert rejection["rejects_inference"] is True
    uncertain = _check("no_payment_inference", database, [_turn("需要结合更多资料核实。")])
    assert uncertain["rejects_inference"] is None
    calls = [{"name": "query_finance", "args": {"month": "2026-09"}}]
    month = _check("missing_month", database, [_turn("甲企业九月总额是9622.06元。请问查哪个月份？", calls),
                                                  _turn("2026年9月总额9622.06元。")])
    assert month["no_assumed_month_call"] is False

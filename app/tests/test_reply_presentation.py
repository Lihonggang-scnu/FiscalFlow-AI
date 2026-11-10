from __future__ import annotations

import pytest

from taxharness.agent import _sanitize_reply


@pytest.mark.parametrize("version", ["revision 2", "revision: 2", '"revision": 2'])
def test_saved_version_remains_readable_in_business_sentence(version):
    raw = f"F004金额999.99元、日期2026-09-10未改，发票仍为 {version}，与上次一致。"
    reply = _sanitize_reply(raw)
    assert reply == "F004金额999.99元、日期2026-09-10未改，发票仍为 保存版本 2，与上次一致。"
    assert "仍为 ，" not in reply


def test_business_values_are_not_removed_as_internal_metadata():
    raw = "F004备注是“客户项目住宿，待补经办说明”，用途为空。金额999.99元，未重复修改。"
    assert _sanitize_reply(raw) == raw

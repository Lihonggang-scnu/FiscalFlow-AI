import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from taxharness.agent import BudgetMiddleware
from taxharness.agent_store import AgentStore
from taxharness.agent_tools import AgentContext
from taxharness.errors import BudgetExceeded, LiveModelDisabled
from taxharness.provider import build_live_model, credential_status, provider_config


def _config(path):
    path.write_text('''
[provider]
kind = "deepseek"
model = "deepseek-flash"
base_url = "https://api.deepseek.com"

[profiles.review]
kind = "openai_compatible"
model = "model-provided-by-evaluator"
base_url = "https://example.invalid/v1"
api_key_env = "TAXHARNESS_REVIEW_KEY"
input_price_cny_per_million = 2.5
output_price_cny_per_million = 10.0
context_budget_tokens = 24000
''', encoding="utf-8")


def test_profile_selects_model_and_keeps_credentials_separate(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    _config(path)
    monkeypatch.setenv("TAXHARNESS_CONFIG", str(path))
    monkeypatch.setenv("TAXHARNESS_PROFILE", "review")
    monkeypatch.setenv("TAXHARNESS_CREDENTIAL_DIR", str(tmp_path / "deepseek"))
    monkeypatch.setenv("TAXHARNESS_API_KEY", "deepseek-placeholder")
    monkeypatch.delenv("TAXHARNESS_REVIEW_KEY", raising=False)
    config = provider_config()
    assert config.kind == "openai_compatible" and config.model == "model-provided-by-evaluator"
    assert config.base_url == "https://example.invalid/v1" and config.context_budget_tokens == 24000
    assert config.api_key_dir == "" and credential_status(config)["source"] is None


def test_custom_model_uses_its_own_configuration_without_sending_a_request(tmp_path, monkeypatch):
    import langchain_openai

    path = tmp_path / "config.toml"
    _config(path)
    monkeypatch.setenv("TAXHARNESS_CONFIG", str(path))
    monkeypatch.setenv("TAXHARNESS_PROFILE", "review")
    monkeypatch.setenv("TAXHARNESS_LIVE_MODEL_ENABLED", "true")
    monkeypatch.delenv("TAXHARNESS_OFFPEAK_ONLY", raising=False)
    monkeypatch.delenv("TAXHARNESS_CUSTOM_MODEL_AUTHORIZED", raising=False)
    with pytest.raises(LiveModelDisabled):
        build_live_model(authorized=True)
    monkeypatch.setenv("TAXHARNESS_CUSTOM_MODEL_AUTHORIZED", "true")
    monkeypatch.setenv("TAXHARNESS_REVIEW_KEY", "test-only-placeholder")
    captured = {}
    def fake_model(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(http_client=kwargs["http_client"])
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_model)
    model = build_live_model(authorized=True)
    try:
        assert captured["model"] == "model-provided-by-evaluator"
        assert captured["base_url"] == "https://example.invalid/v1"
        assert captured["extra_body"] is None
    finally:
        model.http_client.close()


def test_budget_reservation_and_settlement_use_configured_rates(tmp_path):
    store = AgentStore(tmp_path / "ledger.sqlite3")
    call = store.reserve_model_call(session_id="s", user_id="U001", model="review", estimated_input_tokens=1_000_000,
        estimated_output_tokens=0, budget_usd=1, input_rate=0.75, output_rate=3.0)
    with pytest.raises(BudgetExceeded):
        store.reserve_model_call(session_id="s", user_id="U001", model="review", estimated_input_tokens=1_000_000,
            estimated_output_tokens=0, budget_usd=1, input_rate=0.75, output_rate=3.0)
    store.settle_model_call(call, 500_000, 0, input_rate=0.75, output_rate=3.0)
    with sqlite3.connect(store.path) as connection:
        status, actual = connection.execute("SELECT status,actual_usd FROM agent_model_calls WHERE call_id=?", (call,)).fetchone()
    assert status == "settled" and actual == 0.375


def test_deepseek_peak_pricing_is_accounted_for_when_authorized(tmp_path, monkeypatch):
    import taxharness.agent as agent

    monkeypatch.delenv("TAXHARNESS_OFFPEAK_ONLY", raising=False)
    monkeypatch.setattr(agent, "_peak_window", lambda _now: True)
    store = AgentStore(tmp_path / "ledger.sqlite3")
    context = AgentContext(user_id="U001", session_id="s", database_path=str(tmp_path / "business.sqlite3"),
        agent_store_path=str(tmp_path / "agent.sqlite3"), current_company_id="C001", budget_ledger_path=str(store.path),
        budget_usd=1.0, max_output_tokens=100, turn_id="t")
    request = SimpleNamespace(runtime=SimpleNamespace(context=context), system_message=SystemMessage(content="查询"),
                              messages=[HumanMessage(content="查询")], tools=[])
    response = AIMessage(content="完成", usage_metadata={"input_tokens": 200, "output_tokens": 50, "total_tokens": 250})
    BudgetMiddleware().wrap_model_call(request, lambda _request: SimpleNamespace(result=[response]))
    with sqlite3.connect(store.path) as connection:
        value = connection.execute("SELECT actual_usd FROM agent_model_calls").fetchone()[0]
    assert value == pytest.approx((200 * 0.6 + 50 * 2.4) / 1_000_000)

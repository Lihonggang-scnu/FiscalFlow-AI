from __future__ import annotations

import sys
from pathlib import Path

from scripts import chat


def test_chat_passes_explicit_state_paths_without_model(monkeypatch, tmp_path: Path):
    captured = {}

    class DummyStore:
        def session(self, session_id, user_id):
            return None

    class DummyRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.agent_store = DummyStore()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(chat, "database_initialized", lambda path: True)
    monkeypatch.setattr(chat, "AgentRuntime", DummyRuntime)
    monkeypatch.setattr("builtins.input", lambda _prompt: "/退出")
    database = tmp_path / "business.sqlite3"
    agent_store = tmp_path / "ledger.sqlite3"
    checkpoint_store = tmp_path / "checkpoints.sqlite3"
    preference_store = tmp_path / "preferences.sqlite3"
    monkeypatch.setattr(sys, "argv", [
        "chat.py", "--database", str(database), "--agent-store", str(agent_store),
        "--checkpoint-store", str(checkpoint_store), "--preference-store", str(preference_store),
    ])

    assert chat.main() == 0
    assert captured["database_path"] == database
    assert captured["agent_store_path"] == agent_store
    assert captured["checkpoint_path"] == checkpoint_store
    assert captured["preference_store_path"] == preference_store
    assert captured["closed"] is True


def test_chat_store_defaults_read_environment(monkeypatch):
    monkeypatch.setenv("TAXHARNESS_AGENT_STORE", "env-ledger.sqlite3")
    monkeypatch.setenv("TAXHARNESS_CHECKPOINT_STORE", "env-checkpoints.sqlite3")
    monkeypatch.setenv("TAXHARNESS_PREFERENCE_STORE", "env-preferences.sqlite3")
    assert chat._path_env("TAXHARNESS_AGENT_STORE") == Path("env-ledger.sqlite3")
    assert chat._path_env("TAXHARNESS_CHECKPOINT_STORE") == Path("env-checkpoints.sqlite3")
    assert chat._path_env("TAXHARNESS_PREFERENCE_STORE") == Path("env-preferences.sqlite3")

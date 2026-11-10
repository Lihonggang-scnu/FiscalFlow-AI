from __future__ import annotations

import json
from pathlib import Path

from taxharness.provider import credential_status, provider_config
from scripts import preflight


def test_credential_precedence_env_then_config_directory(tmp_path, monkeypatch):
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    (credential_dir / "only.txt").write_text("dummy-credential-value", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f'[provider]\ncredential_dir="{credential_dir.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("TAXHARNESS_CONFIG", str(config))
    monkeypatch.delenv("TAXHARNESS_API_KEY", raising=False)
    assert credential_status(provider_config())["source"] == "external_directory"
    monkeypatch.setenv("TAXHARNESS_API_KEY", "env-credential-value")
    assert credential_status(provider_config())["source"] == "TAXHARNESS_API_KEY"
    monkeypatch.delenv("TAXHARNESS_API_KEY", raising=False)


def test_preflight_treats_pytest_as_optional(monkeypatch):
    original = preflight.metadata.version

    def fake_version(name):
        if name == "pytest":
            raise preflight.metadata.PackageNotFoundError
        return original(name)

    monkeypatch.setattr(preflight.metadata, "version", fake_version)
    code, result = preflight.run_preflight(
        Path("data/02_示例数据.xlsx").resolve(), Path("data/03_业务规则.docx.txt").resolve()
    )
    assert code == 0
    assert result["foundation_ready"] is True
    assert result["packages"]["pytest"] is None
    assert "pytest" in result["optional_packages"]


def test_preflight_requires_actual_beijing_timezone_data(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "timezone_data_status",
        lambda: {"available": False, "zone": "Asia/Shanghai"},
    )
    code, result = preflight.run_preflight(
        Path("data/02_示例数据.xlsx").resolve(), Path("data/03_业务规则.docx.txt").resolve()
    )
    assert code == 1
    assert result["foundation_ready"] is False
    assert result["timezone_data"] == {"available": False, "zone": "Asia/Shanghai"}

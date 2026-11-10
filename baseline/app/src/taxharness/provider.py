"""DeepSeek provider configuration and conservative live-call boundary."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import InvalidInputError, LiveModelDisabled


@dataclass(frozen=True)
class ProviderConfig:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 180.0
    max_output_tokens: int = 4096
    thinking: bool = False
    max_retries: int = 0
    api_key_dir: str = ""
    budget_usd: float = 2.0


def _file_provider_config() -> dict:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.getenv("TAXHARNESS_CONFIG", str(project_root / "config.toml"))).expanduser()
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as stream:
        return dict(tomllib.load(stream).get("provider", {}))


def provider_config() -> ProviderConfig:
    values = _file_provider_config()
    model = str(values.get("model", "deepseek-flash"))
    if model != "deepseek-flash":
        raise InvalidInputError("本模块只允许用户指定的 API model=deepseek-flash。")
    return ProviderConfig(
        model=model,
        base_url=str(values.get("base_url", "https://api.deepseek.com")),
        timeout_seconds=float(values.get("timeout_seconds", 180)),
        max_output_tokens=int(values.get("max_output_tokens", 4096)),
        thinking=False,
        max_retries=0,
        api_key_dir=str(values.get("credential_dir", os.getenv("TAXHARNESS_CREDENTIAL_DIR", ""))),
        budget_usd=float(values.get("budget_usd", 2)),
    )


def _validate_credential(value: str) -> bool:
    return bool(value) and not any(character.isspace() for character in value) and 8 <= len(value) <= 512


def credential_status(config: ProviderConfig | None = None) -> dict:
    """Inspect the same credential precedence used by live provider creation without returning its value."""

    config = config or provider_config()
    direct = os.getenv("TAXHARNESS_API_KEY", "").strip()
    if direct:
        return {"source": "TAXHARNESS_API_KEY", "format_valid": _validate_credential(direct), "value_logged": False}
    if not config.api_key_dir:
        return {"source": None, "directory_configured": False, "nonempty_txt_count": 0, "format_valid": False, "value_logged": False}
    directory = Path(config.api_key_dir).expanduser()
    files = [path for path in directory.glob("*.txt") if path.is_file() and path.stat().st_size > 0]
    valid = False
    if len(files) == 1:
        try:
            valid = _validate_credential(files[0].read_text(encoding="utf-8-sig").strip())
        except (OSError, UnicodeError):
            valid = False
    return {"source": "external_directory", "directory_configured": True, "nonempty_txt_count": len(files), "format_valid": valid, "value_logged": False}


def _read_api_key(config: ProviderConfig) -> str:
    direct = os.getenv("TAXHARNESS_API_KEY", "").strip()
    if direct:
        if not _validate_credential(direct):
            raise InvalidInputError("TAXHARNESS_API_KEY 格式无效。")
        return direct
    if not config.api_key_dir:
        raise InvalidInputError("未配置 DeepSeek 凭据；请使用 TAXHARNESS_API_KEY 或外部 credential_dir。")
    directory = Path(config.api_key_dir).expanduser()
    files = [path for path in directory.glob("*.txt") if path.is_file() and path.stat().st_size > 0]
    if len(files) != 1:
        raise InvalidInputError("DeepSeek 外部凭据目录必须包含唯一非空 TXT 文件。")
    value = files[0].read_text(encoding="utf-8-sig").strip()
    if not _validate_credential(value):
        raise InvalidInputError("DeepSeek 外部凭据格式无效。")
    return value


def _peak_window(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    clock = local.time().replace(tzinfo=None)
    return local.weekday() < 5 and (time(9) <= clock < time(12) or time(14) <= clock < time(18))


def _peak_authorized_now(now: datetime) -> bool:
    """A one-time peak authorization expires even if the server remains running."""

    value = os.getenv("TAXHARNESS_PEAK_ALLOWED_UNTIL", "").strip()
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return False
    return expiry.tzinfo is not None and now.astimezone(ZoneInfo("Asia/Shanghai")) < expiry


def build_live_model(*, authorized: bool = False, config: ProviderConfig | None = None):
    """Build a ChatOpenAI-compatible model; no network request occurs here."""

    if not authorized or os.getenv("TAXHARNESS_LIVE_MODEL_ENABLED", "false").lower() != "true":
        raise LiveModelDisabled("实时模型调用尚未授权或开关未开启。")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if os.getenv("TAXHARNESS_OFFPEAK_ONLY", "false").lower() == "true" and _peak_window(now) and not _peak_authorized_now(now):
        raise LiveModelDisabled("当前处于工作日高峰时段，候选版模型将在空闲时段开放。")
    config = config or provider_config()
    if config.base_url != "https://api.deepseek.com":
        raise InvalidInputError("默认 provider 只允许 https://api.deepseek.com。")
    from langchain_openai import ChatOpenAI
    import httpx

    api_key = _read_api_key(config)
    client = httpx.Client(transport=httpx.HTTPTransport(retries=0), timeout=config.timeout_seconds, trust_env=False)
    return ChatOpenAI(
        model=config.model,
        base_url=config.base_url,
        api_key=api_key,
        use_responses_api=False,
        temperature=0,
        timeout=config.timeout_seconds,
        max_retries=0,
        max_tokens=config.max_output_tokens,
        extra_body={"thinking": {"type": "disabled"}},
        http_client=client,
    )

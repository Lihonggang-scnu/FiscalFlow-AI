"""DeepSeek provider configuration and conservative live-call boundary."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .errors import InvalidInputError, LiveModelDisabled


@dataclass(frozen=True)
class ProviderConfig:
    kind: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 180.0
    max_output_tokens: int = 4096
    thinking: bool = False
    max_retries: int = 0
    api_key_dir: str = ""
    api_key_env: str = "TAXHARNESS_API_KEY"
    budget_usd: float = 2.0
    input_price_cny: float | None = 1.0
    output_price_cny: float | None = 4.0
    context_budget_tokens: int = 24000
    extra_body: dict = field(default_factory=dict)


def _file_provider_config() -> dict:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.getenv("TAXHARNESS_CONFIG", str(project_root / "config.toml"))).expanduser()
    if not config_path.is_file():
        return {}
    with config_path.open("rb") as stream:
        return dict(tomllib.load(stream))


def provider_config() -> ProviderConfig:
    config = _file_provider_config()
    profile = os.getenv("TAXHARNESS_PROFILE", "").strip()
    if profile:
        values = config.get("profiles", {}).get(profile)
        if not isinstance(values, dict):
            raise InvalidInputError(f"模型配置 {profile} 不存在。")
    else:
        values = dict(config.get("provider", {}))
    kind = str(values.get("kind", "deepseek")).strip()
    if kind not in {"deepseek", "openai_compatible"}:
        raise InvalidInputError("模型配置 kind 必须是 deepseek 或 openai_compatible。")
    model = str(values.get("model", "deepseek-flash" if kind == "deepseek" else "")).strip()
    base_url = str(values.get("base_url", "https://api.deepseek.com" if kind == "deepseek" else "")).strip()
    parsed = urlsplit(base_url)
    if not model or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidInputError("模型配置需要有效的 model 和 base_url。")
    if kind == "deepseek" and (model != "deepseek-flash" or base_url.rstrip("/") != "https://api.deepseek.com"):
        raise InvalidInputError("DeepSeek 配置使用 deepseek-flash 和官方地址；其他模型使用独立配置。")
    input_price = values.get("input_price_cny_per_million", 1.0 if kind == "deepseek" else None)
    output_price = values.get("output_price_cny_per_million", 4.0 if kind == "deepseek" else None)
    return ProviderConfig(
        kind=kind,
        model=model,
        base_url=base_url,
        timeout_seconds=float(values.get("timeout_seconds", 180)),
        max_output_tokens=int(values.get("max_output_tokens", 4096)),
        thinking=bool(values.get("thinking", False)),
        max_retries=0,
        api_key_dir=str(values.get("credential_dir", os.getenv("TAXHARNESS_CREDENTIAL_DIR", "") if kind == "deepseek" else "")),
        api_key_env=str(values.get("api_key_env", "TAXHARNESS_API_KEY")),
        budget_usd=float(values.get("budget_usd", 2)),
        input_price_cny=float(input_price) if input_price is not None else None,
        output_price_cny=float(output_price) if output_price is not None else None,
        context_budget_tokens=int(values.get("context_budget_tokens", 24000)),
        extra_body=dict(values.get("extra_body", {})),
    )


def _validate_credential(value: str) -> bool:
    return bool(value) and not any(character.isspace() for character in value) and 8 <= len(value) <= 512


def credential_status(config: ProviderConfig | None = None) -> dict:
    """Inspect the same credential precedence used by live provider creation without returning its value."""

    config = config or provider_config()
    direct = os.getenv(config.api_key_env, "").strip()
    if direct:
        return {"source": config.api_key_env, "format_valid": _validate_credential(direct), "value_logged": False}
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
    direct = os.getenv(config.api_key_env, "").strip()
    if direct:
        if not _validate_credential(direct):
            raise InvalidInputError(f"{config.api_key_env} 格式无效。")
        return direct
    if not config.api_key_dir:
        raise InvalidInputError(f"未配置模型凭据；请使用 {config.api_key_env} 或外部 credential_dir。")
    directory = Path(config.api_key_dir).expanduser()
    files = [path for path in directory.glob("*.txt") if path.is_file() and path.stat().st_size > 0]
    if len(files) != 1:
        raise InvalidInputError("模型外部凭据目录必须包含唯一非空 TXT 文件。")
    value = files[0].read_text(encoding="utf-8-sig").strip()
    if not _validate_credential(value):
        raise InvalidInputError("模型外部凭据格式无效。")
    return value


def _peak_window(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    clock = local.time().replace(tzinfo=None)
    return local.weekday() < 5 and (time(9) <= clock < time(12) or time(14) <= clock < time(18))


def ledger_rates(input_price_cny: float, output_price_cny: float, provider_kind: str, now: datetime) -> tuple[float, float]:
    """Use the shared ledger's established 0.3 units/CNY conversion and peak tariff."""
    factor = 2 if provider_kind == "deepseek" and _peak_window(now) else 1
    return float(input_price_cny) * 0.3 * factor, float(output_price_cny) * 0.3 * factor


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
    if config.kind != "deepseek" and os.getenv("TAXHARNESS_CUSTOM_MODEL_AUTHORIZED", "false").lower() != "true":
        raise LiveModelDisabled("当前阶段只启用 DeepSeek；其他模型请配置对应预算后启用。")
    if config.input_price_cny is None or config.output_price_cny is None or config.input_price_cny < 0 or config.output_price_cny < 0:
        raise LiveModelDisabled("请为当前模型配置输入和输出的百万 token 价格。")
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
        extra_body=({"thinking": {"type": "enabled" if config.thinking else "disabled"}, **config.extra_body}
                    if config.kind == "deepseek" else (config.extra_body or None)),
        http_client=client,
    )

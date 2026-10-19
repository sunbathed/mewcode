from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when no usable model provider configuration is available."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    protocol: str
    model: str
    base_url: str
    api_key: str


def _first(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _default_zcode_config_path() -> Path:
    override = os.getenv("MEWCODE_ZCODE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    return home / ".zcode" / "v2" / "provider_config.json"


def _from_provider_config(path: Path) -> ProviderConfig | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    config = raw.get("config", raw) if isinstance(raw, dict) else {}
    api = config.get("providerConfigRules", {}) if isinstance(config, dict) else {}
    rules = api.get("providerRules", []) if isinstance(api, dict) else []
    if not isinstance(rules, list):
        return None

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        provider = rule.get("config", rule)
        if not isinstance(provider, dict):
            continue
        api_data = provider.get("api", {})
        access_data = provider.get("access", {})
        if not isinstance(api_data, dict):
            continue
        if not isinstance(access_data, dict):
            access_data = {}
        base_url = _first(api_data, "baseUrl", "base_url")
        api_key = _first(access_data, "apiKey", "api_key", "key") or _first(api_data, "apiKey", "api_key", "key")
        api_type = _first(api_data, "type", "protocol")
        model = _first(provider, "model", "defaultModel")
        models = provider.get("modelOrder") or provider.get("personalModelIds")
        if not model and isinstance(models, list) and models:
            model = next((m for m in models if isinstance(m, str) and m.strip()), "")
        if base_url and api_key and model and ("openai" in api_type or not api_type):
            return ProviderConfig("openai", model, base_url, api_key)
    return None


def _from_environment(environ: Mapping[str, str]) -> ProviderConfig | None:
    api_key = environ.get("MEWCODE_API_KEY", "").strip()
    base_url = environ.get("MEWCODE_BASE_URL", "").strip()
    model = environ.get("MEWCODE_MODEL", "").strip()
    if api_key and base_url and model:
        return ProviderConfig(environ.get("MEWCODE_PROTOCOL", "openai").strip() or "openai", model, base_url, api_key)
    return None


def load_provider_config(
    *,
    zcode_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> ProviderConfig:
    """Load explicit MewCode overrides first, then a ZCode provider config."""
    if dotenv_path is not None:
        load_dotenv(dotenv_path, override=False)
    env = environ if environ is not None else os.environ
    explicit = _from_environment(env)
    if explicit:
        return explicit

    config = _from_provider_config(zcode_path or _default_zcode_config_path())
    if config:
        return config
    raise ConfigurationError(
        "未找到可用的 LLM 配置。请检查 ZCode provider_config.json，或设置 "
        "MEWCODE_API_KEY、MEWCODE_BASE_URL 和 MEWCODE_MODEL。"
    )

import json

import pytest

from mewcode.config import ConfigurationError, load_provider_config


def test_environment_overrides(tmp_path):
    cfg = load_provider_config(environ={"MEWCODE_API_KEY": "secret", "MEWCODE_BASE_URL": "http://localhost/v1", "MEWCODE_MODEL": "demo"}, zcode_path=tmp_path / "none.json")
    assert cfg.model == "demo"
    assert cfg.api_key == "secret"


def test_provider_config(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"config": {"providerConfigRules": {"providerRules": [{"config": {"api": {"type": "openai-chat-completions", "baseUrl": "http://localhost/v1", "apiKey": "secret"}, "modelOrder": ["demo"]}}]}}}), encoding="utf-8")
    cfg = load_provider_config(zcode_path=path, environ={})
    assert cfg.base_url == "http://localhost/v1"


def test_missing_config(tmp_path):
    with pytest.raises(ConfigurationError):
        load_provider_config(zcode_path=tmp_path / "none.json", environ={})

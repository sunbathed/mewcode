from __future__ import annotations

from mewcode.engine.prompt_pipeline import assemble_api_payload, system_reminder_message


def test_system_reminder_message_format():
    msg = system_reminder_message("这是动态提醒")
    assert msg["role"] == "user"
    assert msg["content"] == "<system-reminder>\n这是动态提醒\n</system-reminder>"


def test_assemble_api_payload_with_cache():
    system = "你是 MewCode"
    messages = [{"role": "user", "content": "你好"}]
    tools = [{"type": "function", "function": {"name": "ReadFile"}}]

    payload = assemble_api_payload(system, messages, tools=tools, enable_cache=True)
    assert payload["system"] == system
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"] == system
    assert payload["messages"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][1]["role"] == "user"
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


def test_assemble_api_payload_without_cache():
    system = "你是 MewCode"
    messages = [{"role": "user", "content": "你好"}]
    payload = assemble_api_payload(system, messages, enable_cache=False)
    assert "cache_control" not in payload["messages"][0]

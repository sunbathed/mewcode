import json

import pytest

from mewcode.session import (
    Message,
    Session,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)


def test_session_roundtrip_with_blocks():
    session = Session()
    session.add_message("user", "读一下文件")
    session.add_message(
        "assistant",
        "好的", 
        blocks=[TextBlock("好的"), ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.py"})],
    )
    session.add_tool_results([("call_1", "完整文件内容")])
    restored = Session.from_dict(session.to_dict())
    assert len(restored.messages) == 3
    assert isinstance(restored.messages[1].blocks[1], ToolUseBlock)
    assert restored.messages[1].blocks[1].id == "call_1"
    assert isinstance(restored.messages[2].blocks[0], ToolResultBlock)
    assert restored.messages[2].blocks[0].tool_use_id == "call_1"


def test_api_messages_tool_sequence():
    session = Session()
    session.add_message("user", "读一下文件")
    session.add_message(
        "assistant",
        "",
        blocks=[ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.py"})],
    )
    session.add_tool_results([("call_1", "内容")])
    api = session.api_messages()
    assert api[0] == {"role": "user", "content": "读一下文件"}
    assert api[1]["role"] == "assistant"
    assert api[1]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(api[1]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}
    assert api[2] == {"role": "tool", "tool_call_id": "call_1", "content": "内容"}


def test_legacy_message_compat():
    old = Message.from_dict({"role": "user", "content": "旧消息", "created_at": "2024-01-01"})
    assert old.blocks is None
    assert old.to_api() == [{"role": "user", "content": "旧消息"}]
    data = old.to_dict()
    assert "blocks" not in data


def test_invalid_message_rejected():
    with pytest.raises(ValueError):
        Message.from_dict({"role": "alien", "content": "x"})
    with pytest.raises(ValueError):
        Message.from_dict({"role": "user"})
    with pytest.raises(ValueError):
        Message.from_dict({"role": "user", "content": 42})
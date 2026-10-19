"""第五章：App 端到端核心测试（会话持久化与恢复、工具调用回合）。"""

import io

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.session import ToolResultBlock, ToolUseBlock
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools import ToolRegistry


class FakeClient:
    def __init__(self):
        self.requests = []

    async def stream(self, messages):
        self.requests.append(messages)
        yield TextDelta("回答")
        yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_conversation_persists_and_restores(tmp_path):
    answers = iter(["你好", "/exit"])
    output = io.StringIO()
    client = FakeClient()
    app = App(ProviderConfig("openai", "demo", "http://test/v1", "secret"), storage=SessionStorage(tmp_path), client=client, terminal=Terminal(input_fn=lambda _: next(answers), output=output))
    await app.run_async()
    assert len(client.requests) == 1
    assert "回答" in output.getvalue()
    restored = App(ProviderConfig("openai", "demo", "http://test/v1", "secret"), storage=SessionStorage(tmp_path), client=FakeClient(), terminal=Terminal(input_fn=lambda _: "/exit", output=io.StringIO()))
    assert [m.content for m in restored.session.messages] == ["你好", "回答"]


class ToolClient:
    """第一轮：发起分片的 ReadFile 工具请求；第二轮（回传后）：输出最终回答。"""

    def __init__(self):
        self.turns = []
        self.calls = 0

    async def stream(self, messages):
        self.turns.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_1", name="ReadFile")
            yield ToolUseDelta(index=0, args_fragment='{"path":')
            yield ToolUseDelta(index=0, args_fragment='"hello.txt"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("文件内容是：")
            yield TextDelta("hello world")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_tool_call_single_turn(tmp_path):
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    answers = iter(["读取 hello.txt", "/exit"])
    output = io.StringIO()
    client = ToolClient()
    registry = ToolRegistry(workdir=str(tmp_path))
    app = App(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        storage=SessionStorage(tmp_path),
        client=client,
        terminal=Terminal(input_fn=lambda _: next(answers), output=output),
        registry=registry,
    )
    await app.run_async()

    text = output.getvalue()
    assert "⎿ ⎿ ReadFile" in text
    assert "✓" in text
    assert "文件内容是：hello world" in text

    # 回传消息：第二轮请求包含 tool 结果
    assert client.calls == 2
    tool_messages = [m for m in client.turns[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"

    # 会话持久化了 tool_use 与 tool_result，且可恢复
    saved = [m for m in app.session.messages]
    assert any(isinstance(b, ToolUseBlock) for m in saved for b in (m.blocks or []))
    assert any(isinstance(b, ToolResultBlock) for m in saved for b in (m.blocks or []))
    restored = App(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        storage=SessionStorage(tmp_path),
        client=FakeClient(),
        terminal=Terminal(input_fn=lambda _: "/exit", output=io.StringIO()),
    )
    assert len(restored.session.messages) == len(saved)
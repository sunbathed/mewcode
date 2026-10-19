"""第五章：TUI 核心渲染测试（对话流、工具卡片）。"""

import pytest

from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.storage import SessionStorage
from mewcode.tools import ToolRegistry
from mewcode.tui import MewCodeTUI


class FakeClient:
    async def stream(self, messages):
        yield TextDelta("流式回答")
        yield StreamEnd("stop")


class ToolClient:
    def __init__(self):
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_1", name="ReadFile")
            yield ToolUseDelta(index=0, args_fragment='{"path":"a.txt"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("文件内容是 hello")
            yield StreamEnd("stop")


def make_app(tmp_path, client):
    return MewCodeTUI(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        storage=SessionStorage(tmp_path),
        client=client,
        registry=ToolRegistry(workdir=str(tmp_path)),
    )


def conversation_text(app):
    rows = []
    for child in app.query_one("#conversation").children:
        content = getattr(child, "content", "")
        rows.append(getattr(content, "plain", str(content)))
    return rows


@pytest.mark.asyncio
async def test_single_stream_has_one_conversation_feed(tmp_path):
    app = make_app(tmp_path, FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#conversation").display is False
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#conversation").display is True
        rows = conversation_text(app)
        assert rows == ["你  › hi", "流式回答"]
        assert not app.query("#live-answer")
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tool_card_updates_in_place(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app = make_app(tmp_path, ToolClient())
    from mewcode.session import ToolUseBlock

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r", "e", "a", "d", "enter")
        await pilot.pause()
        await pilot.pause()
        rows = conversation_text(app)
        assert len(rows) == 3
        assert rows[0] == "你  › read"
        assert rows[1].startswith("✓ ⎿ ⎿ ReadFile")
        assert "hello" in rows[1]
        assert rows[2] == "文件内容是 hello"
        assert any(isinstance(b, ToolUseBlock) for m in app.session.messages for b in (m.blocks or []))
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()
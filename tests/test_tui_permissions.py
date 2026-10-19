"""第六章：TUI 层权限核心测试（HITL Modal 放行 / 黑名单硬拦截）。"""

import pytest
from textual.widgets import Static

from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.storage import SessionStorage
from mewcode.tools import ToolRegistry
from mewcode.tui import MewCodeTUI


class WriteClient:
    """第一轮 WriteFile，第二轮最终回答。"""

    def __init__(self):
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_1", name="WriteFile")
            yield ToolUseDelta(index=0, args_fragment='{"path":"hello.txt","content":"1"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("已写入")
            yield StreamEnd("stop")


class BashClient:
    def __init__(self):
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="c1", name="Bash")
            yield ToolUseDelta(index=0, args_fragment='{"command":"rm -rf /"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("已停止")
            yield StreamEnd("stop")


def make_app(tmp_path, client, permission_mode="default"):
    return MewCodeTUI(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        storage=SessionStorage(tmp_path),
        client=client,
        registry=ToolRegistry(workdir=str(tmp_path)),
        workdir=str(tmp_path),
        permission_mode=permission_mode,
    )


def conversation_text(app):
    rows = []
    for child in app.query_one("#conversation").children:
        content = getattr(child, "content", "")
        rows.append(getattr(content, "plain", str(content)))
    return rows


@pytest.mark.asyncio
async def test_default_mode_write_opens_permission_modal(tmp_path):
    """default 模式 WriteFile 弹出 HITL Modal，按 2（始终允许）放行并生成规则。"""
    app = make_app(tmp_path, WriteClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await pilot.pause()
        await pilot.pause()
        texts = [getattr(w, "content", "") for w in app.screen.query(Static)]
        joined = "".join(str(getattr(t, "plain", t)) for t in texts)
        assert "想要执行以下操作" in joined
        await pilot.press("2")  # 始终允许
        for _ in range(6):
            await pilot.pause()
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "1"
        local = tmp_path / ".mewcode" / "permissions.local.yaml"
        assert local.exists()
        assert "WriteFile(hello.txt)" in local.read_text(encoding="utf-8")
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()


@pytest.mark.asyncio
async def test_dangerous_command_denied_no_modal(tmp_path):
    """TUI 中黑名单命令在 bypassPermissions 模式下仍被硬拦截、不弹窗。"""
    app = make_app(tmp_path, BashClient(), permission_mode="bypassPermissions")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r", "u", "n", "enter")
        await pilot.pause()
        await pilot.pause()
        texts = [getattr(w, "content", "") for w in app.screen.query(Static)]
        joined = "".join(str(getattr(t, "plain", t)) for t in texts)
        assert "想要执行以下操作" not in joined  # 黑名单直接拒绝，无 Modal
        rows = conversation_text(app)
        assert any("✗" in r for r in rows)
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()
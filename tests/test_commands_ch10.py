"""第 10 章：App / TUI 集成测试（status/review/permission 别名、Tab 补全、help 列表）。"""

import io

import pytest

from mewcode.app import App
from mewcode.commands import parse_command
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal

from conftest import ScriptedClient, make_registry, text_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client=None, *, permission_mode="bypassPermissions", **kwargs):
    output = io.StringIO()
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client or ScriptedClient([]),
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=make_registry(tmp_path),
        workdir=str(tmp_path),
        permission_mode=permission_mode,
        **kwargs,
    )
    return app, output


@pytest.mark.asyncio
async def test_status_command_shows_state(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([text_turn("回答")]))
    await app._ask("你好")
    app._handle_command(parse_command("/status"))
    text = output.getvalue()
    assert "MewCode 状态" in text
    assert "模式" in text
    assert "Token" in text
    assert "工具" in text
    assert "工作目录" in text


@pytest.mark.asyncio
async def test_status_alias_s(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/s"))
    assert "MewCode 状态" in output.getvalue()


@pytest.mark.asyncio
async def test_review_sends_prompt_to_agent(tmp_path):
    """PROMPT 类型命令：/review 触发一轮 Agent 对话。"""
    from mewcode.commands.handlers.review import REVIEW_PROMPT

    client = ScriptedClient([text_turn("审查完成，一切正常")])
    app, output = make_app(tmp_path, client)
    # 模拟 run_async 的 review 分支：直接把 REVIEW_PROMPT 交给 Agent
    await app._ask(REVIEW_PROMPT)
    assert "审查完成" in output.getvalue()
    assert client.requests[-1][0]["role"] == "system"


@pytest.mark.asyncio
async def test_permission_alias_and_mode_switch(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]), permission_mode="default")
    # /permission 别名（框架命令）与旧 /permissions 等价
    app._handle_command(parse_command("/permission"))
    assert "default" in output.getvalue()
    app._handle_command(parse_command("/permission mode acceptEdits"))
    from mewcode.permission import PermissionMode

    assert app.permission.mode == PermissionMode.ACCEPT_EDITS


@pytest.mark.asyncio
async def test_help_lists_new_commands(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/help"))
    text = output.getvalue()
    assert "/status" in text
    assert "/review" in text
    assert "/permission" in text
    assert "/plan" in text
    assert "/do" in text


@pytest.mark.asyncio
async def test_unknown_command_guides_to_help(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/bogus"))
    assert "未知命令" in output.getvalue()
    assert "/help" in output.getvalue()


@pytest.mark.asyncio
async def test_compact_command_no_content(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/compact"))
    assert "无需压缩" in output.getvalue()


@pytest.mark.asyncio
async def test_registry_is_wired_into_app(tmp_path):
    app, _ = make_app(tmp_path)
    assert app.commands.find("compact") is not None
    assert app.commands.find("c") is not None
    assert app.commands.find("status") is not None
    assert app.commands.find("exit") is not None
    # 补全候选：输入 /com → 候选列表含 /compact
    assert "/compact" in app.commands.complete("/com")


# ---------------------------------------------------------------------------
# TUI 集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_tab_completion_shows_popup(tmp_path):
    from mewcode.tui import MewCodeTUI
    from mewcode.tools import ToolRegistry
    from textual.widgets import Input

    app = MewCodeTUI(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=ScriptedClient([]),
        registry=ToolRegistry(workdir=str(tmp_path)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # 输入框输入 /c 后触发 Tab 补全（真实按键流）
        await pilot.press("/", "c")
        await pilot.press("tab")
        await pilot.pause()
        # 补全弹窗显示，包含 /compact 候选
        assert app.completion_popup.display is True
        from textual.widgets import OptionList

        ol = app.completion_popup.query_one("#completion-list", OptionList)
        assert ol.option_count >= 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.completion_popup.display is False
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_help_shows_all_commands(tmp_path):
    from mewcode.tui import MewCodeTUI
    from mewcode.tools import ToolRegistry

    app = MewCodeTUI(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=ScriptedClient([]),
        registry=ToolRegistry(workdir=str(tmp_path)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/", "h", "e", "l", "p", "enter")
        await pilot.pause()
        await pilot.pause()
        rows = []
        for child in app.query_one("#conversation").children:
            content = getattr(child, "content", "")
            rows.append(getattr(content, "plain", str(content)))
        joined = "\n".join(rows)
        assert "可用命令" in joined
        assert "/status" in joined
        assert "/review" in joined
        assert "/compact" in joined
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_status_command(tmp_path):
    from mewcode.tui import MewCodeTUI
    from mewcode.tools import ToolRegistry

    app = MewCodeTUI(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=ScriptedClient([]),
        registry=ToolRegistry(workdir=str(tmp_path)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/", "s", "t", "a", "t", "u", "s", "enter")
        await pilot.pause()
        await pilot.pause()
        rows = []
        for child in app.query_one("#conversation").children:
            content = getattr(child, "content", "")
            rows.append(getattr(content, "plain", str(content)))
        joined = "\n".join(rows)
        assert "MewCode 状态" in joined
        await pilot.press("/", "e", "x", "i", "t", "enter")
        await pilot.pause()

"""第四章：App 层集成测试（/plan、/do 命令、Agent 事件流渲染、取消与停止提示）。

只保留核心：对话渲染、工具卡片、Plan Mode 命令与拦截、取消/上限提示。
"""

import io

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal

from conftest import ScriptedClient, make_registry, text_turn, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client, *, max_iterations=10):
    output = io.StringIO()
    registry = make_registry(tmp_path)
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client,
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=registry,
        workdir=str(tmp_path),
        max_iterations=max_iterations,
    )
    return app, output


class TextClient:
    async def stream(self, messages):
        yield TextDelta("你好！")
        yield StreamEnd("stop")


class ToolClient:
    """第一轮 ReadFile，第二轮最终回答。"""

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


@pytest.mark.asyncio
async def test_plain_conversation_via_agent(tmp_path):
    app, output = make_app(tmp_path, TextClient())
    await app._ask("你好")
    assert "你好！" in output.getvalue()


@pytest.mark.asyncio
async def test_tool_turn_renders_card_and_result(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    app, output = make_app(tmp_path, ToolClient())
    await app._ask("读一下 a.txt")
    text = output.getvalue()
    assert "⎿ ⎿ ReadFile" in text
    assert "✓" in text
    assert "文件内容是 hello" in text
    assert app.client.calls == 2


@pytest.mark.asyncio
async def test_multi_turn_loop_via_agent(tmp_path):
    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    script = [
        tools_turn([("c1", "Glob", {"pattern": "*.txt"})]),
        tools_turn([("c2", "ReadFile", {"path": "a.txt"})]),
        text_turn("找到 a.txt，内容是 data"),
    ]
    app, output = make_app(tmp_path, ScriptedClient(script))
    await app._ask("找入口文件")
    assert "⎿ ⎿ Glob" in output.getvalue()
    assert "⎿ ⎿ ReadFile" in output.getvalue()
    assert "找到 a.txt，内容是 data" in output.getvalue()


@pytest.mark.asyncio
async def test_plan_command_enters_plan_mode(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/plan"))
    assert app.plan_state.active is True
    assert "已进入规划模式" in output.getvalue()
    assert app.plan_state.plan_file is not None


@pytest.mark.asyncio
async def test_plan_creates_plans_directory(tmp_path):
    app, _ = make_app(tmp_path, ScriptedClient([]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/plan"))
    assert (tmp_path / ".mewcode" / "plans").is_dir()


@pytest.mark.asyncio
async def test_do_command_exits_plan_mode(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/plan"))
    app._handle_command(commands.parse_command("/do"))
    assert app.plan_state.active is False
    assert "已退出规划模式" in output.getvalue()


@pytest.mark.asyncio
async def test_help_lists_plan_and_do(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/help"))
    assert "/plan" in output.getvalue()
    assert "/do" in output.getvalue()


@pytest.mark.asyncio
async def test_cancel_before_ask_interrupts(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([text_turn("不该出现")]))
    app.request_cancel()
    await app._ask("你好")
    assert "已取消本次操作" in output.getvalue()
    assert "不该出现" not in output.getvalue()


@pytest.mark.asyncio
async def test_max_iterations_reported(tmp_path):
    script = [tools_turn([("c", "Glob", {"pattern": "*"})])] * 10
    app, output = make_app(tmp_path, ScriptedClient(script), max_iterations=2)
    await app._ask("持续干活")
    assert "上限" in output.getvalue()


@pytest.mark.asyncio
async def test_plan_mode_blocks_write_via_app(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([tools_turn([("c", "WriteFile", {"path": "x.py", "content": "x"})])]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/plan"))
    await app._ask("写个文件")
    assert "✗" in output.getvalue()
    assert not (tmp_path / "x.py").exists()


@pytest.mark.asyncio
async def test_plan_mode_restricts_client_tools(tmp_path):
    from mewcode.llm_client import OpenAICompatibleClient

    class FakeCompatClient(OpenAICompatibleClient):
        def __init__(self):
            self.tools = None  # 不触发网络请求，仅供 tools 断言

    client = FakeCompatClient()
    app, _ = make_app(tmp_path, client)
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/plan"))
    names = {spec["function"]["name"] for spec in client.tools}
    assert names == {"ReadFile", "Glob", "Grep"}
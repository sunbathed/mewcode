"""第七章：App / Agent 层 MCP 集成核心测试。

覆盖：启动连接并注册、/mcp 状态命令、单 Server 失败不阻塞启动、
延迟工具名进入 system-reminder。
"""

import asyncio
import io
import sys
from pathlib import Path

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.engine import Agent
from mewcode.mcp import MCPManager, MCPServerConfig
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal

from conftest import ScriptedClient, make_registry, text_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")
FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


def make_fake_config(tmp_path) -> MCPServerConfig:
    return MCPServerConfig(name="fake", command=sys.executable, args=[str(FAKE_SERVER)])


def make_app(tmp_path, client, *, mcp_manager=None, **kwargs):
    output = io.StringIO()
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client,
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=make_registry(tmp_path),
        workdir=str(tmp_path),
        mcp_manager=mcp_manager,
        **kwargs,
    )
    return app, output


@pytest.mark.asyncio
async def test_setup_mcp_connects_and_registers(tmp_path):
    manager = MCPManager(configs={"fake": make_fake_config(tmp_path)})
    app, output = make_app(tmp_path, ScriptedClient([]), mcp_manager=manager)
    await app.setup_mcp()
    text = output.getvalue()
    assert "MCP 已连接 1 个 Server" in text
    assert "注册 2 个工具" in text
    assert "mcp_fake_echo" in app.registry.names()
    assert "mcp_fake_add" in app.registry.names()
    assert app.registry.is_discovered("mcp_fake_echo") is False
    await manager.close_all()


@pytest.mark.asyncio
async def test_mcp_command_shows_status(tmp_path):
    manager = MCPManager(configs={"fake": make_fake_config(tmp_path)})
    app, output = make_app(tmp_path, ScriptedClient([]), mcp_manager=manager)
    await app.setup_mcp()
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/mcp"))
    text = output.getvalue()
    assert "已配置 1 个 MCP Server" in text
    assert "fake" in text
    assert "echo" in text
    assert "add" in text
    await manager.close_all()


@pytest.mark.asyncio
async def test_mcp_partial_failure_does_not_block_startup(tmp_path):
    """一个 Server 失败，App 仍正常启动（只打警告）。"""
    manager = MCPManager(
        configs={
            "bad": MCPServerConfig(name="bad", command="definitely-not-real-cmd-xyz"),
            "fake": make_fake_config(tmp_path),
        }
    )
    app, output = make_app(tmp_path, ScriptedClient([]), mcp_manager=manager)
    await app.setup_mcp()
    text = output.getvalue()
    assert "MCP 连接失败" in text
    assert "MCP 已连接 1 个 Server" in text
    assert "mcp_fake_echo" in app.registry.names()
    await manager.close_all()


@pytest.mark.asyncio
async def test_mcp_command_no_configs(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]))
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/mcp"))
    assert "未配置任何 MCP Server" in output.getvalue()


@pytest.mark.asyncio
async def test_deferred_names_in_system_reminder(tmp_path):
    from mewcode.tools.base import BaseTool
    from mewcode.session import Session

    class _Lazy(BaseTool):
        name = "mcp_demo_lazy"
        description = "延迟工具"
        safety = "command"

        async def _run(self, args):
            return "ok"

        def should_defer(self):
            return True

    session = Session(model="demo")
    registry = make_registry(tmp_path)
    registry.register(_Lazy())
    client = ScriptedClient([text_turn("好")])
    agent = Agent(
        PROVIDER,
        session=session,
        registry=registry,
        workdir=str(tmp_path),
        client=client,
    )
    async for _ in agent.run("hi"):
        pass
    reminder_msgs = [
        m["content"]
        for m in client.requests[0]
        if m.get("role") == "user" and m.get("content", "").startswith("<system-reminder>")
    ]
    assert any("mcp_demo_lazy" in content for content in reminder_msgs)
    api_names = {s["function"]["name"] for s in registry.api_specs()}
    assert "mcp_demo_lazy" not in api_names  # 完整 schema 不进工具列表
    assert "mcp_demo_lazy" in registry.deferred_names()
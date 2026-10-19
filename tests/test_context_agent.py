"""第八章：上下文管理与 Agent Loop / App 的集成测试。

覆盖：自动压缩事件与会话重写、撞墙后的紧急压缩（ForceCompact）、
超大工具结果的存盘提示（ToolResultEvent.persisted），以及 /compact 命令。
"""

import io

import pytest

from mewcode.app import App
from mewcode.commands import KNOWN_COMMANDS
from mewcode.config import ProviderConfig
from mewcode.context import ContextConfig, ContextManager
from mewcode.engine import Agent, AgentStopReason
from mewcode.engine.events import ContextCompactedEvent, LoopCompleteEvent, ToolResultEvent
from mewcode.llm_client import LLMError, StreamEnd, TextDelta
from mewcode.session import Session, ToolResultBlock
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools.base import BaseTool, ToolResult
from conftest import ScriptedClient, make_registry, text_turn, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def summary_turn(text: str = "用户要求梳理项目代码结构，已完成大部分模块的阅读。") -> list:
    return text_turn(f"<analysis>草稿</analysis>\n<summary>{text}</summary>")


class BigOutput(BaseTool):
    """输出超大文本的测试工具，用于触发第 1 层存盘。"""

    name = "BigOutput"
    description = "输出超大文本"
    safety = "read"

    async def _run(self, args):
        return "x" * 60_000


def make_agent_with_context(tmp_path, script, *, config=None, compact_script=None, registry=None):
    session = Session(model="demo")
    reg = registry or make_registry(tmp_path)
    reg.register(BigOutput())
    client = ScriptedClient(script)
    compact_client = ScriptedClient(compact_script or [summary_turn()])
    context = ContextManager(
        PROVIDER,
        workdir=str(tmp_path),
        config=config or ContextConfig(),
        compact_client=compact_client,
    )
    agent = Agent(
        PROVIDER,
        session=session,
        registry=reg,
        workdir=str(tmp_path),
        client=client,
        context=context,
    )
    return agent, client, session, context


async def collect(agent, text="梳理一下项目结构"):
    events = []
    async for event in agent.run(text):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# 自动压缩接入 Agent Loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_auto_compacts_and_emits_event(tmp_path):
    """三轮循环：第 3 轮请求前上下文逼近阈值 → 自动压缩并发出事件。"""
    script = [
        tools_turn([("c1", "Glob", {"pattern": "*.py"})]),
        tools_turn([("c2", "Glob", {"pattern": "*.md"})]),
        text_turn("完成"),
    ]
    config = ContextConfig(
        context_window=1_000_000,  # 阈值足够高：只靠 keep_recent=1 触发压缩（演示路径）
        keep_recent_messages=1,
    )
    agent, client, session, context = make_agent_with_context(tmp_path, script, config=config)
    # 让自动阈值小到必然触发：直接压到 0
    context.config = ContextConfig(
        context_window=1000, summary_output_reserve=0, auto_safety_margin=0, keep_recent_messages=1
    )

    events = await collect(agent)

    compacts = [e for e in events if isinstance(e, ContextCompactedEvent)]
    assert any(e.reason == "auto" for e in compacts)
    loop = [e for e in events if isinstance(e, LoopCompleteEvent)][-1]
    assert loop.stop_reason == AgentStopReason.END_TURN
    # 摘要已进入会话历史，旧消息被替换
    assert "梳理项目代码结构" in session.messages[0].content
    assert any("不要根据摘要猜测" in m.content for m in session.messages)


@pytest.mark.asyncio
async def test_agent_force_compact_on_prompt_too_long(tmp_path):
    """正常请求撞上 prompt_too_long → 紧急压缩一次 → 重试成功。"""
    script = [
        tools_turn([("c1", "Glob", {"pattern": "*.py"})]),
        tools_turn([("c2", "Glob", {"pattern": "*.md"})]),
        LLMError("Prompt is too long (context length 200000 exceeds maximum)"),
        text_turn("重试成功"),
    ]
    config = ContextConfig(keep_recent_messages=1)
    agent, client, session, _ = make_agent_with_context(tmp_path, script, config=config)

    events = await collect(agent)

    # 紧急压缩事件 + 重试成功收尾
    forces = [e for e in events if isinstance(e, ContextCompactedEvent) and e.reason == "force"]
    assert len(forces) == 1
    loop = [e for e in events if isinstance(e, LoopCompleteEvent)][-1]
    assert loop.stop_reason == AgentStopReason.END_TURN
    # 请求重发了一次：2 次工具轮 + 1 次 PTL + 1 次重试
    assert client.calls == 4
    # 重试时用的是压缩后的历史
    compact_request = "".join(
        str(m.get("content", "")) for m in client.requests[3] if isinstance(m, dict)
    )
    assert "结构化摘要" in compact_request


@pytest.mark.asyncio
async def test_force_compact_fails_only_once_then_error(tmp_path):
    """紧急压缩后仍超限：不无限重试，按正常错误流程终止。"""
    script = [
        tools_turn([("c1", "Glob", {"pattern": "*.py"})]),
        LLMError("prompt_too_long"),
        LLMError("prompt_too_long"),
    ]
    config = ContextConfig(keep_recent_messages=1)
    agent, client, session, _ = make_agent_with_context(tmp_path, script, config=config)

    events = await collect(agent)

    loop = [e for e in events if isinstance(e, LoopCompleteEvent)][-1]
    assert loop.stop_reason == AgentStopReason.INTERNAL_ERROR
    assert client.calls == 3  # PTL、压缩后重试仍 PTL，不再重试


# ---------------------------------------------------------------------------
# 第 1 层：超大工具结果存盘提示
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_tool_result_persisted_event(tmp_path):
    script = [tools_turn([("big_1", "BigOutput", {})]), text_turn("完成")]
    agent, client, session, context = make_agent_with_context(tmp_path, script)

    events = await collect(agent)

    result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert result_events[0].persisted is True
    assert "tool-results" in result_events[0].persisted_path
    # 完整内容已落盘
    from pathlib import Path

    assert Path(result_events[0].persisted_path).read_text(encoding="utf-8") == "x" * 60_000
    # 会话里只保留预览
    blocks = [
        b
        for m in session.messages
        for b in (m.blocks or [])
        if isinstance(b, ToolResultBlock)
    ]
    assert len(blocks) == 1
    assert "<persisted-output>" in blocks[0].content
    assert len(blocks[0].content) < 3_000


# ---------------------------------------------------------------------------
# App /compact 命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_command_reports_token_delta(tmp_path):
    from mewcode.session import ToolUseBlock

    session = Session(model="demo")
    session.add_message("user", "梳理代码结构")
    session.add_message("assistant", "开始", blocks=[ToolUseBlock(id="c1", name="Glob", input={"pattern": "*"})])
    session.add_tool_result("c1", "文件列表")
    session.add_message("user", "继续")
    session.add_message("assistant", "再看看")
    session.add_message("user", "再来一轮")
    session.add_message("assistant", "好")
    session.add_message("user", "还有吗")
    session.add_message("assistant", "没了")
    session.add_message("user", "继续继续")
    session.add_message("assistant", "好的")
    session.add_message("user", "收尾")

    output = io.StringIO()
    registry = make_registry(tmp_path)
    compact_client = ScriptedClient([summary_turn()])
    context = ContextManager(PROVIDER, workdir=str(tmp_path), compact_client=compact_client)
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=ScriptedClient([]),
        terminal=Terminal(input_fn=lambda _: "", output=output),
        registry=registry,
        workdir=str(tmp_path),
        context=context,
    )
    app.session = session

    await app._handle_compact_command()

    text = output.getvalue()
    assert "已压缩" in text
    assert "token" in text
    # 摘要已写回会话（App 手动压缩同样写回外层历史）
    assert "结构化摘要" in session.messages[0].content


def test_compact_is_known_command():
    assert "compact" in KNOWN_COMMANDS
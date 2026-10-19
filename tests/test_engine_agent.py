"""第四章：Agent Loop 引擎核心测试（事件流、消息拼接、停止条件、Plan Mode 集成）。

只保留核心契约：基本循环、消息协议、五种停止条件与规划模式的关键行为。
"""

import asyncio

import pytest

from mewcode.config import ProviderConfig
from mewcode.engine import Agent, AgentConfig
from mewcode.engine.events import (
    AgentStopReason,
    ErrorEvent,
    LoopCompleteEvent,
    TextChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from mewcode.engine.plan_mode import PlanModeState
from mewcode.llm_client import LLMError, StreamEnd
from mewcode.session import Session, ToolResultBlock, ToolUseBlock

from conftest import ScriptedClient, make_registry, text_turn, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_agent(
    tmp_path,
    script,
    *,
    plan_state=None,
    config=None,
    cancel_event=None,
    registry=None,
):
    session = Session(model="demo")
    reg = registry or make_registry(tmp_path)
    client = ScriptedClient(script)
    agent = Agent(
        PROVIDER,
        session=session,
        registry=reg,
        workdir=str(tmp_path),
        client=client,
        agent_config=config or AgentConfig(),
        plan_state=plan_state or PlanModeState(),
        cancel_event=cancel_event,
    )
    return agent, client, session


async def run_agent(agent, text="你好"):
    events = []
    async for event in agent.run(text):
        events.append(event)
    return events


def last_loop(events):
    return [e for e in events if isinstance(e, LoopCompleteEvent)][-1]


# ---------------------------------------------------------------------------
# 基本循环与事件流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_text_turn_ends_with_end_turn(tmp_path):
    agent, client, session = make_agent(tmp_path, [text_turn("你好，世界")])
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN
    assert loop.total_turns == 1
    assert client.calls == 1


@pytest.mark.asyncio
async def test_single_turn_event_order(tmp_path):
    agent, _, _ = make_agent(tmp_path, [text_turn("答案")])
    events = await run_agent(agent)
    kinds = [e.kind for e in events]
    assert kinds[0] == "stream_text"
    assert "usage" in kinds
    assert "turn_complete" in kinds
    assert kinds[-1] == "loop_complete"
    text = "".join(e.text for e in events if isinstance(e, TextChunkEvent))
    assert text == "答案"


@pytest.mark.asyncio
async def test_two_turn_tool_loop_end_turn(tmp_path):
    script = [
        tools_turn([("call_1", "Glob", {"pattern": "*.txt"})]),
        text_turn("完成"),
    ]
    agent, client, session = make_agent(tmp_path, script)
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN
    assert loop.total_turns == 2
    assert client.calls == 2


@pytest.mark.asyncio
async def test_tool_use_events_yielded_for_each_call(tmp_path):
    script = [tools_turn([("c1", "Glob", {"pattern": "*"}), ("c2", "Grep", {"pattern": "x"})])]
    agent, _, _ = make_agent(tmp_path, script)
    events = await run_agent(agent)
    uses = [e for e in events if isinstance(e, ToolUseEvent)]
    assert [u.name for u in uses] == ["Glob", "Grep"]
    assert [u.id for u in uses] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_tool_results_yielded_with_ids(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [tools_turn([("c1", "ReadFile", {"path": "a.txt"})])]
    agent, _, _ = make_agent(tmp_path, script)
    events = await run_agent(agent)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    assert results[0].tool_use_id == "c1"
    assert results[0].is_error is False
    assert "hello" in results[0].output


# ---------------------------------------------------------------------------
# 消息拼接协议（理论篇关键设计）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_text_and_tool_use_in_same_message(tmp_path):
    script = [tools_turn([("c1", "Glob", {"pattern": "*"})], "我先扫一眼。")]
    agent, _, session = make_agent(tmp_path, script)
    await run_agent(agent)
    assistant = [m for m in session.messages if m.role == "assistant"][0]
    blocks = assistant.blocks or []
    assert any(isinstance(b, ToolUseBlock) for b in blocks)
    assert any(getattr(b, "type", "") == "text" for b in blocks)


@pytest.mark.asyncio
async def test_tool_results_roundtrip_with_matching_ids(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [
        tools_turn([("c1", "ReadFile", {"path": "a.txt"}), ("c2", "Glob", {"pattern": "*.txt"})]),
        text_turn("好了"),
    ]
    agent, client, session = make_agent(tmp_path, script)
    await run_agent(agent)
    tool_messages = [m for m in client.requests[1] if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_messages} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_session_persists_blocks_and_restores(tmp_path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    script = [tools_turn([("c1", "ReadFile", {"path": "a.txt"})]), text_turn("完成")]
    agent, _, session = make_agent(tmp_path, script)
    await run_agent(agent)
    restored = Session.from_dict(session.to_dict())
    assert any(isinstance(b, ToolUseBlock) for m in restored.messages for b in (m.blocks or []))
    assert any(isinstance(b, ToolResultBlock) for m in restored.messages for b in (m.blocks or []))


@pytest.mark.asyncio
async def test_request_messages_include_system_prompt(tmp_path):
    agent, client, _ = make_agent(tmp_path, [text_turn("好")])
    await run_agent(agent)
    assert client.requests[0][0]["role"] == "system"
    assert "MewCode" in client.requests[0][0]["content"]


# ---------------------------------------------------------------------------
# 五种停止条件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_stop(tmp_path):
    script = [tools_turn([("c", "Glob", {"pattern": "*"})])] * 10
    agent, client, _ = make_agent(tmp_path, script, config=AgentConfig(max_iterations=2))
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.MAX_ITERATIONS
    assert loop.total_turns == 2


@pytest.mark.asyncio
async def test_cancel_before_run(tmp_path):
    cancel = asyncio.Event()
    cancel.set()
    agent, client, _ = make_agent(tmp_path, [text_turn("不该出现")], cancel_event=cancel)
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.USER_CANCELLED
    assert loop.total_turns == 0
    assert client.calls == 0


@pytest.mark.asyncio
async def test_timeout_stop(tmp_path):
    script = [tools_turn([("c", "DelayRead", {"delay": 0.1, "tag": "x"})])]
    agent, _, _ = make_agent(tmp_path, script, config=AgentConfig(timeout=0.05))
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.TIMEOUT
    assert loop.total_turns == 1


@pytest.mark.asyncio
async def test_tool_error_limit_stop(tmp_path):
    script = [
        tools_turn([("c1", "NoSuchTool", {})]),
        tools_turn([("c2", "NoSuchTool", {})]),
        tools_turn([("c3", "NoSuchTool", {})]),
    ]
    agent, _, _ = make_agent(tmp_path, script, config=AgentConfig(max_tool_error_turns=3))
    events = await run_agent(agent)
    assert any(isinstance(e, ErrorEvent) for e in events)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.TOOL_ERROR_LIMIT
    assert loop.total_turns == 3


@pytest.mark.asyncio
async def test_llm_error_stops_with_internal_error(tmp_path):
    agent, client, _ = make_agent(tmp_path, [LLMError("模型不可用")])
    events = await run_agent(agent)
    assert any(isinstance(e, ErrorEvent) and "模型不可用" in e.message for e in events)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Plan Mode 集成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_mode_blocks_regular_write(tmp_path):
    plan_state = PlanModeState()
    plan_state.enter(str(tmp_path))
    script = [tools_turn([("c", "WriteFile", {"path": "src/app.py", "content": "x"})])]
    agent, _, _ = make_agent(tmp_path, script, plan_state=plan_state)
    events = await run_agent(agent)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results[0].is_error
    assert "规划模式" in results[0].error
    assert not (tmp_path / "src" / "app.py").exists()


@pytest.mark.asyncio
async def test_plan_mode_allows_plan_file_write(tmp_path):
    plan_state = PlanModeState()
    plan_file = plan_state.enter(str(tmp_path))
    script = [tools_turn([("c", "WriteFile", {"path": plan_file, "content": "# 开发计划"})])]
    agent, _, _ = make_agent(tmp_path, script, plan_state=plan_state)
    events = await run_agent(agent)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert not results[0].is_error
    from pathlib import Path

    assert Path(plan_file).read_text(encoding="utf-8") == "# 开发计划"


@pytest.mark.asyncio
async def test_plan_mode_restricts_tools_for_request(tmp_path):
    plan_state = PlanModeState()
    plan_state.enter(str(tmp_path))
    agent, _, _ = make_agent(tmp_path, [], plan_state=plan_state)
    names = {spec["function"]["name"] for spec in agent._tools_for_request()}
    assert names == {"ReadFile", "Glob", "Grep"}


@pytest.mark.asyncio
async def test_plan_mode_injects_instructions_into_system_prompt(tmp_path):
    plan_state = PlanModeState()
    plan_state.enter(str(tmp_path))
    agent, client, _ = make_agent(tmp_path, [text_turn("计划")], plan_state=plan_state)
    await run_agent(agent)
    system_content = client.requests[0][0]["content"]
    assert "Plan mode is active" not in system_content
    reminder_messages = [
        m["content"]
        for m in client.requests[0]
        if m.get("role") == "user" and m.get("content", "").startswith("<system-reminder>")
    ]
    assert len(reminder_messages) == 1
    assert "规划模式" in reminder_messages[0] or "plan" in reminder_messages[0].lower()


# ---------------------------------------------------------------------------
# 细节行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_model_response_still_ends(tmp_path):
    script = [[StreamEnd("stop")]]
    agent, _, session = make_agent(tmp_path, script)
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN
    assert session.messages[-1].role == "user"


@pytest.mark.asyncio
async def test_usage_events_accumulate(tmp_path):
    script = [
        tools_turn([("c", "Glob", {"pattern": "*"})], "第一步"),
        text_turn("第二步"),
    ]
    agent, _, _ = make_agent(tmp_path, script)
    events = await run_agent(agent)
    usages = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usages) == 2
    assert usages[0].total_output_tokens <= usages[1].total_output_tokens
    assert usages[1].total_input_tokens >= usages[0].input_tokens

"""第六章：权限系统与 Agent Loop 集成核心测试。

核心契约：
- 权限检查发生在工具执行之前；拒绝返回 errorResult 但 Agent Loop 继续运行；
- ASK 触发 HITL：发 PermissionRequestEvent 阻塞等待，UI resolve 后继续；
- 「始终允许」自动追加 allow 规则，形成权限学习循环；
- 黑名单/沙箱在 bypassPermissions 模式下仍然生效；
- 权限拒绝不触发 TOOL_ERROR_LIMIT（模型有机会调整策略）。
"""

import pytest

from mewcode.config import ProviderConfig
from mewcode.engine import Agent, AgentConfig, PermissionRequestEvent
from mewcode.engine.events import AgentStopReason, LoopCompleteEvent, ToolResultEvent
from mewcode.permission import PermissionChecker, PermissionResponse
from mewcode.session import Session

from conftest import ScriptedClient, make_registry, text_turn, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_agent(
    tmp_path,
    script,
    *,
    permission=None,
    agent_config=None,
):
    session = Session(model="demo")
    reg = make_registry(tmp_path)
    client = ScriptedClient(script)
    agent = Agent(
        PROVIDER,
        session=session,
        registry=reg,
        workdir=str(tmp_path),
        client=client,
        agent_config=agent_config or AgentConfig(),
        permission=permission,
    )
    return agent, client, session


async def run_agent(agent, text="你好"):
    events = []
    async for event in agent.run(text):
        events.append(event)
    return events


def last_loop(events):
    return [e for e in events if isinstance(e, LoopCompleteEvent)][-1]


def results_of(events):
    return [e for e in events if isinstance(e, ToolResultEvent)]


@pytest.mark.asyncio
async def test_dangerous_command_denied_but_loop_continues(tmp_path):
    """黑名单拦截 rm -rf /，但 Agent Loop 不终止——模型可以换策略。"""
    checker = PermissionChecker(workdir=str(tmp_path))
    script = [
        tools_turn([("c1", "Bash", {"command": "rm -rf /"})]),
        text_turn("好的，我改用安全方式清理。"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = await run_agent(agent)
    results = results_of(events)
    assert results[0].is_error
    assert "危险命令" in results[0].error
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN
    assert loop.total_turns == 2
    assert client.calls == 2


@pytest.mark.asyncio
async def test_sandbox_blocks_outside_read(tmp_path):
    checker = PermissionChecker(workdir=str(tmp_path))
    script = [
        tools_turn([("c1", "ReadFile", {"path": "/etc/passwd"})]),
        text_turn("已放弃读取外部文件。"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = await run_agent(agent)
    results = results_of(events)
    assert results[0].is_error
    assert "沙箱" in results[0].error
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN


@pytest.mark.asyncio
async def test_sandbox_allows_inside_read(tmp_path):
    (tmp_path / "ok.txt").write_text("hi", encoding="utf-8")
    checker = PermissionChecker(workdir=str(tmp_path))
    script = [
        tools_turn([("c1", "ReadFile", {"path": "ok.txt"})]),
        text_turn("已读取"),
    ]
    agent, _, session = make_agent(tmp_path, script, permission=checker)
    events = await run_agent(agent)
    results = results_of(events)
    assert not results[0].is_error
    assert "hi" in results[0].output


@pytest.mark.asyncio
async def test_deny_rule_blocks_env_read(tmp_path):
    (tmp_path / ".env").write_text("SECRET=123", encoding="utf-8")
    (tmp_path / ".mewcode").mkdir()
    (tmp_path / ".mewcode" / "permissions.yaml").write_text(
        "- rule: ReadFile(*.env*)\n  effect: deny\n", encoding="utf-8"
    )
    checker = PermissionChecker(workdir=str(tmp_path))
    script = [
        tools_turn([("c1", "ReadFile", {"path": ".env"})]),
        text_turn("我不读取环境变量文件。"),
    ]
    agent, _, _ = make_agent(tmp_path, script, permission=checker)
    events = await run_agent(agent)
    results = results_of(events)
    assert results[0].is_error
    assert "权限规则" in results[0].error


@pytest.mark.asyncio
async def test_default_mode_ask_requires_hitl(tmp_path):
    """default 模式写文件触发 ASK -> HITL 确认 -> 放行。"""
    checker = PermissionChecker(workdir=str(tmp_path), mode="default")
    script = [tools_turn([("c1", "WriteFile", {"path": "new.txt", "content": "1"})])]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = []
    iterator = agent.run("写入")

    async for event in iterator:
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            event.response_future.set_result(PermissionResponse.ALLOW)  # 模拟 UI 按 y

    results = results_of(events)
    assert not results[0].is_error
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "1"
    local_file = tmp_path / ".mewcode" / "permissions.local.yaml"
    assert not local_file.exists() or "new.txt" not in local_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_hitl_deny_blocks_execution(tmp_path):
    """HITL 用户按 n 拒绝：工具不执行，返回错误结果，循环继续。"""
    checker = PermissionChecker(workdir=str(tmp_path), mode="default")
    script = [
        tools_turn([("c1", "WriteFile", {"path": "blocked.txt", "content": "x"})]),
        text_turn("用户拒绝了，我不写了。"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = []
    iterator = agent.run("写入")

    async for event in iterator:
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            event.response_future.set_result(PermissionResponse.DENY)

    results = results_of(events)
    assert results[0].is_error
    assert "用户拒绝" in results[0].error
    assert not (tmp_path / "blocked.txt").exists()
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN


@pytest.mark.asyncio
async def test_always_allow_appends_rule(tmp_path):
    """HITL 用户选「始终允许」：自动追加 allow 规则，后续同类操作自动放行。"""
    checker = PermissionChecker(workdir=str(tmp_path), mode="default")
    script = [
        tools_turn([("c1", "Bash", {"command": "echo first"})]),
        tools_turn([("c2", "Bash", {"command": "echo first message here"})]),
        text_turn("两次 echo 都放行了。"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = []
    iterator = agent.run("跑命令")

    async for event in iterator:
        events.append(event)
        if isinstance(event, PermissionRequestEvent):
            if event.request_id == "c1":
                event.response_future.set_result(PermissionResponse.ALLOW_ALWAYS)
            else:
                # 第二次同类操作：校验已生成 allow 规则，直接放行且不再弹窗
                event.response_future.set_result(PermissionResponse.DENY)

    local_file = tmp_path / ".mewcode" / "permissions.local.yaml"
    content = local_file.read_text(encoding="utf-8")
    assert "Bash(echo first *)" in content
    results = results_of(events)
    assert not results[1].is_error, f"第二条应被自动放行，实际错误：{results[1].error}"
    assert "first message here" in results[1].output
    requests = [e for e in events if isinstance(e, PermissionRequestEvent)]
    assert len(requests) == 1  # 第二次不再弹窗


@pytest.mark.asyncio
async def test_accept_edits_write_no_ask(tmp_path):
    checker = PermissionChecker(workdir=str(tmp_path), mode="acceptEdits")
    script = [
        tools_turn([("c1", "WriteFile", {"path": "edit.txt", "content": "1"})]),
        text_turn("完成"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker)
    events = await run_agent(agent)
    assert not any(isinstance(e, PermissionRequestEvent) for e in events)
    results = results_of(events)
    assert not results[0].is_error
    assert (tmp_path / "edit.txt").read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_no_permission_checker_keeps_old_behavior(tmp_path):
    """不传 permission 时，Agent 行为与第四章完全一致（无任何拦截）。"""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [
        tools_turn([("c1", "WriteFile", {"path": "w.txt", "content": "1"})]),
        text_turn("完成"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=None)
    events = await run_agent(agent)
    assert not any(isinstance(e, PermissionRequestEvent) for e in events)
    results = results_of(events)
    assert not results[0].is_error
    assert (tmp_path / "w.txt").read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_permission_denials_do_not_count_as_tool_errors(tmp_path):
    """连续权限拒绝不应触发 TOOL_ERROR_LIMIT——模型应该有机会调整策略。"""
    checker = PermissionChecker(workdir=str(tmp_path), mode="default")
    script = [
        tools_turn([("c1", "Bash", {"command": "rm -rf /"})]),
        tools_turn([("c2", "Bash", {"command": "mkfs.ext4 /dev/sda"})]),
        text_turn("都不行，我放弃了。"),
    ]
    agent, client, session = make_agent(tmp_path, script, permission=checker, agent_config=AgentConfig(max_tool_error_turns=1))
    events = await run_agent(agent)
    loop = last_loop(events)
    assert loop.stop_reason == AgentStopReason.END_TURN
    assert loop.total_turns == 3
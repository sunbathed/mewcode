"""第 13 章：SubAgent 系统集成测试（App / AgentTool / 验收场景）。

验收场景（对应实战文档）：
  1. 自定义 security-reviewer 子 Agent（项目 .mewcode/agents/ 定义）
     → 通过 AgentTool 前台运行，返回审查结果
  2. 内置 Explore 子 Agent 可用
  3. Fork 路径继承父对话、无条件后台运行
  4. 子 Agent 工具过滤生效（不能调 Agent 工具 → 防嵌套）
"""

import io
import json

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.engine.agent import Agent, AgentConfig
from mewcode.hooks import (
    Action,
    Hook,
    HookContext,
    HookEngine,
)
from mewcode.hooks.models import LifecycleEvent as LE
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools import ToolRegistry

from conftest import ScriptedClient

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client=None):
    output = io.StringIO()
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client or ScriptedClient([]),
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=ToolRegistry(workdir=str(tmp_path)),
        workdir=str(tmp_path),
        permission_mode="bypassPermissions",
    )
    return app, output


def make_security_reviewer(tmp_path):
    """在项目 .mewcode/agents/ 下创建自定义 security-reviewer 定义。"""
    config_dir = tmp_path / ".mewcode" / "agents"
    config_dir.mkdir(parents=True)
    (config_dir / "security-reviewer.md").write_text(
        "---\n"
        "name: security-reviewer\n"
        "description: 代码安全审查专家\n"
        "model: sonnet\n"
        "maxTurns: 20\n"
        "permissionMode: bypassPermissions\n"
        "disallowedTools:\n"
        "  - Agent\n"
        "  - EditFile\n"
        "  - WriteFile\n"
        "  - Bash\n"
        "---\n\n"
        "你是一个专注于代码安全审查的 Agent，只读模式。\n"
        "只读取代码，不修改任何文件。\n",
        encoding="utf-8",
    )


class SecurityReviewClient:
    """子 Agent 的第一轮（也是唯一轮）：纯文本报告。"""

    def __init__(self, report="未发现已知模式的漏洞"):
        self.requests = []
        self.calls = 0
        self.report = report

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        yield TextDelta(self.report)
        yield StreamEnd("stop")


# ---------------------------------------------------------------------------
# 验收场景 1：自定义 security-reviewer 前台运行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defined_agent_frontend_runs(tmp_path, monkeypatch):
    """自定义子 Agent 定义从项目 .mewcode/agents/ 加载并前台执行。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    make_security_reviewer(tmp_path)
    client = SecurityReviewClient("代码审查完成，发现 2 个 HIGH 漏洞")
    app, _ = make_app(tmp_path, client)

    # 定义已加载
    assert app.agent_loader.get("security-reviewer") is not None
    assert "security-reviewer" in app.agent_loader.available_types_text()

    # 前台调用 AgentTool（model 覆盖为 inherit 复用宿主的 fake client）
    result = await app.agent_tool.run(
        {
            "prompt": "审查 internal/permissions/permissions.go",
            "description": "代码安全审查",
            "subagent_type": "security-reviewer",
            "model": "inherit",
        }
    )
    assert result.success
    assert "代码审查完成" in result.output
    # 子 Agent 的系统提示（定义 body）注入了请求
    system_prompt = client.requests[0][0]["content"]
    assert "代码安全审查" in system_prompt


@pytest.mark.asyncio
async def test_defined_agent_unknown_type(tmp_path):
    """未知 agent_type：错误信息列出可用类型（帮助 LLM 自我纠正）。"""
    app, _ = make_app(tmp_path)
    result = await app.agent_tool.run(
        {
            "prompt": "任务",
            "description": "d",
            "subagent_type": "no-such-agent",
        }
    )
    assert not result.success
    assert "Unknown agent type" in (result.output or result.error)
    assert "explore" in (result.output or result.error)


# ---------------------------------------------------------------------------
# 验收场景 2：内置 Explore 子 Agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builtin_explore_runs(tmp_path):
    """内置 Explore 子 Agent 前台执行，只读探索返回结果。"""
    client = SecurityReviewClient("项目中共有 12 个 Go 文件")
    app, _ = make_app(tmp_path, client)
    result = await app.agent_tool.run(
        {
            "prompt": "搜一下项目里有哪些 Go 文件",
            "description": "代码探索",
            "subagent_type": "explore",
            "model": "inherit",
        }
    )
    assert result.success
    assert "12 个 Go 文件" in result.output
    # 子 Agent 的读取工具可用（Explore 有 ReadFile/Glob/Grep）
    assert client.calls == 1


# ---------------------------------------------------------------------------
# 验收场景 3：Fork 路径（后台）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_launches_background(tmp_path):
    """Fork 路径：无条件后台运行，返回 Task ID，不等待结果。"""
    client = ScriptedClient([])
    app, _ = make_app(tmp_path, client)
    # 准备一个父会话（有对话历史可继承）
    app.session.add_message("user", "我们在重构模块")

    result = await app.agent_tool.run(
        {
            "prompt": "顺便写个单元测试",
            "description": "补充测试",
            "name": "写测试的fork",
        }
    )
    assert result.success
    assert "background" in result.output
    assert "Task ID:" in result.output
    # 任务被登记到 TaskManager 且是 Fork 路径
    task_id = [
        line.split(":")[1].strip() for line in result.output.splitlines() if "Task ID:" in line
    ][0]
    task = app.task_manager.get(task_id)
    assert task is not None
    assert task.name == "写测试的fork"
    # Fork 会话继承父对话：子 Agent 的会话含 Boilerplate 与父历史
    assert task.agent.session.messages[0].content.startswith("<fork_boilerplate>")
    assert app.trace.list_nodes()  # 有追踪记录
    # 让后台任务跑完再清理
    for _ in range(50):
        if app.task_manager.poll_completed():
            break
        import asyncio

        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 工具过滤：子 Agent 不能调 Agent 工具（防嵌套）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_cannot_nest(tmp_path):
    """子 Agent 的工具集不含 Agent 工具（全局黑名单生效，防无限嵌套）。"""
    app, _ = make_app(tmp_path)
    definition = app.agent_loader.get("general-purpose")
    from mewcode.subagent import resolve_agent_tools

    filtered = resolve_agent_tools(app.registry, definition, is_background=False)
    assert "Agent" not in filtered.names()


@pytest.mark.asyncio
async def test_security_reviewer_no_write_tools(tmp_path, monkeypatch):
    """security-reviewer 的 disallowedTools 黑名单生效（只读专家）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    make_security_reviewer(tmp_path)
    app, _ = make_app(tmp_path)
    definition = app.agent_loader.get("security-reviewer")
    from mewcode.subagent import resolve_agent_tools

    filtered = resolve_agent_tools(app.registry, definition, is_background=False)
    names = set(filtered.names())
    assert "EditFile" not in names
    assert "WriteFile" not in names
    assert "Bash" not in names
    assert "Agent" not in names


# ---------------------------------------------------------------------------
# /tasks 命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_command_lists(tmp_path):
    """/tasks 命令：列出后台任务与子 Agent 调用链。"""
    app, output = make_app(tmp_path)
    # 先产生一个后台任务
    await app.agent_tool.run(
        {
            "prompt": "写计划",
            "description": "计划",
            "subagent_type": "plan",
            "run_in_background": True,
        }
    )
    from mewcode.commands import parse_command

    app._handle_command(parse_command("/tasks"))
    text = output.getvalue()
    assert "后台任务" in text
    assert "调用链" in text or "暂无子 Agent" in text


@pytest.mark.asyncio
async def test_task_command_info_cancel(tmp_path):
    """/task info / /task cancel：后台任务详情与取消。"""
    app, output = make_app(tmp_path)
    result = await app.agent_tool.run(
        {
            "prompt": "任务",
            "description": "d",
            "subagent_type": "general-purpose",
            "run_in_background": True,
        }
    )
    task_id = [line.split(":")[1].strip() for line in result.output.splitlines() if "Task ID:" in line][0]
    from mewcode.commands import parse_command

    app._handle_command(parse_command(f"/task info {task_id}"))
    text = output.getvalue()
    assert task_id in text
    app._handle_command(parse_command(f"/task cancel {task_id}"))
    assert "已请求取消" in output.getvalue() or "已结束" in output.getvalue()


# ---------------------------------------------------------------------------
# run_to_completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_to_completion_returns_last_text(tmp_path):
    """run_to_completion：任务注入后跑到底，返回最后文本。"""
    app, _ = make_app(tmp_path)
    session = app.session  # 复用
    agent = Agent(
        PROVIDER,
        session=session,
        registry=app.registry,
        workdir=str(tmp_path),
        client=SecurityReviewClient("子任务完成，共修改 2 个文件"),
        agent_config=AgentConfig(max_iterations=5),
        permission=app.permission,
    )
    session.add_message("user", "已有内容")
    result = await agent.run_to_completion("执行子任务")
    assert result == "子任务完成，共修改 2 个文件"


@pytest.mark.asyncio
async def test_hooks_apply_to_subagent_rejection(tmp_path):
    """Hook 在子 Agent 中仍然生效：pre_tool_use 拦截仍会拒绝。"""
    from mewcode.subagent import AgentTool, AgentLoader, TaskManager, TraceRegistry

    class HookedClient:
        """子 Agent 请求调用 WriteFile（但 Hook 会拦截）。"""

        def __init__(self):
            self.requests = []
            self.calls = 0

        async def stream(self, messages):
            self.requests.append(messages)
            self.calls += 1
            if self.calls == 1:
                yield ToolUseStart(index=0, id="call1", name="WriteFile")
                raw = json.dumps({"path": "config.json", "content": "{}"})
                for i in range(0, len(raw), 2):
                    yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
                yield StreamEnd("tool_calls")
            else:
                yield TextDelta("文件被拦截，我改用其他方式。")
                yield StreamEnd("stop")

    hooks = HookEngine(
        [
            Hook(
                id="block-json",
                event="pre_tool_use",
                action=Action(type="command", command='echo "禁止写 JSON"'),
                reject=True,
            )
        ]
    )
    app, _ = make_app(tmp_path, HookedClient())
    app.hooks = hooks
    client = HookedClient()
    app.client = client
    # 子 Agent 共享父 Hook 引擎
    agent = Agent(
        PROVIDER,
        session=app.session,
        registry=app.registry,
        workdir=str(tmp_path),
        client=client,
        agent_config=AgentConfig(max_iterations=5),
        permission=app.permission,
        hooks=hooks,
    )
    result = await agent.run_to_completion("写 config.json")
    assert "文件被拦截" in result
    assert not (tmp_path / "config.json").exists()
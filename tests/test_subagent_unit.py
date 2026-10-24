"""第 13 章：SubAgent 系统单元测试（解析 / 加载 / 过滤 / Fork / 任务 / 追踪）。"""

import asyncio
import json

import pytest

from mewcode.subagent import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    AgentDef,
    AgentLoader,
    AgentParseError,
    BackgroundTask,
    CUSTOM_AGENT_DISALLOWED_TOOLS,
    FORK_BOILERPLATE,
    FORK_BOILERPLATE_TAG,
    ForkError,
    TaskManager,
    TraceRegistry,
    build_forked_messages,
    parse_agent_file,
    resolve_agent_tools,
)
from mewcode.subagent.task_tools import TaskGetTool, TaskListTool
from mewcode.tools import ToolRegistry

SAMPLE = """---
name: security-reviewer
description: 代码安全审查专家
model: sonnet
maxTurns: 20
permissionMode: dontAsk
disallowedTools:
  - Agent
  - EditFile
  - WriteFile
  - Bash
---

你是一个专注于代码安全审查的 Agent，只读模式。
"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_agent_file(tmp_path):
    path = tmp_path / "security-reviewer.md"
    path.write_text(SAMPLE, encoding="utf-8")
    definition = parse_agent_file(path)
    assert definition.agent_type == "security-reviewer"
    assert definition.when_to_use == "代码安全审查专家"
    assert definition.model == "sonnet"
    assert definition.max_turns == 20
    assert definition.permission_mode == "dontAsk"
    assert definition.disallowed_tools == ["Agent", "EditFile", "WriteFile", "Bash"]
    assert "只读模式" in definition.system_prompt
    assert definition.source == "builtin"  # 默认来源


def test_parse_agent_file_missing_frontmatter(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("没有 frontmatter", encoding="utf-8")
    with pytest.raises(AgentParseError, match="frontmatter"):
        parse_agent_file(path)


def test_parse_agent_file_missing_required(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("---\ndescription: 没有名字\n---\nbody", encoding="utf-8")
    with pytest.raises(AgentParseError, match="name"):
        parse_agent_file(path)


def test_parse_agent_file_invalid_model(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\nname: x\ndescription: d\nmodel: gpt-99\n---\nbody", encoding="utf-8"
    )
    with pytest.raises(AgentParseError, match="model"):
        parse_agent_file(path)


def test_parse_agent_file_invalid_permission_mode(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\nname: x\ndescription: d\npermissionMode: weird\n---\nbody", encoding="utf-8"
    )
    with pytest.raises(AgentParseError, match="permissionMode"):
        parse_agent_file(path)


def test_parse_agent_file_defaults(tmp_path):
    path = tmp_path / "gp.md"
    path.write_text(
        "---\nname: general-purpose\ndescription: 通用\n---\nbody", encoding="utf-8"
    )
    definition = parse_agent_file(path)
    assert definition.model == "inherit"
    assert definition.max_turns == 50
    assert definition.permission_mode == "default"
    assert definition.background is False


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def test_loader_builtin_agents(tmp_path):
    loader = AgentLoader(tmp_path)
    types = loader.available_types_text()
    assert "explore" in types
    assert "plan" in types
    assert "general-purpose" in types


def test_project_def_overrides_builtin(tmp_path, monkeypatch):
    """项目级定义覆盖同名的内置定义（先到先得）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_dir = tmp_path / ".mewcode" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "explore.md").write_text(
        "---\nname: explore\ndescription: 项目定制版探索\n---\n项目版 SOP",
        encoding="utf-8",
    )
    loader = AgentLoader(tmp_path)
    definition = loader.get("explore")
    assert definition.when_to_use == "项目定制版探索"
    assert definition.source == "project"


def test_user_def_overrides_builtin_but_not_project(tmp_path, monkeypatch):
    """用户级覆盖内置，但不覆盖项目级。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_dir = tmp_path / ".mewcode" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "explore.md").write_text(
        "---\nname: explore\ndescription: 项目版\n---\nP", encoding="utf-8"
    )
    user_dir = tmp_path / "home" / ".mewcode" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "explore.md").write_text(
        "---\nname: explore\ndescription: 用户版\n---\nU", encoding="utf-8"
    )
    loader = AgentLoader(tmp_path)
    assert loader.get("explore").when_to_use == "项目版"
    assert loader.get("explore").source == "project"


def test_hot_reload_picks_up_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_dir = tmp_path / ".mewcode" / "agents"
    project_dir.mkdir(parents=True)
    path = project_dir / "reviewer.md"
    path.write_text(
        "---\nname: reviewer\ndescription: 旧描述\n---\n旧", encoding="utf-8"
    )
    loader = AgentLoader(tmp_path)
    assert loader.get("reviewer").when_to_use == "旧描述"
    path.write_text(
        "---\nname: reviewer\ndescription: 新描述\n---\n新", encoding="utf-8"
    )
    assert loader.get("reviewer").when_to_use == "新描述"


def test_bad_file_falls_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_dir = tmp_path / ".mewcode" / "agents"
    project_dir.mkdir(parents=True)
    path = project_dir / "reviewer.md"
    path.write_text(
        "---\nname: reviewer\ndescription: 描述\n---\n内容", encoding="utf-8"
    )
    loader = AgentLoader(tmp_path)
    assert loader.get("reviewer").system_prompt == "内容"
    path.write_text("{刚被改坏了", encoding="utf-8")
    # 解析失败回退缓存
    assert loader.get("reviewer").system_prompt == "内容"


# ---------------------------------------------------------------------------
# 工具过滤
# ---------------------------------------------------------------------------


def make_registry(tmp_path):
    return ToolRegistry(workdir=str(tmp_path))


def test_global_disallowed_tools_contains_agent():
    assert "Agent" in ALL_AGENT_DISALLOWED_TOOLS
    assert "AskUserQuestion" in ALL_AGENT_DISALLOWED_TOOLS
    assert "TaskStop" in ALL_AGENT_DISALLOWED_TOOLS


def test_custom_disallowed_same_content():
    assert CUSTOM_AGENT_DISALLOWED_TOOLS == ALL_AGENT_DISALLOWED_TOOLS


def test_background_whitelist_excludes_agent():
    assert "Agent" not in ASYNC_AGENT_ALLOWED_TOOLS
    assert "ReadFile" in ASYNC_AGENT_ALLOWED_TOOLS


def test_foreground_full_tools(tmp_path):
    registry = make_registry(tmp_path)
    definition = AgentDef(agent_type="gp", when_to_use="通用")
    filtered = resolve_agent_tools(registry, definition, is_background=False)
    assert "ReadFile" in filtered.names()
    assert "Bash" in filtered.names()


def test_disallowed_tools_removes(tmp_path):
    registry = make_registry(tmp_path)
    definition = AgentDef(
        agent_type="ro", when_to_use="只读", disallowed_tools=["WriteFile", "EditFile"]
    )
    filtered = resolve_agent_tools(registry, definition, is_background=False)
    assert "WriteFile" not in filtered.names()
    assert "EditFile" not in filtered.names()
    assert "ReadFile" in filtered.names()


def test_tools_whitelist_intersects(tmp_path):
    registry = make_registry(tmp_path)
    definition = AgentDef(agent_type="narrow", when_to_use="窄", tools=["ReadFile", "Glob"])
    filtered = resolve_agent_tools(registry, definition, is_background=False)
    names = set(filtered.names())
    assert {"ReadFile", "Glob"} <= names
    assert "Bash" not in names


def test_background_whitelist_applied(tmp_path):
    registry = make_registry(tmp_path)
    definition = AgentDef(agent_type="bg", when_to_use="后台")
    filtered = resolve_agent_tools(registry, definition, is_background=True)
    names = set(filtered.names())
    # 后台白名单内的工具可用
    assert "ReadFile" in names
    assert "Bash" in names or "ToolSearch" in names
    # 白名单外的工具被移除
    assert "Agent" not in names


def test_mcp_tools_pass_through(tmp_path):
    """MCP 工具（mcp__ 前缀）不参与过滤，始终可用。"""
    from mewcode.mcp.wrapper import MCPToolWrapper

    registry = make_registry(tmp_path)

    class DummyMCPClient:
        async def call_tool(self, name, args):
            return {"content": [{"type": "text", "text": "x"}]}

    wrapper = MCPToolWrapper(
        DummyMCPClient(),
        server_name="github",
        tool_def={"name": "search", "description": "搜索", "inputSchema": {"type": "object"}},
    )
    registry.register(wrapper)
    definition = AgentDef(agent_type="gp", when_to_use="通用")
    filtered = resolve_agent_tools(registry, definition, is_background=False)
    assert "mcp_github_search" in filtered.names()


# ---------------------------------------------------------------------------
# Fork 路径
# ---------------------------------------------------------------------------


def test_fork_builds_boilerplate_and_task(tmp_path):
    from mewcode.session import Session

    session = Session()
    session.add_message("user", "帮我重构一下")
    forked = build_forked_messages(session, "顺便写个测试")
    contents = [msg.content for msg in forked.messages]
    assert any(FORK_BOILERPLATE_TAG in c for c in contents)
    assert forked.messages[-1].content == "顺便写个测试"
    # 父对话历史被继承
    assert any("帮我重构一下" == c for c in contents)


def test_fork_nesting_rejected(tmp_path):
    from mewcode.session import Session

    session = Session()
    session.add_message("user", f"{FORK_BOILERPLATE_TAG}\n你是 fork 进程")
    with pytest.raises(ForkError, match="nesting"):
        build_forked_messages(session, "任务")


def test_fork_completes_interrupted_tool_use(tmp_path):
    """父会话末尾有未完成 tool_use 时，用 interrupted 占位符补全。"""
    from mewcode.session import Session, ToolResultBlock, ToolUseBlock

    session = Session()
    session.add_message("user", "任务")
    session.add_message(
        "assistant",
        "准备写文件",
        blocks=[ToolUseBlock(id="call_1", name="WriteFile", input={"path": "a.txt"})],
    )
    forked = build_forked_messages(session, "新任务")
    # 最后一条（追加任务之前）应有 user 消息携带 interrupted tool_result
    last = forked.messages[-2]
    assert last.role == "user"
    tool_results = [b for b in last.blocks or [] if getattr(b, "type", "") == "tool_result"]
    assert tool_results and tool_results[0].tool_use_id == "call_1"
    assert tool_results[0].content == "interrupted"


# ---------------------------------------------------------------------------
# TaskManager
# ---------------------------------------------------------------------------


class DummyAgent:
    """最小假 Agent：run_to_completion 返回固定结果。"""

    total_input_tokens = 10
    total_output_tokens = 5

    async def run_to_completion(self, task, *, fork_conversation=None):
        if fork_conversation is not None:
            return "fork-result"
        return f"done:{task}"


async def test_task_manager_launch_and_complete():
    manager = TaskManager()
    task_id = manager.launch(DummyAgent(), "写测试")
    assert len(task_id) == 8  # UUID 前 8 位
    task = manager.get(task_id)
    assert task is not None
    assert task.status == "running"
    # 等待后台完成
    for _ in range(100):
        completed = manager.poll_completed()
        if completed:
            break
        await asyncio.sleep(0.02)
    assert completed and completed[0].id == task_id
    assert completed[0].result == "done:写测试"
    assert completed[0].status == "completed"
    assert completed[0].end_time is not None
    assert completed[0].progress.input_tokens == 10


async def test_task_manager_fork_conversation():
    """Fork 路径：对话已构建好，run_to_completion 不再追加任务。"""
    manager = TaskManager()
    task_id = manager.launch(DummyAgent(), "", fork_conversation=object())
    for _ in range(100):
        completed = manager.poll_completed()
        if completed:
            break
        await asyncio.sleep(0.02)
    assert completed[0].result == "fork-result"


async def test_task_manager_cancel():
    manager = TaskManager()
    task_id = manager.launch(DummyAgent(), "任务")
    assert manager.cancel(task_id) is True
    assert manager.cancel("nonexistent") is False
    manager.shutdown()


async def test_task_list_get_tools():
    """TaskList / TaskGet 工具查询后台任务。"""
    manager = TaskManager()
    task_id = manager.launch(DummyAgent(), "任务A", name="我的任务")
    list_tool = TaskListTool(manager)
    result = await list_tool.run({})
    assert "我的任务" in result.output
    get_tool = TaskGetTool(manager)
    result = await get_tool.run({"task_id": task_id})
    assert task_id in result.output
    result = await get_tool.run({"task_id": "nope"})
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# TraceRegistry
# ---------------------------------------------------------------------------


async def test_trace_tree():
    trace = TraceRegistry()
    parent_id = trace.start_agent(agent_type="explore", name="探索")
    child_id = trace.start_agent(parent_id=parent_id, agent_type="fork", name="分叉")
    trace.complete_agent(parent_id, status="completed", tool_calls=5, input_tokens=100, output_tokens=50)
    trace.complete_agent(child_id, status="failed")
    text = trace.tree_text()
    assert "探索" in text
    assert "分叉" in text
    assert "completed" in text
    assert "failed" in text
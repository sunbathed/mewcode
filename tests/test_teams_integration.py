"""第 15 章：Agent Teams 系统集成测试（完整生命周期 / 续写 / Coordinator）。

验收场景（对应实战文档）：
  1. TeamCreate 创建团队 → Agent 工具带 team_name 派生队员 alice
     → 队员用共享任务板 + SendMessage 向 Lead 汇报 → 空闲通知回流
  2. Lead SendMessage 给已停止的队员 → 从磁盘 transcript 恢复续写
  3. 引擎 pre_turn_inject：队员消息在每轮 Agent Loop 开头注入
  4. TeamDelete：活跃时拒绝，全员空闲后清理（含 worktree）
  5. Coordinator Mode 双锁激活 + 工具集收窄
"""

from __future__ import annotations

import io
import json

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.engine.agent import Agent, AgentConfig
from mewcode.session import Session
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.teams import AgentNameRegistry, TeamManager, resolve_team_dir
from mewcode.tools import ToolRegistry

from conftest import ScriptedClient, text_turn, tools_turn

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


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    AgentNameRegistry.instance().reset()
    yield
    AgentNameRegistry.instance().reset()


# ---------------------------------------------------------------------------
# 验收场景 1：创建团队 → 派生队员 → 协作 → 空闲通知
# ---------------------------------------------------------------------------


async def test_team_lifecycle_end_to_end(tmp_path):
    """TeamCreate → Agent(team_name) spawn 队员 → 队员建任务并汇报 → 空闲。"""
    teammate_script = [
        tools_turn([("t1", "TaskCreate", {"title": "读取文档", "description": "读 README 并总结"})]),
        tools_turn([("t2", "SendMessage", {"to": "lead", "message": "文档已读取，共 5 个章节", "summary": "doc summary done"})]),
        text_turn("任务完成，已向 lead 汇报。"),
    ]
    app, _ = make_app(tmp_path, ScriptedClient(teammate_script))

    # 第一步：TeamCreate
    result = await app.team_create_tool.run({"team_name": "demo", "description": "验收团队"})
    assert result.success
    assert "Team 'demo' created" in result.output

    # 第二步：Agent 工具带 team_name 派生队员
    spawn_result = await app.agent_tool.run(
        {
            "prompt": "读取 README.md 并总结主要章节",
            "description": "文档总结",
            "subagent_type": "general-purpose",
            "name": "alice",
            "team_name": "demo",
        }
    )
    assert spawn_result.success, spawn_result.output
    assert "Teammate 'alice' spawned" in spawn_result.output

    team = app.team_manager.get_team("demo")
    member = team.get_member("alice")
    assert member is not None
    assert member.is_active is True

    # 第三步：等队员跑完（in-process asyncio task）
    handle = app.team_manager.get_inprocess_handle(member.agent_id)
    assert handle is not None
    await asyncio_wait_for_handle(handle)

    # 队员的系统提示包含团队附录；请求里能看到它的任务指令
    system_prompt = app.client.requests[0][0]["content"]
    assert "running as an agent in a team" in system_prompt

    # 队员用共享任务板建了任务
    store = app.team_manager.get_task_store("demo")
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "读取文档"

    # Lead 邮箱：队员消息 + 空闲通知（推模型）
    messages = app.team_manager.get_mailbox("demo").consume("lead")
    contents = [m.content for m in messages]
    assert any("文档已读取" in c for c in contents)
    assert any("idle" in c for c in contents)
    # 空闲通知排在队员消息之后（时间戳序）
    assert messages[-1].summary == "alice idle"

    # 团队配置：成员标记为空闲
    team = app.team_manager.get_team("demo")
    assert team.get_member("alice").is_active is False

    # 对话记录已持久化到磁盘
    transcript_path = resolve_team_dir("demo") / "transcripts" / f"{member.agent_id}.json"
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    roles = [m["role"] for m in data["messages"]]
    assert "assistant" in roles
    # 引擎修复回归：任务文本不再重复写入两次
    user_contents = [m["content"] for m in data["messages"] if m["role"] == "user" and m["content"]]
    assert user_contents.count("读取 README.md 并总结主要章节") == 1


async def asyncio_wait_for_handle(handle, timeout: float = 5.0) -> None:
    import asyncio

    await asyncio.wait_for(handle.task, timeout=timeout)


# ---------------------------------------------------------------------------
# 验收场景 2：SendMessage 续写已停止的队员
# ---------------------------------------------------------------------------


async def test_send_message_resumes_stopped_teammate(tmp_path):
    """队员空闲后，Lead SendMessage → 从磁盘 transcript 恢复继续工作。"""
    teammate_script = [
        text_turn("第一轮工作完成。"),
        text_turn("续写：补充测试也完成了。"),
    ]
    app, _ = make_app(tmp_path, ScriptedClient(teammate_script))
    await app.team_create_tool.run({"team_name": "demo"})
    await app.agent_tool.run(
        {
            "prompt": "第一轮任务",
            "description": "第一轮",
            "subagent_type": "general-purpose",
            "name": "alice",
            "team_name": "demo",
        }
    )
    member = app.team_manager.get_team("demo").get_member("alice")
    handle = app.team_manager.get_inprocess_handle(member.agent_id)
    await asyncio_wait_for_handle(handle)
    first_transcript = resolve_team_dir("demo") / "transcripts" / f"{member.agent_id}.json"
    first_count = len(json.loads(first_transcript.read_text(encoding="utf-8"))["messages"])

    # Lead 发新指令：系统检测到 alice 已停止 → 自动恢复
    result = await app.team_send_tool.run(
        {"to": "alice", "message": "还需要补充集成测试", "summary": "follow up task"}
    )
    assert result.success
    assert "已从磁盘 transcript 恢复" in result.output

    handle = app.team_manager.get_inprocess_handle(member.agent_id)
    await asyncio_wait_for_handle(handle)

    # 续写后的 transcript 更长（历史 + 新指令 + 新回复）
    second_count = len(json.loads(first_transcript.read_text(encoding="utf-8"))["messages"])
    assert second_count > first_count
    # 队员再次空闲，Lead 收到第二条空闲通知（第一轮的未消费，累计 >= 2）
    messages = app.team_manager.get_mailbox("demo").consume("lead")
    idle_notices = [m for m in messages if m.summary == "alice idle"]
    assert len(idle_notices) >= 2


# ---------------------------------------------------------------------------
# 验收场景 3：Lead 邮箱在 Agent Loop 每轮开头注入
# ---------------------------------------------------------------------------


async def test_lead_mailbox_injected_into_loop(tmp_path):
    """pre_turn_inject：消息以 system reminder 出现在请求里（不污染历史）。"""
    client = ScriptedClient([text_turn("收到，我来处理团队消息。")])
    app, _ = make_app(tmp_path, client)
    agent = Agent(
        PROVIDER,
        session=Session(model="demo"),
        registry=app.registry,
        workdir=str(tmp_path),
        client=client,
        agent_config=AgentConfig(max_iterations=5),
    )
    agent.pre_turn_inject = lambda turn: ["[团队 demo]\n收到 1 条团队消息：alice idle"]
    await agent.run_to_completion("检查团队状态")
    request = client.requests[0]
    reminder_texts = [
        m["content"] for m in request if "teammate" in m.get("content", "") or "alice idle" in m.get("content", "")
    ]
    assert any("<system-reminder>" in t and "alice idle" in t for t in reminder_texts)

    # 回调抛异常不阻断 Agent Loop
    def _boom(turn):
        raise RuntimeError("drain failed")

    agent2 = Agent(
        PROVIDER,
        session=Session(model="demo"),
        registry=app.registry,
        workdir=str(tmp_path),
        client=ScriptedClient([text_turn("ok")]),
        agent_config=AgentConfig(max_iterations=5),
    )
    agent2.pre_turn_inject = _boom
    result = await agent2.run_to_completion("继续")
    assert result == "ok"


async def test_app_drains_lead_mailbox_across_teams(tmp_path):
    """App._drain_lead_mailbox：跨团队收集 Lead 的未读消息。"""
    app, _ = make_app(tmp_path, ScriptedClient([]))
    manager: TeamManager = app.team_manager
    manager.create_team("alpha")
    manager.create_team("beta")
    from mewcode.teams import create_message

    manager.get_mailbox("alpha").write("lead", create_message("alice", "lead", "alpha 汇报", summary="a"))
    manager.get_mailbox("beta").write("lead", create_message("bob", "lead", "beta 汇报", summary="b"))
    drained = app._drain_lead_mailbox(1)
    assert len(drained) == 1
    assert "alpha 汇报" in drained[0]
    assert "beta 汇报" in drained[0]
    # 消费语义：第二次为空
    assert app._drain_lead_mailbox(2) == []


# ---------------------------------------------------------------------------
# 验收场景 4：TeamDelete 清理（含 in-process 运行保护与 worktree）
# ---------------------------------------------------------------------------


def init_git_repo(path):
    from mewcode.worktree.changes import run_git

    path.mkdir(parents=True, exist_ok=True)
    assert run_git(["init"], cwd=str(path)).returncode == 0
    run_git(["config", "user.email", "t@t"], cwd=str(path))
    run_git(["config", "user.name", "t"], cwd=str(path))
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    run_git(["add", "."], cwd=str(path))
    assert run_git(["commit", "-m", "init"], cwd=str(path)).returncode == 0


async def test_team_delete_with_worktree_cleanup(tmp_path):
    """git 仓库里队员获得 worktree；TeamDelete 全员空闲后强制清理。"""
    init_git_repo(tmp_path)
    app, _ = make_app(tmp_path, ScriptedClient([text_turn("完成。")]))
    await app.team_create_tool.run({"team_name": "demo"})
    spawn = await app.agent_tool.run(
        {
            "prompt": "修改文件",
            "description": "并行修改",
            "subagent_type": "general-purpose",
            "name": "alice",
            "team_name": "demo",
        }
    )
    assert spawn.success
    assert "worktree" in spawn.output
    member = app.team_manager.get_team("demo").get_member("alice")
    # 队员有独立的 worktree 目录（team-demo/alice 嵌套 slug）
    assert "team-demo" in member.worktree_path
    handle = app.team_manager.get_inprocess_handle(member.agent_id)
    await asyncio_wait_for_handle(handle)

    # 运行中删除被拒绝 → 空闲后删除成功并清理 worktree
    result = await app.team_delete_tool.run({"team_name": "demo"})
    assert "deleted" in result.output
    # worktree 清理是后台任务，稍等让事件循环跑完
    import asyncio as _asyncio
    from pathlib import Path

    await _asyncio.sleep(0.3)
    assert not Path(member.worktree_path).exists()
    assert app.team_manager.get_team("demo") is None


# ---------------------------------------------------------------------------
# 验收场景 5：Coordinator Mode
# ---------------------------------------------------------------------------


async def test_coordinator_mode_double_lock_and_registry(tmp_path, monkeypatch):
    """feature flag + 环境变量双锁；激活后工具集收窄。"""
    # 两把锁都没开 → 不激活
    app, _ = make_app(tmp_path, ScriptedClient([]))
    assert not app._coordinator_mode_active()

    # 只开 feature flag → 不激活（缺环境变量）
    config_dir = tmp_path / ".mewcode"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.yaml").write_text("features:\n  coordinatorMode: true\n", encoding="utf-8")
    assert not app._coordinator_mode_active()

    # 双锁齐开 → 激活
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1")
    assert app._coordinator_mode_active()

    coordinator = app._build_coordinator_registry()
    names = set(coordinator.names())
    # Lead 保留读类与调度工具，被剥夺写类
    assert "ReadFile" in names and "Bash" in names
    assert "TeamCreate" in names and "SendMessage" in names
    assert "WriteFile" not in names and "EditFile" not in names
    # 共享任务工具（TaskCreate 等）注入
    assert {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"} <= names
    # TaskList 是共享任务板版本（描述可区分）
    assert "共享任务板" in coordinator.get("TaskList").description

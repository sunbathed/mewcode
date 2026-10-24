"""第 15 章：Agent Teams 系统单元测试。

覆盖：核心数据结构与序列化、名称注册表、文件邮箱、共享任务板、
对话记录持久化、后端检测与 CLI 命令构建、in-process 句柄、
协调者模式双锁与工具收窄、队员工具注入、团队工具与消息路由。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mewcode.session import Session, ToolUseBlock
from mewcode.teams import (
    AgentNameRegistry,
    AgentTeam,
    BackendType,
    Mailbox,
    SharedTask,
    SharedTaskStore,
    TeamError,
    TeammateInfo,
    build_coordinator_registry,
    build_team_task_tools,
    build_teammate_tools,
    create_message,
    format_mailbox_digest,
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    is_coordinator_mode,
    load_transcript,
    resolve_team_dir,
    save_transcript,
    unique_team_name,
)
from mewcode.teams.backend_detect import BackendDetectionError, detect_backend, detect_backend_or_fallback
from mewcode.teams.coordinator import COORDINATOR_MODE_ALLOWED_TOOLS
from mewcode.teams.spawn_inprocess import InProcessTeammateHandle, spawn_inprocess_teammate
from mewcode.teams.spawn_tmux import build_cli_command, shell_quote, spawn_tmux_teammate
from mewcode.teams.tools import SendMessageTool, TeamCreateTool, TeamDeleteTool
from mewcode.teams.manager import TeamManager
from mewcode.tools import ToolRegistry
from mewcode.tools.base import BaseTool


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每个测试独立的团队目录与注册表（单例隔离）。"""
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    AgentNameRegistry.instance().reset()
    yield
    AgentNameRegistry.instance().reset()


# ---------------------------------------------------------------------------
# 核心类型与序列化
# ---------------------------------------------------------------------------


def test_backend_type_str_values():
    """BackendType 继承 str：序列化直接输出字符串值。"""
    assert BackendType.TMUX == "tmux"
    assert BackendType.ITERM2 == "iterm2"
    assert BackendType.IN_PROCESS == "in-process"


def test_teammate_info_roundtrip():
    member = TeammateInfo(
        name="alice",
        agent_id="a1b2c3",
        agent_type="general-purpose",
        model="inherit",
        worktree_path="/tmp/wt",
        backend_type="in-process",
        is_active=None,
    )
    data = member.to_dict()
    assert data["is_active"] is None  # 三态保留
    restored = TeammateInfo.from_dict(data)
    assert restored == member


def test_agent_team_save_load_roundtrip(tmp_path):
    team = AgentTeam(name="demo", lead_agent_id="lead", description="测试团队")
    team.add_member(
        TeammateInfo("alice", "id-a", "general-purpose", "", "/wt/a", "in-process", True)
    )
    team.config_path = str(tmp_path / "config.json")
    team.save()
    loaded = AgentTeam.load(team.config_path)
    assert loaded.name == "demo"
    assert loaded.lead_agent_id == "lead"
    assert loaded.members[0].name == "alice"
    assert loaded.members[0].is_active is True


def test_agent_team_member_semantics():
    team = AgentTeam(name="demo", lead_agent_id="lead")
    team.add_member(TeammateInfo("alice", "a", "", "", "", "in-process", True))
    team.add_member(TeammateInfo("bob", "b", "", "", "", "in-process", False))
    team.add_member(TeammateInfo("carol", "c", "", "", "", "in-process", None))
    # all_idle：全员明确空闲才成立
    assert not team.all_idle()
    assert team.set_member_active("alice", False)
    assert team.set_member_active("carol", False)
    assert team.all_idle()
    # active_members：未明确标记 False 的都算活跃（含 None）
    team.members[2].is_active = None
    assert [m.name for m in team.active_members()] == ["carol"]
    # 同名队员覆盖而非累积
    team.add_member(TeammateInfo("alice", "a2", "", "", "", "in-process", True))
    assert len(team.members) == 3
    assert team.get_member("alice").agent_id == "a2"


def test_unique_team_name_suffix(tmp_path):
    base = Path(tmp_path) / "teams"
    (base / "demo").mkdir(parents=True)
    assert unique_team_name("demo") == "demo-2"
    (base / "demo-2").mkdir()
    assert unique_team_name("demo") == "demo-3"
    assert unique_team_name("fresh") == "fresh"


def test_resolve_team_dir_sanitize(monkeypatch, tmp_path):
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    d = resolve_team_dir("My Team!")
    assert d == tmp_path / "teams" / "my-team"


# ---------------------------------------------------------------------------
# AgentNameRegistry
# ---------------------------------------------------------------------------


def test_name_registry_singleton_and_resolve():
    registry = AgentNameRegistry.instance()
    assert AgentNameRegistry.instance() is registry
    registry.register("alice", "agent-a1")
    registry.register("bob", "agent-b2")
    # 名字 → ID
    assert registry.resolve("alice") == "agent-a1"
    # ID 直通（task-notification 返回的就是 ID）
    assert registry.resolve("agent-b2") == "agent-b2"
    assert registry.resolve("unknown") is None
    registry.unregister("alice")
    assert registry.resolve("alice") is None
    assert registry.resolve("agent-b2") == "agent-b2"


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------


def test_mailbox_write_consume_order_and_delete(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox")
    m1 = create_message("alice", "lead", "第一条", summary="first")
    m2 = create_message("alice", "lead", "第二条", summary="second")
    mailbox.write("lead", m1)
    mailbox.write("lead", m2)
    # 文件名以时间戳开头：sorted 即时间序
    consumed = mailbox.consume("lead")
    assert [m.content for m in consumed] == ["第一条", "第二条"]
    # consume 是消费语义：读完即删
    assert mailbox.consume("lead") == []


def test_mailbox_skips_corrupted_files(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox")
    mailbox.write("lead", create_message("alice", "lead", "正常消息", summary="ok"))
    agent_dir = tmp_path / "mailbox" / "lead"
    (agent_dir / "0.000000_broken.json").write_text("{not json", encoding="utf-8")
    consumed = mailbox.consume("lead")
    assert len(consumed) == 1
    assert consumed[0].content == "正常消息"
    # 坏文件保留（跳过不删），好文件已删
    assert (agent_dir / "0.000000_broken.json").exists()


def test_mailbox_broadcast_excludes_sender(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox")
    count = mailbox.broadcast(
        ["agent-a", "agent-b", "agent-c"],
        create_message("agent-b", "*", "大家好"),
        exclude="agent-b",
    )
    assert count == 2
    assert len(mailbox.consume("agent-a")) == 1
    assert mailbox.consume("agent-b") == []


def test_mailbox_message_type_passthrough():
    msg = create_message("a", "b", "content", message_type="shutdown_request")
    assert msg.message_type == "shutdown_request"
    # 未知类型退化为 text
    msg2 = create_message("a", "b", "content", message_type="bogus")
    assert msg2.message_type == "text"


# ---------------------------------------------------------------------------
# SharedTaskStore
# ---------------------------------------------------------------------------


def test_shared_task_store_crud_and_deps(tmp_path):
    store = SharedTaskStore(tmp_path / "tasks.json")
    store.init_empty()
    t1 = store.create("重构数据层", description="...", created_by="lead")
    t2 = store.create("更新测试")
    # 依赖：T2 被 T1 阻塞（增量追加、去重）
    store.update(t2.id, add_blocked_by=[t1.id])
    store.update(t2.id, add_blocked_by=[t1.id, t1.id])
    got = store.get(t2.id)
    assert got.blocked_by == [t1.id]
    # 状态机
    store.update(t1.id, status="in_progress", assignee="alice")
    assert store.get(t1.id).status == "in_progress"
    assert store.get(t1.id).assignee == "alice"
    with pytest.raises(ValueError):
        store.update(t1.id, status="bogus")
    # 列表按数字 ID 排序
    t3 = store.create("第三个")
    assert [t.id for t in store.list_tasks()] == [t1.id, t2.id, t3.id]
    assert store.delete("99") is False


def test_shared_task_store_persistence(tmp_path):
    path = tmp_path / "tasks.json"
    store = SharedTaskStore(path)
    store.create("任务一")
    # 重新实例化（等价于队员进程重新读盘）
    store2 = SharedTaskStore(path)
    assert len(store2.list_tasks()) == 1
    # 新建任务的 ID 接着编号，不会重复
    t2 = store2.create("任务二")
    assert t2.id == "2"


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def test_transcript_roundtrip_with_tool_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    session = Session(model="test-model")
    session.add_message("user", "读取配置文件")
    session.add_message("assistant", "", blocks=[ToolUseBlock(id="call1", name="ReadFile", input={"path": "a.md"})])
    session.add_message("user", "", blocks=[])  # tool result 由 add_tool_results 生成，这里简化
    path = save_transcript("demo", "agent-xyz", session)
    assert path.exists()
    assert "transcripts" in str(path)
    restored = load_transcript("demo", "agent-xyz")
    assert restored is not None
    assert restored.model == "test-model"
    assert len(restored.messages) == 3
    assert restored.messages[1].blocks[0].name == "ReadFile"
    assert load_transcript("demo", "missing-agent") is None


def test_load_transcript_corrupted_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    path = resolve_team_dir("demo") / "transcripts" / "bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert load_transcript("demo", "bad") is None


# ---------------------------------------------------------------------------
# 后端检测
# ---------------------------------------------------------------------------


def test_detect_backend_explicit_and_non_interactive(monkeypatch):
    monkeypatch.setattr("mewcode.teams.backend_detect._tmux_installed", lambda: False)
    assert detect_backend("in-process") is BackendType.IN_PROCESS
    assert detect_backend("", is_interactive=False) is BackendType.IN_PROCESS


def test_detect_backend_tmux_and_iterm2(monkeypatch):
    monkeypatch.setattr("mewcode.teams.backend_detect._tmux_installed", lambda: False)
    monkeypatch.setattr("mewcode.teams.backend_detect._in_tmux_session", lambda: True)
    assert detect_backend() is BackendType.TMUX
    monkeypatch.setattr("mewcode.teams.backend_detect._in_tmux_session", lambda: False)
    monkeypatch.setattr("mewcode.teams.backend_detect._in_iterm2", lambda: True)
    monkeypatch.setattr("mewcode.teams.backend_detect._it2_available", lambda: True)
    assert detect_backend() is BackendType.ITERM2
    # it2 不可用 → 落到 tmux 二进制
    monkeypatch.setattr("mewcode.teams.backend_detect._it2_available", lambda: False)
    monkeypatch.setattr("mewcode.teams.backend_detect._tmux_installed", lambda: True)
    assert detect_backend() is BackendType.TMUX


def test_detect_backend_raises_when_nothing_available(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr("mewcode.teams.backend_detect._tmux_installed", lambda: False)
    with pytest.raises(BackendDetectionError):
        detect_backend()


def test_detect_backend_or_fallback(monkeypatch):
    monkeypatch.setattr("mewcode.teams.backend_detect._tmux_installed", lambda: False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    backend, notice = detect_backend_or_fallback()
    assert backend is BackendType.IN_PROCESS
    assert "回退" in notice  # 显式兜底，不是静默退化


# ---------------------------------------------------------------------------
# Tmux CLI 命令构建与三级降级
# ---------------------------------------------------------------------------


def test_build_cli_command_and_quote_escaping():
    cmd = build_cli_command(
        team_name="demo",
        teammate_name="alice",
        worktree_path="/wt/a",
        prompt="do it 'now'",
        agent_type="explore",
        model="haiku",
    )
    assert cmd.startswith("MEWCODE_TEAM_NAME=demo MEWCODE_TEAMMATE_NAME=alice mewcode --teammate --work-dir /wt/a")
    assert "--agent-type explore" in cmd
    assert "--model haiku" in cmd
    # 单引号转义：prompt 里的单引号不会破坏 shell 解析
    assert "'do it '\\''now'\\'''" in cmd
    assert shell_quote("it's") == "'it'\\''s'"


def test_spawn_tmux_three_level_fallback(monkeypatch):
    from mewcode.teams.spawn_tmux import TmuxSpawnError

    calls: list[list[str]] = []

    def fake_run_tmux(args, timeout=10):
        calls.append(args)
        if args[0] == "split-window" and len(calls) == 1:
            raise TmuxSpawnError("first split fails")
        if args[0] == "list-panes":
            return "pane-77\n"
        return "pane-1"

    monkeypatch.setattr("mewcode.teams.spawn_tmux._run_tmux", fake_run_tmux)
    info = spawn_tmux_teammate("demo", "alice", "/wt", "开工", agent_type="", model="")
    # 一级 split 失败 → 二级 new-window + split → send-keys 启动命令
    assert calls[0][0] == "split-window"
    assert calls[1][0] == "new-window"
    assert calls[2][0] == "split-window"
    assert calls[-1][0] == "send-keys"
    assert info.pane_id == "pane-1"
    assert info.window_name == "demo-alice"


def test_spawn_tmux_new_session_level(monkeypatch):
    from mewcode.teams.spawn_tmux import TmuxSpawnError

    calls: list[list[str]] = []

    def fake_run_tmux(args, timeout=10):
        calls.append(args)
        if args[0] == "split-window":
            raise TmuxSpawnError("no split")
        if args[0] == "list-panes":
            return "pane-99\n"
        return ""

    monkeypatch.setattr("mewcode.teams.spawn_tmux._run_tmux", fake_run_tmux)
    info = spawn_tmux_teammate("demo", "bob", "/wt", "开工")
    # 一级 split 失败 → 二级 new-window + split（仍失败）→ 三级 new-session
    assert calls[1][0] == "new-window"
    assert calls[3][0] == "new-session"
    assert info.pane_id == "pane-99"


# ---------------------------------------------------------------------------
# In-process 句柄
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail

    async def run_to_completion(self, task, *, fork_conversation=None):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return f"done: {task}"


def test_inprocess_handle_completes():
    async def scenario():
        agent = _FakeAgent()
        handle = spawn_inprocess_teammate(agent, "任务A", "alice")
        assert isinstance(handle, InProcessTeammateHandle)
        assert not handle.done
        result = await asyncio.wait_for(handle.task, timeout=2)
        return handle, result

    handle, result = asyncio.run(scenario())
    assert result == "done: 任务A"
    assert handle.done
    assert handle.result() == "done: 任务A"


def test_inprocess_handle_cancel():
    async def scenario():
        agent = _FakeAgent(delay=5)
        handle = spawn_inprocess_teammate(agent, "长任务", "bob")
        handle.cancel()
        try:
            await asyncio.wait_for(handle.task, timeout=2)
            return "completed"
        except asyncio.CancelledError:
            return "cancelled"

    assert asyncio.run(scenario()) == "cancelled"


# ---------------------------------------------------------------------------
# Coordinator Mode
# ---------------------------------------------------------------------------


def test_coordinator_double_lock(monkeypatch, tmp_path):
    # 第一把锁：feature flag
    (tmp_path / ".mewcode").mkdir(parents=True)
    (tmp_path / ".mewcode" / "config.yaml").write_text(
        "features:\n  coordinatorMode: true\n", encoding="utf-8"
    )
    from mewcode.teams import coordinator_feature_enabled

    assert coordinator_feature_enabled(str(tmp_path))
    # flag 未配置 → 不开启
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not coordinator_feature_enabled(str(empty))
    # 双锁：flag 开 + 环境变量开 → is_coordinator_mode True
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1")
    assert is_coordinator_mode(enable_flag=True)
    # 只开环境变量、flag 关 → 不进入
    assert not is_coordinator_mode(enable_flag=False)
    monkeypatch.delenv("MEWCODE_COORDINATOR_MODE")
    assert not is_coordinator_mode(enable_flag=True)


def test_coordinator_registry_narrowing():
    registry = ToolRegistry(workdir=".")
    registry.register(_DummyTool("Agent"))
    registry.register(_DummyTool("TeamCreate"))
    registry.register(_DummyTool("SendMessage"))
    registry.register(_DummyTool("TaskList"))  # 后台任务版本
    coordinator = build_coordinator_registry(registry, {"TaskList": _DummyTool("TaskList", "shared")})
    names = set(coordinator.names())
    # 读类 + 调度保留
    assert {"ReadFile", "Bash", "Glob", "Grep", "Agent", "TeamCreate", "SendMessage"} <= names
    # 写类被剥夺
    assert "WriteFile" not in names
    assert "EditFile" not in names
    assert "ToolSearch" not in names
    # 共享任务板版本覆盖后台任务版本（同名后注册者胜）
    assert coordinator.get("TaskList").description == "shared"


class _DummyTool(BaseTool):
    def __init__(self, name: str, description: str = "dummy") -> None:
        self.name = name
        self.description = description

    async def _run(self, args):
        return "ok"


def test_coordinator_prompt_and_user_context():
    prompt = get_coordinator_system_prompt()
    for phase in ("Research", "Synthesis", "Implementation", "Verification"):
        assert phase in prompt
    assert "lazy delegation" in prompt.lower() or "反面模式" in prompt
    context = get_coordinator_user_context(["ReadFile", "Bash"])
    assert "ReadFile" in context["workerToolsContext"]
    default_context = get_coordinator_user_context()
    assert "SendMessage" in default_context["workerToolsContext"]
    assert COORDINATOR_MODE_ALLOWED_TOOLS


# ---------------------------------------------------------------------------
# 队员工具注入
# ---------------------------------------------------------------------------


def test_build_teammate_tools_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_TEAMS_DIR", str(tmp_path / "teams"))
    from mewcode.teams.manager import TeamManager

    manager = TeamManager()
    team = manager.create_team("demo")
    base = ToolRegistry(workdir=str(tmp_path))
    base.register(_DummyTool("Agent", "lead-only"))
    registry = build_teammate_tools(
        base,
        worktree_path=str(tmp_path),
        task_store=manager.get_task_store(team.name),
        mailbox=manager.get_mailbox(team.name),
        manager=manager,
        team_name=team.name,
        teammate_name="alice",
        self_agent_id="agent-1",
    )
    names = set(registry.names())
    # 工作工具 + 协调工具
    assert {"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"} <= names
    assert {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"} <= names
    # 队员不能 spawn 队员：Agent 工具不在池中
    assert "Agent" not in names
    # 队员的 SendMessage 绑定自己的身份
    send = registry.get("SendMessage")
    assert send._self_name == "alice"
    assert send._allow_resume is False
    # 共享任务工具写的是团队任务板
    asyncio.run(
        _create_task_via_tool(registry.get("TaskCreate"))
    )
    store = manager.get_task_store(team.name)
    assert len(store.list_tasks()) == 1


async def _create_task_via_tool(tool) -> None:
    await tool.run({"title": "协作任务"})


# ---------------------------------------------------------------------------
# 团队工具：TeamCreate / TeamDelete / SendMessage
# ---------------------------------------------------------------------------


def test_team_create_and_delete_tools(tmp_path):
    manager = TeamManager()
    create = TeamCreateTool(manager)
    delete = TeamDeleteTool(manager)

    result = asyncio.run(
        create.run({"team_name": "demo team", "description": "验收"})
    )
    assert result.success
    assert "Team 'demo-team' created" in result.output
    assert (resolve_team_dir("demo-team") / "config.json").exists()
    assert (resolve_team_dir("demo-team") / "tasks.json").exists()
    assert manager.active_team_name == "demo-team"

    # 有活跃队员时拒绝删除
    team = manager.get_team("demo-team")
    team.add_member(TeammateInfo("alice", "a", "", "", "", "in-process", True))
    team.save()
    refused = asyncio.run(delete.run({"team_name": "demo-team"}))
    assert "Cannot delete team" in refused.output
    assert (resolve_team_dir("demo-team") / "config.json").exists()

    # 全员空闲 → 删除成功，目录清理
    team.set_member_active("alice", False)
    team.save()
    done = asyncio.run(delete.run({"team_name": "demo-team"}))
    assert "deleted" in done.output
    assert not resolve_team_dir("demo-team").exists()


def test_send_message_routing(tmp_path):
    manager = TeamManager()
    manager.create_team("demo")
    manager.register_member("demo", TeammateInfo("alice", "agent-a", "", "", "", "in-process", True))
    manager.register_member("demo", TeammateInfo("bob", "agent-b", "", "", "", "in-process", False))

    send = SendMessageTool(manager, allow_resume=True)

    # 按名称路由
    result = asyncio.run(
        send.run({"to": "alice", "message": "开始干活", "summary": "kick off work"})
    )
    assert result.success
    assert len(manager.get_mailbox("demo").consume("agent-a")) == 1

    # 按 agent_id 路由（task-notification 里拿到的就是 ID）
    result = asyncio.run(
        send.run({"to": "agent-b", "message": "hi", "summary": "greeting"})
    )
    assert result.success
    assert len(manager.get_mailbox("demo").consume("agent-b")) == 1

    # 广播
    result = asyncio.run(
        send.run({"to": "*", "message": "全体注意", "summary": "broadcast note"})
    )
    assert result.success
    assert "broadcast to 2" in result.output

    # 未知名报错
    result = asyncio.run(
        send.run({"to": "nobody", "message": "?", "summary": "unknown target"})
    )
    assert "未找到收件人" in result.output

    # 纯文本消息缺 summary 报错
    result = asyncio.run(
        send.run({"to": "alice", "message": "no summary"})
    )
    assert "summary" in result.output


def test_send_message_teammate_side_writes_lead_mailbox(tmp_path):
    manager = TeamManager()
    manager.create_team("demo")
    team = manager.get_team("demo")
    member = TeammateInfo("alice", "agent-a", "", "", "", "in-process", True)
    manager.register_member("demo", member)
    # 队员侧实例：绑定固定团队上下文
    send = SendMessageTool(
        manager,
        mailbox=manager.get_mailbox("demo"),
        team_name="demo",
        self_name="alice",
        self_agent_id="agent-a",
        allow_resume=False,
    )
    result = asyncio.run(
        send.run({"to": "lead", "message": "我做完了", "summary": "work done notice"})
    )
    assert result.success
    lead_messages = manager.get_mailbox("demo").consume("lead")
    assert len(lead_messages) == 1
    assert lead_messages[0].from_agent == "alice"


def test_manager_set_member_idle_notifies_lead(tmp_path):
    manager = TeamManager()
    manager.create_team("demo")
    member = TeammateInfo("alice", "agent-a", "", "", "", "in-process", True)
    manager.register_member("demo", member)
    manager.set_member_idle("demo", "alice", last_output="结果摘要")
    team = manager.get_team("demo")
    assert team.get_member("alice").is_active is False
    messages = manager.get_mailbox("demo").consume("lead")
    assert len(messages) == 1
    assert "idle" in messages[0].content
    assert "结果摘要" in messages[0].content
    assert messages[0].summary == "alice idle"


def test_format_mailbox_digest():
    messages = [
        create_message("alice", "lead", "内容一", summary="first update"),
        create_message("bob", "lead", "内容二", summary="second update", message_type="shutdown_request"),
    ]
    digest = format_mailbox_digest(messages)
    assert "收到 2 条团队消息" in digest
    assert 'from="alice"' in digest
    assert 'type="shutdown_request"' in digest
    assert format_mailbox_digest([]) == ""


# ---------------------------------------------------------------------------
# SharedTask 数据结构
# ---------------------------------------------------------------------------


def test_shared_task_roundtrip():
    task = SharedTask(id="1", title="T", blocks=["2"], blocked_by=["3"], status="in_progress")
    restored = SharedTask.from_dict(task.to_dict())
    assert restored == task

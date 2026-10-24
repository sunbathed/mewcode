"""第 14 章：Worktree 系统集成测试（App / Agent 工具 / 斜杠命令 / 验收场景）。

验收场景（对应实战文档）：
  主 Agent 创建 witness.txt（original content from main agent）→
  Agent 工具派子 Agent（isolation=worktree）修改 witness.txt 并 git 提交 →
  worktree 里的 witness.txt 是「modified by isolated worker」，主目录那份
  保持原样，文件冲突被隔离。
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from mewcode.app import App
from mewcode.commands import ParsedCommand
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools import ToolRegistry
from mewcode.worktree import count_worktree_changes
from mewcode.worktree.changes import run_git

from conftest import ScriptedClient, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    result = run_git(list(args), cwd=str(repo))
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def init_git_repo(path: Path) -> None:
    """初始化一个可用的 git 仓库（含身份配置与首个提交）。"""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "init")


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


def make_isolated_worker(tmp_path, isolation="worktree", permission="bypassPermissions"):
    """在项目 .mewcode/agents/ 下创建 isolated-worker 定义（可控制 isolation 字段）。"""
    config_dir = tmp_path / ".mewcode" / "agents"
    config_dir.mkdir(parents=True, exist_ok=True)
    isolation_line = f"isolation: {isolation}\n" if isolation else ""
    (config_dir / "isolated-worker.md").write_text(
        "---\n"
        "name: isolated-worker\n"
        "description: 在独立工作树中执行文件修改\n"
        "maxTurns: 20\n"
        f"permissionMode: {permission}\n"
        + isolation_line
        + "---\n\n"
        "你在独立的工作树中执行任务，完成后提交更改。\n",
        encoding="utf-8",
    )


def tool_round(tool_id: str, name: str, args: dict) -> list:
    return tools_turn([(tool_id, name, args)])


class WorktreeWorkerClient:
    """子 Agent 脚本：改写 witness.txt → git 提交 → 文本收尾。"""

    def __init__(self, commit=True):
        self.requests = []
        self.calls = 0
        self.commit = commit

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            for event in tool_round(
                "c1", "WriteFile", {"path": "witness.txt", "content": "modified by isolated worker"}
            ):
                yield event
            return
        if self.calls == 2 and self.commit:
            for event in tool_round(
                "c2", "Bash", {"command": 'git add -A && git commit -m "modify witness"'}
            ):
                yield event
            return
        yield TextDelta("任务完成。")
        yield StreamEnd("stop")


class ReadOnlyWorkerClient:
    """子 Agent 脚本：只读文件，不做任何修改（worktree 应被自动清理）。"""

    def __init__(self):
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            for event in tool_round("r1", "ReadFile", {"path": "README.md"}):
                yield event
            return
        yield TextDelta("只做了阅读分析，没有修改任何文件。")
        yield StreamEnd("stop")


# ---------------------------------------------------------------------------
# 验收场景：isolation=worktree 的子 Agent 在隔离目录中修改文件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_worktree_isolation(tmp_path):
    """主目录 witness.txt 不受影响，worktree 里是修改后的版本。"""
    init_git_repo(tmp_path)
    # 主 Agent 先创建 witness.txt 并提交（worktree 里才有这个文件可改）
    (tmp_path / "witness.txt").write_text("original content from main agent", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "witness")

    client = WorktreeWorkerClient()
    app, _ = make_app(tmp_path, client)
    make_isolated_worker(tmp_path)

    result = await app.agent_tool.run(
        {
            "prompt": "把 witness.txt 的内容改成 modified by isolated worker，然后用 git 提交",
            "description": "隔离修改 witness 文件",
            "subagent_type": "isolated-worker",
        }
    )
    assert not result.is_error, result.output

    # 主目录的 witness.txt 保持原样（子 Agent 的写入被隔离在 worktree）
    assert (
        (tmp_path / "witness.txt").read_text(encoding="utf-8") == "original content from main agent"
    )
    # worktree 里的 witness.txt 是子 Agent 修改后的版本
    worktrees = list(app.worktree_manager.active.values())
    assert len(worktrees) == 1
    wt = worktrees[0]
    assert wt.name.startswith("agent-")  # 临时命名模式
    assert (
        (Path(wt.path) / "witness.txt").read_text(encoding="utf-8") == "modified by isolated worker"
    )
    # 子 Agent 提交了 commit → worktree 保留供 review
    changes = count_worktree_changes(wt.path, wt.head_commit)
    assert changes.new_commits == 1
    assert "[Worktree 保留在" in result.output


@pytest.mark.asyncio
async def test_isolation_param_overrides_definition(tmp_path):
    """工具参数级 isolation=worktree 覆盖定义文件（定义里未声明 isolation）。"""
    init_git_repo(tmp_path)
    make_isolated_worker(tmp_path, isolation="")  # 定义级无隔离
    client = WorktreeWorkerClient()
    app, _ = make_app(tmp_path, client)

    result = await app.agent_tool.run(
        {
            "prompt": "把 witness.txt 的内容改成 modified by isolated worker，然后用 git 提交",
            "description": "隔离修改 witness 文件",
            "subagent_type": "isolated-worker",
            "isolation": "worktree",
        }
    )
    assert not result.is_error, result.output
    assert len(app.worktree_manager.active) == 1


@pytest.mark.asyncio
async def test_clean_worktree_auto_removed(tmp_path):
    """子 Agent 只读不改 → worktree 没有变更 → 自动清理。"""
    init_git_repo(tmp_path)
    client = ReadOnlyWorkerClient()
    app, _ = make_app(tmp_path, client)
    make_isolated_worker(tmp_path)

    result = await app.agent_tool.run(
        {
            "prompt": "阅读 README.md 并总结内容",
            "description": "只读分析任务",
            "subagent_type": "isolated-worker",
        }
    )
    assert not result.is_error, result.output
    # 干净的临时 worktree 已被自动删除
    assert len(app.worktree_manager.active) == 0
    assert "[Worktree 保留在" not in result.output
    # .mewcode/worktrees 下没有残留目录
    wt_dir = tmp_path / ".mewcode" / "worktrees"
    assert not wt_dir.exists() or not any(wt_dir.iterdir())


@pytest.mark.asyncio
async def test_background_worktree_task_completes_with_cleanup(tmp_path):
    """后台 + worktree 隔离：任务完成后保留 worktree（有提交），结果可查询。"""
    init_git_repo(tmp_path)
    make_isolated_worker(tmp_path)
    client = WorktreeWorkerClient()
    app, _ = make_app(tmp_path, client)

    result = await app.agent_tool.run(
        {
            "prompt": "把 witness.txt 的内容改成 modified by isolated worker，然后用 git 提交",
            "description": "后台隔离任务",
            "subagent_type": "isolated-worker",
            "run_in_background": True,
        }
    )
    assert "Task ID" in result.output

    completed = []
    for _ in range(200):  # 最多等 10 秒
        completed = app.task_manager.poll_completed()
        if completed:
            break
        await asyncio.sleep(0.05)
    assert completed, "后台任务未在超时内完成"
    task = completed[0]
    assert task.status == "completed", task.result
    # 有提交 → worktree 保留，内容是子 Agent 改后的
    worktrees = list(app.worktree_manager.active.values())
    assert len(worktrees) == 1
    assert (
        (Path(worktrees[0].path) / "witness.txt").read_text(encoding="utf-8")
        == "modified by isolated worker"
    )


# ---------------------------------------------------------------------------
# /worktree 斜杠命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_command_create_list_enter_exit(tmp_path):
    """create → list → enter（工具根目录切换）→ 写文件落在 worktree → exit 切回。"""
    init_git_repo(tmp_path)
    app, output = make_app(tmp_path)

    await app._handle_worktree_command(ParsedCommand("worktree", "create my-feature"))
    assert "my-feature" in app.worktree_manager.active
    assert "my-feature" in output.getvalue()

    await app._handle_worktree_command(ParsedCommand("worktree", "enter my-feature"))
    wt = app.worktree_manager.active["my-feature"]
    # 显式 cwd：注册中心根目录已切到 worktree
    assert app.registry.get("WriteFile").workdir == wt.path
    assert app.registry.get("Bash").workdir == wt.path

    # 进入后写文件落在 worktree 里，主目录不受影响
    write_result = await app.registry.execute("WriteFile", {"path": "inner.txt", "content": "x"})
    assert not write_result.is_error, write_result.error
    assert (Path(wt.path) / "inner.txt").is_file()
    assert not (tmp_path / "inner.txt").exists()

    await app._handle_worktree_command(ParsedCommand("worktree", "exit"))
    assert app.registry.get("WriteFile").workdir == str(tmp_path)
    assert app.worktree_manager.current_session is None
    assert Path(wt.path).is_dir()  # keep 模式：目录保留可复用


@pytest.mark.asyncio
async def test_worktree_exit_remove_blocked_by_changes(tmp_path):
    """退出即删除时有变更保护：默认拒绝，--discard 才放行。"""
    init_git_repo(tmp_path)
    app, output = make_app(tmp_path)

    await app._handle_worktree_command(ParsedCommand("worktree", "create blocked"))
    await app._handle_worktree_command(ParsedCommand("worktree", "enter blocked"))
    wt = app.worktree_manager.active["blocked"]
    (Path(wt.path) / "wip.txt").write_text("precious\n", encoding="utf-8")

    await app._handle_worktree_command(ParsedCommand("worktree", "exit remove"))
    assert "discard_changes" in output.getvalue()
    assert Path(wt.path).is_dir()  # 保护生效
    assert app.worktree_manager.current_session is not None  # 现场未动

    await app._handle_worktree_command(ParsedCommand("worktree", "exit remove --discard"))
    assert not Path(wt.path).exists()
    assert app.worktree_manager.current_session is None
    assert app.registry.get("WriteFile").workdir == str(tmp_path)  # 已切回主目录


@pytest.mark.asyncio
async def test_worktree_remove_with_changes_requires_discard_flag(tmp_path):
    init_git_repo(tmp_path)
    app, output = make_app(tmp_path)
    await app._handle_worktree_command(ParsedCommand("worktree", "create rm-test"))
    wt = app.worktree_manager.active["rm-test"]
    (Path(wt.path) / "keep-me.txt").write_text("data\n", encoding="utf-8")

    await app._handle_worktree_command(ParsedCommand("worktree", "remove rm-test"))
    assert "discard_changes" in output.getvalue()
    assert Path(wt.path).is_dir()

    await app._handle_worktree_command(ParsedCommand("worktree", "remove rm-test --discard"))
    assert not Path(wt.path).exists()


@pytest.mark.asyncio
async def test_worktree_invalid_name_reports_error(tmp_path):
    init_git_repo(tmp_path)
    app, output = make_app(tmp_path)
    await app._handle_worktree_command(ParsedCommand("worktree", "create ../../etc"))
    assert "Worktree 操作失败" in output.getvalue()
    assert len(app.worktree_manager.active) == 0


@pytest.mark.asyncio
async def test_app_restores_worktree_session_on_startup(tmp_path):
    """进程重启后从持久化 session 恢复 worktree 状态并切换工具根目录。"""
    init_git_repo(tmp_path)
    app, _ = make_app(tmp_path)
    await app._handle_worktree_command(ParsedCommand("worktree", "create restorable"))
    await app._handle_worktree_command(ParsedCommand("worktree", "enter restorable"))
    wt_path = app.worktree_manager.current_session.worktree_path

    # 重新构造 App（模拟进程重启）
    app2, output2 = make_app(tmp_path)
    assert app2.worktree_manager.current_session is not None
    assert app2.worktree_manager.current_session.worktree_path == wt_path
    # 工具根目录自动切回 worktree（继续之前的工作现场）
    assert app2.registry.get("WriteFile").workdir == wt_path
    assert "已恢复 Worktree 会话" in output2.getvalue()


# ---------------------------------------------------------------------------
# 完整对话链路（主 Agent 经 _ask 派发隔离子 Agent）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_conversation_acceptance(tmp_path):
    """文档验收的对话形式：主 Agent 写 witness → 派隔离子 Agent 改写并提交。"""
    init_git_repo(tmp_path)
    script = [
        # 主 Agent 第 1 轮：写 witness.txt
        tool_round("w1", "WriteFile", {"path": "witness.txt", "content": "original content from main agent"}),
        # 主 Agent 第 2 轮：派隔离子 Agent（isolation 参数）
        tool_round(
            "a1",
            "Agent",
            {
                "prompt": "把 witness.txt 的内容改成 modified by isolated worker，然后用 git 提交",
                "description": "隔离修改 witness",
                "subagent_type": "isolated-worker",
                "isolation": "worktree",
            },
        ),
        # 子 Agent 第 1 轮：改写 witness.txt
        tool_round("s1", "WriteFile", {"path": "witness.txt", "content": "modified by isolated worker"}),
        # 子 Agent 第 2 轮：git 提交
        tool_round("s2", "Bash", {"command": 'git add -A && git commit -m "modify witness"'}),
        # 子 Agent 收尾
        [TextDelta("子任务完成。"), StreamEnd("stop")],
        # 主 Agent 收尾
        [TextDelta("已委派隔离子 Agent 完成修改。"), StreamEnd("stop")],
    ]
    make_isolated_worker(tmp_path)
    client = ScriptedClient(script)
    app, _ = make_app(tmp_path, client)

    await app._ask("创建 witness.txt，然后派隔离子 Agent 修改它")

    # 主目录原样、worktree 已修改并提交
    assert (
        (tmp_path / "witness.txt").read_text(encoding="utf-8") == "original content from main agent"
    )
    worktrees = list(app.worktree_manager.active.values())
    assert len(worktrees) == 1
    assert (
        (Path(worktrees[0].path) / "witness.txt").read_text(encoding="utf-8")
        == "modified by isolated worker"
    )

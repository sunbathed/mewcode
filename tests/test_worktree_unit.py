"""第 14 章：Worktree 系统单元测试。

覆盖：slug 安全校验、会话持久化、变更检测、创建后初始化、
WorktreeManager 生命周期（真实 git 仓库）、自动清理与过期回收、
上下文通知、注册中心重定向。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from mewcode.worktree import (
    Changes,
    WorktreeError,
    WorktreeManager,
    WorktreeSession,
    auto_cleanup,
    build_worktree_notice,
    cleanup_stale_worktrees,
    count_worktree_changes,
    flatten_slug,
    has_worktree_changes,
    is_ephemeral_name,
    load_worktree_session,
    load_worktree_settings,
    new_agent_worktree_name,
    reroot_registry,
    save_worktree_session,
    validate_slug,
)
from mewcode.worktree.changes import run_git
from mewcode.worktree.setup import _copy_ignored_files
from mewcode.tools import ToolRegistry
from mewcode.tools.base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# 测试基建：真实 git 仓库
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


class DummyTool(BaseTool):
    name = "DummyTool"
    description = "测试用自定义工具"
    safety = "read"

    async def _run(self, args) -> str:  # pragma: no cover - 不会真正执行
        return ""

    async def run(self, args) -> ToolResult:  # pragma: no cover
        return ToolResult()


# ---------------------------------------------------------------------------
# slug 安全校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,ok",
    [
        ("feature", True),
        ("feature/login", True),
        ("team-refactor/alice", True),
        ("v1.0", True),
        ("agent-a3f2b1c", True),
        ("my_work-tree.2", True),
        ("", False),
        ("a//b", False),  # 空段
        ("/abc", False),  # 前导空段
        ("abc/", False),  # 尾随空段
        (".", False),
        ("..", False),
        ("a/../b", False),  # 路径遍历
        ("a/./b", False),
        ("../../etc/passwd", False),  # 经典路径遍历
        ("a b", False),  # 空格不在白名单
        ("a*b", False),
        ("a/b c", False),
        ("x" * 65, False),  # 超长
        ("x" * 64, True),  # 恰好 64
    ],
)
def test_validate_slug(name, ok):
    err = validate_slug(name)
    assert (err is None) is ok, f"validate_slug({name!r}) = {err!r}"


def test_flatten_slug():
    assert flatten_slug("feature/login") == "feature+login"
    assert flatten_slug("team-refactor/alice") == "team-refactor+alice"
    assert flatten_slug("plain") == "plain"


def test_new_agent_worktree_name_matches_ephemeral_pattern():
    name = new_agent_worktree_name()
    assert name.startswith("agent-")
    assert is_ephemeral_name(name)


# ---------------------------------------------------------------------------
# 会话持久化
# ---------------------------------------------------------------------------


def test_session_roundtrip(tmp_path):
    session = WorktreeSession(
        original_cwd="/repo",
        worktree_path="/repo/.mewcode/worktrees/feature",
        worktree_name="feature",
        original_branch="main",
        original_head_commit="abc123",
        session_id="s1",
    )
    save_worktree_session(tmp_path, session)
    loaded = load_worktree_session(tmp_path)
    assert loaded == session


def test_save_none_writes_empty_object(tmp_path):
    save_worktree_session(tmp_path, None)
    assert (tmp_path / "worktree_session.json").read_text(encoding="utf-8") == "{}"
    assert load_worktree_session(tmp_path) is None


def test_load_missing_returns_none(tmp_path):
    assert load_worktree_session(tmp_path) is None


# ---------------------------------------------------------------------------
# 变更检测
# ---------------------------------------------------------------------------


async def test_count_changes_clean_worktree(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("clean")
    changes = count_worktree_changes(wt.path, wt.head_commit)
    assert changes == Changes(uncommitted=0, new_commits=0)
    assert not has_worktree_changes(wt.path, wt.head_commit)


async def test_count_changes_detects_edit_and_commit(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("dirty")

    # 未提交修改
    (Path(wt.path) / "README.md").write_text("modified\n", encoding="utf-8")
    changes = count_worktree_changes(wt.path, wt.head_commit)
    assert changes.uncommitted == 1 and changes.new_commits == 0

    # 新增提交（status 变干净但 rev-list 不为 0）
    git(Path(wt.path), "add", ".")
    git(Path(wt.path), "commit", "-m", "wip")
    changes = count_worktree_changes(wt.path, wt.head_commit)
    assert changes.uncommitted == 0 and changes.new_commits == 1
    assert has_worktree_changes(wt.path, wt.head_commit)


# ---------------------------------------------------------------------------
# WorktreeManager 生命周期
# ---------------------------------------------------------------------------


async def test_create_makes_dir_and_branch(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("feature/login")
    assert wt.name == "feature/login"
    assert Path(wt.path).is_dir()
    assert wt.branch == "worktree-feature+login"
    assert wt.based_on == "HEAD"
    # 分支确实存在且工作目录有完整文件
    assert "worktree-feature+login" in git(tmp_path, "branch", "--list")
    assert (Path(wt.path) / "README.md").is_file()


async def test_create_rejects_invalid_slug(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    with pytest.raises(WorktreeError, match="invalid worktree name"):
        await manager.create("../../etc")
    with pytest.raises(WorktreeError, match="invalid worktree name"):
        await manager.create("a/../b")


async def test_create_duplicate_name_fails(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    await manager.create("dup")
    with pytest.raises(WorktreeError, match="already exists"):
        await manager.create("dup")


async def test_fast_recovery_reuses_existing_dir(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt1 = await manager.create("recover")

    # 模拟进程重启：新 manager（active 为空），目录还在
    manager2 = WorktreeManager(repo_root=str(tmp_path))
    wt2 = await manager2.create("recover")
    assert wt2.path == wt1.path
    assert wt2.head_commit == wt1.head_commit
    assert wt2.head_commit  # 纯文件系统解析出了 HEAD SHA
    assert (Path(wt2.path) / "README.md").is_file()


async def test_read_head_sha_pure_filesystem(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("sha-test")
    expected = git(tmp_path, "rev-parse", "HEAD").strip()  # 新分支从 HEAD 分出
    assert manager.read_worktree_head_sha(wt.path) == expected
    # 非 worktree 目录返回 None
    assert manager.read_worktree_head_sha(str(tmp_path / "nope")) is None


async def test_enter_saves_session_and_exit_restores(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    await manager.create("feat")
    session = await manager.enter("feat", session_id="s-1")
    assert session.worktree_name == "feat"
    assert session.original_branch  # 记录了进入前的分支
    assert manager.current_session is session
    # 持久化到磁盘
    loaded = load_worktree_session(tmp_path / ".mewcode")
    assert loaded is not None and loaded.worktree_name == "feat"

    await manager.exit("feat", action="keep")
    assert manager.current_session is None
    # session 文件清成空对象（--resume 正确工作的前提）
    assert (tmp_path / ".mewcode" / "worktree_session.json").read_text(encoding="utf-8") == "{}"


async def test_enter_unknown_name_fails(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    with pytest.raises(WorktreeError, match="not found"):
        await manager.enter("nope")


async def test_exit_remove_with_changes_requires_discard(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("guard")
    (Path(wt.path) / "new-file.txt").write_text("precious work\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="discard_changes"):
        await manager.exit("guard", action="remove")
    assert Path(wt.path).is_dir()  # 保护生效，目录还在
    # 显式确认丢弃后才能删
    await manager.exit("guard", action="remove", discard_changes=True)
    assert not Path(wt.path).exists()
    assert "guard" not in manager.active


async def test_remove_clean_worktree_no_discard_needed(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    await manager.create("clean-rem")
    await manager.remove("clean-rem")
    assert "clean-rem" not in manager.active


async def test_list_merges_active_and_disk(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    await manager.create("listed")
    # 模拟恢复态：新 manager 只扫磁盘也能看到
    manager2 = WorktreeManager(repo_root=str(tmp_path))
    names = [wt.name for wt in manager2.list_worktrees()]
    assert "listed" in names or "worktree-listed" in names


async def test_restore_session_after_restart(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    await manager.create("resume-me")
    await manager.enter("resume-me")

    manager2 = WorktreeManager(repo_root=str(tmp_path))
    restored = manager2.restore_session()
    assert restored is not None
    assert restored.worktree_name == "resume-me"
    assert manager2.current_session is restored
    assert "resume-me" in manager2.active


async def test_restore_clears_when_worktree_deleted(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("gone")
    await manager.enter("gone")
    shutil.rmtree(wt.path)

    manager2 = WorktreeManager(repo_root=str(tmp_path))
    assert manager2.restore_session() is None
    # 持久化记录被清掉
    assert load_worktree_session(tmp_path / ".mewcode") is None


# ---------------------------------------------------------------------------
# 创建后初始化（setup.py）
# ---------------------------------------------------------------------------


async def test_setup_copies_local_configs(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("cfg")
    assert (Path(wt.path) / ".env").read_text(encoding="utf-8") == "API_KEY=secret\n"


async def test_setup_git_hooks_path(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("hooks")
    value = git(Path(wt.path), "config", "core.hooksPath").strip()
    # worktree 的 hooks 指回主仓库（.git/hooks 或 .husky）
    assert value.endswith("hooks") or value.endswith(".husky")


def test_setup_copies_ignored_files_via_worktreeinclude(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (tmp_path / ".worktreeinclude").write_text("*.secret\n", encoding="utf-8")
    (tmp_path / "keys.secret").write_text("token\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore", ".worktreeinclude")
    git(tmp_path, "commit", "-m", "ignore rules")

    wt_path = tmp_path / ".mewcode" / "worktrees" / "ignored-files"
    wt_path.mkdir(parents=True)
    _copy_ignored_files(tmp_path, wt_path)
    assert (wt_path / "keys.secret").read_text(encoding="utf-8") == "token\n"


async def test_symlink_directories_config(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x\n", encoding="utf-8")
    manager = WorktreeManager(repo_root=str(tmp_path), symlink_directories=["node_modules"])
    wt = await manager.create("linked")
    dst = Path(wt.path) / "node_modules"
    # Windows 无符号链接权限时 best-effort：不报错即可，能建则建
    if dst.exists() or dst.is_symlink():
        assert (dst / "lib.js").is_file()


async def test_symlink_skip_when_source_missing(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path), symlink_directories=["no-such-dir"])
    wt = await manager.create("no-link")
    assert not (Path(wt.path) / "no-such-dir").exists()


# ---------------------------------------------------------------------------
# 自动清理与过期回收
# ---------------------------------------------------------------------------


async def test_auto_cleanup_removes_clean_worktree(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create(new_agent_worktree_name())
    result = await auto_cleanup(manager, wt.name, wt.head_commit)
    assert result["kept"] is False
    assert wt.name not in manager.active
    assert not Path(wt.path).exists()


async def test_auto_cleanup_keeps_worktree_with_changes(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create(new_agent_worktree_name())
    (Path(wt.path) / "work.txt").write_text("agent output\n", encoding="utf-8")
    result = await auto_cleanup(manager, wt.name, wt.head_commit)
    assert result["kept"] is True
    assert result["path"] == wt.path
    assert Path(wt.path).is_dir()


async def test_auto_cleanup_never_touches_manual_names(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create("my-feature")
    result = await auto_cleanup(manager, wt.name, wt.head_commit)
    assert result["kept"] is True
    assert Path(wt.path).is_dir()


def _backdate(path: Path, hours_ago: float) -> None:
    ts = time.time() - hours_ago * 3600
    os.utime(path, (ts, ts))


async def test_stale_cleanup_removes_expired_ephemeral(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    agent_wt = await manager.create(new_agent_worktree_name())
    manual_wt = await manager.create("manual-keep")
    _backdate(Path(agent_wt.path), 48)
    _backdate(Path(manual_wt.path), 48)

    removed = await cleanup_stale_worktrees(manager, cutoff_hours=24)
    assert removed == 1
    assert not Path(agent_wt.path).exists()
    assert Path(manual_wt.path).is_dir()  # 手动命名永不被清理


async def test_stale_cleanup_keeps_recent_and_dirty(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    fresh_wt = await manager.create(new_agent_worktree_name())  # 未过期：mtime 保持最新
    dirty_wt = await manager.create(new_agent_worktree_name())
    (Path(dirty_wt.path) / "wip.txt").write_text("wip\n", encoding="utf-8")
    _backdate(Path(dirty_wt.path), 48)  # 过期但有未保存工作 → 仍保留

    removed = await cleanup_stale_worktrees(manager, cutoff_hours=24)
    assert removed == 0
    assert Path(fresh_wt.path).is_dir()  # 未过期
    assert Path(dirty_wt.path).is_dir()  # 有未保存工作


async def test_stale_cleanup_keeps_in_use_session(tmp_path):
    init_git_repo(tmp_path)
    manager = WorktreeManager(repo_root=str(tmp_path))
    wt = await manager.create(new_agent_worktree_name())
    _backdate(Path(wt.path), 48)
    await manager.enter(wt.name)

    removed = await cleanup_stale_worktrees(manager, cutoff_hours=24)
    assert removed == 0
    assert Path(wt.path).is_dir()


# ---------------------------------------------------------------------------
# 配置 / 通知 / 注册中心重定向
# ---------------------------------------------------------------------------


def test_load_worktree_settings(tmp_path):
    config_dir = tmp_path / ".mewcode"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "worktree:\n"
        "  symlink_directories:\n"
        "    - node_modules\n"
        "    - .venv\n"
        "  cleanup_interval_seconds: 600\n"
        "  stale_cutoff_hours: 48\n",
        encoding="utf-8",
    )
    settings = load_worktree_settings(str(tmp_path))
    assert settings.symlink_directories == ["node_modules", ".venv"]
    assert settings.cleanup_interval_seconds == 600
    assert settings.stale_cutoff_hours == 48


def test_load_worktree_settings_defaults(tmp_path):
    settings = load_worktree_settings(str(tmp_path))
    assert settings.symlink_directories == []
    assert settings.cleanup_interval_seconds == 3600
    assert settings.stale_cutoff_hours == 24


def test_build_worktree_notice():
    notice = build_worktree_notice("/repo", "/repo/.mewcode/worktrees/agent-x")
    assert "/repo/.mewcode/worktrees/agent-x" in notice
    assert "/repo" in notice
    assert "路径" in notice  # 提醒路径翻译


def test_reroot_registry_rebuilds_core_and_keeps_rest():
    base = ToolRegistry(workdir="/repo")
    dummy = DummyTool()
    base.register(dummy)

    re_rooted = reroot_registry(base, "/repo/.mewcode/worktrees/wt")
    # 核心文件工具以新 workdir 重建
    assert re_rooted.get("WriteFile").workdir == "/repo/.mewcode/worktrees/wt"
    assert re_rooted.get("Bash").workdir == "/repo/.mewcode/worktrees/wt"
    # 非核心工具实例原样保留
    assert re_rooted.get("DummyTool") is dummy
    # ToolSearch 指向新注册中心
    assert re_rooted.get("ToolSearch").registry is re_rooted

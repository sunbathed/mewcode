"""第 14 章：Worktree 变更检测。

退出保护与自动清理都依赖同一个判断：这个 worktree 里有没有未保存的
工作成果。检查同时覆盖两个维度：
  - 未提交的修改：git status --porcelain 的行数
  - 新增的提交：git rev-list --count {创建时 HEAD}..HEAD

两个维度都查，是因为子 Agent 可能已经 commit 了但还没 push——
这种情况 status 是干净的，但 rev-list 不是。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

# 所有 git 子进程统一注入：git 需要输入凭证时直接失败而不是挂起等输入
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


@dataclass(slots=True)
class Changes:
    """一个 worktree 的变更计数：未提交修改数 + 创建后新增提交数。"""

    uncommitted: int = 0
    new_commits: int = 0

    @property
    def has_changes(self) -> bool:
        return self.uncommitted > 0 or self.new_commits > 0


def run_git(args: list[str], *, cwd: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """运行一条 git 命令（文本模式，捕获 stdout/stderr，不抛异常）。"""
    import os

    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def count_worktree_changes(wt_path: str, head_commit: str) -> Changes:
    """统计 worktree 的未提交修改数与创建后新增提交数。"""
    changes = Changes()
    status = run_git(["status", "--porcelain"], cwd=wt_path)
    if status.returncode == 0:
        changes.uncommitted = len([line for line in status.stdout.splitlines() if line.strip()])

    if head_commit:
        rev_list = run_git(["rev-list", "--count", f"{head_commit}..HEAD"], cwd=wt_path)
        if rev_list.returncode == 0:
            try:
                changes.new_commits = int(rev_list.stdout.strip())
            except ValueError:
                changes.new_commits = 0
    return changes


def has_worktree_changes(wt_path: str, head_commit: str) -> bool:
    """worktree 是否有未保存的工作成果（未提交修改或新增提交）。

    fail-closed：git 命令本身执行失败时返回 True——宁可多保留一个
    目录也不误删，删除是破坏性操作，误留的代价远小于误删。
    """
    status = run_git(["status", "--porcelain"], cwd=wt_path)
    if status.returncode != 0:
        return True
    if any(line.strip() for line in status.stdout.splitlines()):
        return True
    if head_commit:
        rev_list = run_git(["rev-list", "--count", f"{head_commit}..HEAD"], cwd=wt_path)
        if rev_list.returncode != 0:
            return True
        try:
            if int(rev_list.stdout.strip() or "0") > 0:
                return True
        except ValueError:
            return True
    return False


def has_unpushed_commits(wt_path: str) -> bool:
    """worktree 里是否有未推送到远端的 commit。

    语义：相对远端而言。仓库没有配置任何 remote 时没有「推送」的
    概念，新增提交已由 has_worktree_changes 的新增维度兜住，这里
    返回 False 让清理流程继续（否则无远端仓库的清理永远失效）。
    """
    remotes = run_git(["remote"], cwd=wt_path)
    if remotes.returncode != 0 or not remotes.stdout.strip():
        return False
    rev_list = run_git(["rev-list", "--max-count=1", "HEAD", "--not", "--remotes"], cwd=wt_path)
    if rev_list.returncode != 0:
        return True  # fail-closed：查不出来就当作有未推送提交
    return bool(rev_list.stdout.strip())


__all__ = [
    "Changes",
    "GIT_ENV",
    "count_worktree_changes",
    "has_unpushed_commits",
    "has_worktree_changes",
    "run_git",
]

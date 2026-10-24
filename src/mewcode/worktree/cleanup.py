"""第 14 章：Worktree 自动清理（子 Agent 退出后的按需清理 + 过期回收）。

两个层次的清理能力：

  auto_cleanup            子 Agent 正常退出后的立即判断：worktree 里没有
                          未提交修改、也没有新增提交 → 直接清掉；有 →
                          保留让主 Agent review（路径与分支追加到结果里）
  cleanup_stale_worktrees 后台周期回收：进程崩溃留下的孤儿临时目录。
                          五层过滤（命名模式 → 使用中/未过期 → 变更 →
                          未推送），全部通过才删除

用户通过 /worktree create 手动创建的 worktree 名称不会匹配临时命名
模式，永远不会被自动清理——自动清理只动「自己创建的临时目录」。
宁可多占一点磁盘，也不丢失有价值的工作成果。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .changes import count_worktree_changes, has_unpushed_commits

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .manager import WorktreeManager

logger = logging.getLogger(__name__)

# 临时 worktree 的命名模式：
#   agent- 开头 + 7~8 位 hex —— 子 Agent 自动创建（a 是固定字面前缀）
#   wf_     开头 + hex      —— 工作流创建
EPHEMERAL_PATTERNS = [
    re.compile(r"^agent-[0-9a-f]{7,8}$"),
    re.compile(r"^wf_[0-9a-f]{8}"),
]

# agent- 前缀：auto_cleanup 的兜底防线（用户手动命名的绝不自动删）
AGENT_NAME_PREFIX = "agent-"


def is_ephemeral_name(name: str) -> bool:
    """名称是否匹配临时 worktree 的命名模式。"""
    return any(pattern.match(name) for pattern in EPHEMERAL_PATTERNS)


def new_agent_worktree_name() -> str:
    """子 Agent 用的临时 worktree 名：agent- + 8 位随机 hex。

    用唯一 ID 命名，避免同类型 Agent 并发时目录冲突。
    """
    import uuid

    return f"agent-{uuid.uuid4().hex[:8]}"


async def auto_cleanup(manager: "WorktreeManager", name: str, head_commit: str) -> dict:
    """子 Agent 完成后的自动清理：没修改就删，有修改就保留。

    返回 {kept, path, branch, reason}；kept=True 时主 Agent 可以去
    path/branch review 子 Agent 的工作成果。删除在返回前同步完成，
    调用方拿到 kept=False 时目录一定已经清掉了。
    """
    wt = manager.active.get(name)
    if wt is None:
        return {"kept": True, "path": "", "branch": "", "reason": "worktree not found"}
    # 兜底：手动创建的 worktree 不走自动清理（双保险，正常调用链
    # 只会用 agent- 前缀的临时名进来）
    if not name.startswith(AGENT_NAME_PREFIX):
        return {"kept": True, "path": wt.path, "branch": wt.branch, "reason": "not ephemeral"}
    if count_worktree_changes(wt.path, head_commit).has_changes:
        return {"kept": True, "path": wt.path, "branch": wt.branch, "reason": "has changes"}

    await manager.remove(name, discard_changes=False)
    return {"kept": False, "path": "", "branch": "", "reason": "clean"}


async def cleanup_stale_worktrees(manager: "WorktreeManager", cutoff_hours: int) -> int:
    """扫描 worktree 目录，清理过期且无价值的孤儿临时 worktree。返回删除数。

    五层过滤，全部通过才删除：
      1. 命名模式：只清理临时的（agent-* / wf_*）
      2. 使用中/未过期：跳过当前 session 正在用的和 mtime 未超时的
      3. 未提交修改：有就跳过
      4. 新增提交：创建后有 commit 就跳过（子 Agent 可能 commit 了没 push）
      5. 未推送提交：相对远端有未推送的 commit 也跳过（fail-closed）
    """
    import time as _time

    cutoff_ts = _time.time() - cutoff_hours * 3600
    removed = 0
    worktree_dir = Path(manager.worktree_dir)
    if not worktree_dir.is_dir():
        return 0
    for entry in sorted(worktree_dir.iterdir()):
        name = entry.name
        # 第 1 层：只清理临时 worktree
        if not entry.is_dir() or not is_ephemeral_name(name):
            continue
        # 第 2 层：跳过当前使用中和未过期的
        if manager.current_session is not None and Path(manager.current_session.worktree_path) == entry:
            continue
        try:
            if entry.stat().st_mtime > cutoff_ts:
                continue
        except OSError:
            continue
        head_sha = manager.read_worktree_head_sha(str(entry))
        if head_sha is None:
            # 不是合法 worktree（残留空目录），直接删
            _rmtree(entry)
            removed += 1
            continue
        # 第 3、4 层：未提交修改 / 新增提交（fail-closed）
        changes = count_worktree_changes(str(entry), head_sha)
        if changes.uncommitted > 0 or changes.new_commits > 0:
            continue
        # 第 5 层：未推送提交
        if has_unpushed_commits(str(entry)):
            continue
        # 全部通过才删除：git worktree remove + branch -D
        branch = f"worktree-{name}"
        remove_result = manager._run_git(["worktree", "remove", "--force", str(entry)])
        if remove_result.returncode != 0:
            logger.warning("Stale cleanup: remove %s failed: %s", name, remove_result.stderr.strip())
            continue
        await asyncio.sleep(0.1)
        manager._run_git(["branch", "-D", branch])
        manager.active.pop(name, None)
        removed += 1
    return removed


def _rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stale cleanup: rmtree %s failed: %s", path, exc)


async def start_stale_cleanup_task(
    manager: "WorktreeManager", interval: int, cutoff_hours: int
) -> None:
    """后台定时清理循环：先 sleep 再清理（首次不在启动时立刻发生）。

    整个循环 try/except 包裹：一次清理异常不中断后续的定时任务。
    由调用方以 asyncio.create_task 启动并在退出时 cancel。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            count = await cleanup_stale_worktrees(manager, cutoff_hours)
            if count:
                logger.info("Stale worktree cleanup removed %d worktrees", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 定时任务自身绝不崩
            logger.warning("Stale worktree cleanup error: %s", exc)


__all__ = [
    "AGENT_NAME_PREFIX",
    "EPHEMERAL_PATTERNS",
    "auto_cleanup",
    "cleanup_stale_worktrees",
    "is_ephemeral_name",
    "new_agent_worktree_name",
    "start_stale_cleanup_task",
]

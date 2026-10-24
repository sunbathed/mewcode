"""第 14 章：Worktree 的核心数据结构。

两个 dataclass 刻意分开两个关注点：
  - Worktree         资源本身：一个 worktree 的静态信息，可被多次进入使用
  - WorktreeSession  一次使用行为：进入前的现场快照，有明确的开始与结束
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Worktree:
    """一个 worktree 的静态信息（资源本身）。"""

    name: str              # 用户给的名字（含 / 的嵌套 slug）
    path: str              # 磁盘路径
    branch: str            # git 分支名（自动生成为 worktree-{flat_slug}）
    based_on: str          # 从哪个分支/提交创建
    head_commit: str       # 创建（或恢复）时的 HEAD SHA
    created: datetime = field(default_factory=datetime.now)


@dataclass(slots=True)
class WorktreeSession:
    """「进入」worktree 之前的状态快照（退出时才知道回到哪里）。"""

    original_cwd: str            # 进入前的工作目录
    worktree_path: str           # worktree 路径
    worktree_name: str           # slug 名称
    original_branch: str = ""    # 进入前所在的分支
    original_head_commit: str = ""  # 进入时的 HEAD commit SHA
    session_id: str = ""         # 关联的会话 ID（可空）
    hook_based: bool = False     # 是否由 Hook 驱动（区分手动进入与自动进入）


__all__ = ["Worktree", "WorktreeSession"]

"""第 14 章：Git Worktree 文件系统隔离。

给 SubAgent 补上最后一块拼图——文件系统级隔离。上一章的上下文隔离是
逻辑层面的（消息、权限、缓存各自独立），但文件系统共享；Worktree 提供
物理层面的隔离：每个子 Agent 在自己的独立工作目录中工作，彻底消除
并行场景下的文件冲突。

模块划分：
  slug.py        Slug 安全校验：防路径遍历（LLM 生成的名称不可信）
  models.py      数据结构：Worktree（资源）/ WorktreeSession（使用行为）
  session.py     会话状态持久化：JSON 读写，崩溃后可恢复
  changes.py     变更检测：未提交修改 + 新增提交（退出保护的基础）
  setup.py       创建后初始化：配置复制 / hooks / 软链接 / 忽略文件
  manager.py     WorktreeManager：Create/Enter/Exit/Remove/List/Restore
  cleanup.py     自动清理：auto_cleanup + 五层过滤的过期回收 + 定时任务
  integration.py SubAgent 集成：上下文通知 + 注册中心重定向 + 执行入口
"""

from .changes import Changes, count_worktree_changes, has_unpushed_commits, has_worktree_changes
from .cleanup import (
    auto_cleanup,
    cleanup_stale_worktrees,
    is_ephemeral_name,
    new_agent_worktree_name,
    start_stale_cleanup_task,
)
from .integration import build_worktree_notice, execute_with_worktree, reroot_registry
from .manager import (
    WorktreeError,
    WorktreeManager,
    WorktreeSettings,
    load_worktree_settings,
)
from .models import Worktree, WorktreeSession
from .session import SESSION_FILE, load_worktree_session, save_worktree_session
from .slug import MAX_SLUG_LENGTH, flatten_slug, validate_slug

__all__ = [
    "Changes",
    "MAX_SLUG_LENGTH",
    "SESSION_FILE",
    "Worktree",
    "WorktreeError",
    "WorktreeManager",
    "WorktreeSession",
    "WorktreeSettings",
    "auto_cleanup",
    "build_worktree_notice",
    "cleanup_stale_worktrees",
    "count_worktree_changes",
    "execute_with_worktree",
    "flatten_slug",
    "has_unpushed_commits",
    "has_worktree_changes",
    "is_ephemeral_name",
    "load_worktree_session",
    "load_worktree_settings",
    "new_agent_worktree_name",
    "reroot_registry",
    "save_worktree_session",
    "start_stale_cleanup_task",
    "validate_slug",
]

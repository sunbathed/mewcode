"""第 14 章：Worktree 与 SubAgent 的集成。

Worktree 和 SubAgent 是天生一对：SubAgent 隔离了上下文（消息、权限、
缓存），Worktree 隔离了文件系统。两者结合，子 Agent 拥有真正独立的
工作环境。本模块提供三块胶水：

  - build_worktree_notice  上下文通知模板：注入任务文本前，告诉子 Agent
                           它在隔离副本中工作、路径如何翻译
  - reroot_registry        把工具注册中心的根目录切到 worktree
                           （本实现的显式 cwd 机制：工具自带 workdir）
  - execute_with_worktree  完整生命周期：建目录 → 注入通知 → 运行子 Agent
                           → 自动清理（没修改就删，有修改就保留）

没有通知会怎样？子 Agent 不知道自己在隔离副本里，可能直接用父 Agent
传来的绝对路径读写文件（那些路径指向主目录），或者用对话里的旧内容
理解 Worktree 里的文件，产生认知偏差。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..tools import ToolRegistry, ToolSearch
from ..tools.bash import Bash
from ..tools.files import EditFile, ReadFile, WriteFile
from ..tools.search import Glob, Grep
from .cleanup import auto_cleanup, new_agent_worktree_name
from .manager import WorktreeError, WorktreeManager

logger = logging.getLogger(__name__)

# 随目录根变化、需要以新 workdir 重建的核心文件工具
_CORE_TOOL_NAMES = frozenset({"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"})


def build_worktree_notice(original_cwd: str, worktree_path: str) -> str:
    """构造注入子 Agent 任务文本前的 Worktree 上下文通知。

    三件事：你在独立 Git Worktree 中工作；父上下文里的路径指向原目录，
    需要翻译；编辑前重新读取 Worktree 里的版本。
    """
    return (
        "[Worktree 隔离说明]\n"
        f"- 你在一个独立的 Git Worktree 中工作，工作目录：{worktree_path}\n"
        f"- 你继承了父 Agent 的对话上下文，但其中提到的文件路径指向原目录：{original_cwd}\n"
        "- 父上下文里的路径需要翻译：把原目录前缀替换为上面的 Worktree 路径，再操作文件\n"
        "- 编辑任何文件前先用 ReadFile 重新读取 Worktree 里的版本，不要沿用对话中的旧内容\n"
        "- 完成任务后如需保留成果，用 git 在 Worktree 中提交你的修改"
    )


def reroot_registry(base_registry: ToolRegistry | None, workdir: str) -> ToolRegistry:
    """重建一个以 workdir 为根目录的工具注册中心。

    核心文件工具（ReadFile/WriteFile/EditFile/Bash/Glob/Grep）以新
    workdir 重新构造——它们在构造时固定根目录，这是本实现「显式 cwd」
    的落点；其余工具（Agent/Task*/LoadSkill/MCP）实例与目录无关，
    直接复用。ToolSearch 持有注册中心引用，构造时已绑定新注册中心。
    """
    fresh = ToolRegistry(workdir=workdir)
    if base_registry is not None:
        for name, tool in base_registry._tools.items():  # noqa: SLF001 - 同包内复用内部表
            if name in _CORE_TOOL_NAMES or name == "ToolSearch":
                continue
            try:
                fresh.register(tool)
            except Exception:  # noqa: BLE001 - 单个工具注册失败不影响整体
                continue
    return fresh


async def execute_with_worktree(
    manager: WorktreeManager | None,
    original_cwd: str,
    definition: Any,
    params: Any,
    *,
    run_sub_agent: Callable[[str, str], Awaitable[str]],
) -> str:
    """Worktree 隔离的子 Agent 执行入口（完整生命周期）。

    run_sub_agent(worktree_path, task) 由调用方（Agent 工具）注入：
    负责以 worktree 为根构建子 Agent 并跑完任务，返回结果文本。

    流程：创建 worktree（agent- + 随机 ID 命名，避免并发冲突）→
    在任务文本前注入上下文通知 → 运行 → autoCleanup（没修改就删，
    有修改就保留并把路径/分支追加到结果，主 Agent 据此 review）。
    执行失败时保留现场（fail-closed），绝不因清理动作丢失工作成果。
    """
    if manager is None:
        return "Error: worktree manager is not available (工作目录不是 git 仓库？)"

    wt_name = new_agent_worktree_name()
    try:
        wt = await manager.create(wt_name, "HEAD")
    except WorktreeError as exc:
        return f"Cannot create worktree: {exc}"

    notice = build_worktree_notice(original_cwd, wt.path)
    task = f"{notice}\n\n{params.prompt}"

    try:
        result_text = await run_sub_agent(wt.path, task)
    except Exception as exc:  # noqa: BLE001 - 子 Agent 失败保留现场
        logger.warning("Worktree sub-agent failed, keeping worktree %s: %s", wt.path, exc)
        return (
            f"Sub-agent failed: {exc}\n"
            f"[Worktree 保留在 {wt.path}，分支 {wt.branch}，便于排查] "
            f"如需丢弃请用 /worktree remove {wt_name} --discard"
        )

    cleanup = await auto_cleanup(manager, wt_name, wt.head_commit)
    if cleanup.get("kept"):
        result_text += (
            f"\n[Worktree 保留在 {cleanup.get('path', wt.path)}，"
            f"分支 {cleanup.get('branch', wt.branch)}]（含未合并的修改，可 review 后合并或丢弃）"
        )
    return result_text


__all__ = [
    "build_worktree_notice",
    "execute_with_worktree",
    "new_agent_worktree_name",
    "reroot_registry",
]

"""第 15 章：Coordinator Mode——让 Lead 专注调度。

独立于 Agent Team 的可选能力：进入后 Lead 被剥夺写代码的工具
（WriteFile / EditFile），只保留调度（Agent / TeamCreate / TeamDelete /
SendMessage / Task*）和读类操作（ReadFile / Glob / Grep / Bash）。
Bash 保留是因为收敛阶段 Lead 要跑 git merge；读类保留是因为
Synthesis 阶段 Lead 得消化队员的研究结果、写实施规格。

双锁激活，两把都打开才能进入：
  - 第一把：feature flag（.mewcode/config.yaml 的 features.coordinatorMode）
    ——开发者按需开关这个能力
  - 第二把：MEWCODE_COORDINATOR_MODE 环境变量——用户显式 opt-in，
    避免在不知情的情况下改变 Lead 的行为模式
"""

from __future__ import annotations

import os
from pathlib import Path

from ..tools import ToolRegistry
from ..tools.base import Tool

# Coordinator 模式下 Lead 允许使用的工具（调度 + 读类；无 WriteFile/EditFile）
COORDINATOR_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "Agent",
        "SendMessage",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "TeamCreate",
        "TeamDelete",
        "ReadFile",
        "Glob",
        "Grep",
        "Bash",
    }
)


# ---------------------------------------------------------------------------
# 双锁激活
# ---------------------------------------------------------------------------


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def coordinator_env_enabled() -> bool:
    """第二把锁：用户是否显式设置了 MEWCODE_COORDINATOR_MODE。"""
    return _env_truthy(os.environ.get("MEWCODE_COORDINATOR_MODE"))


def coordinator_feature_enabled(workdir: str | Path) -> bool:
    """第一把锁：配置文件是否开放该能力（features.coordinatorMode / coordinator_mode）。"""
    config_path = Path(workdir).expanduser() / ".mewcode" / "config.yaml"
    if not config_path.is_file():
        return False
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置读不出来视为未开启
        return False
    if not isinstance(raw, dict):
        return False
    features = raw.get("features")
    if not isinstance(features, dict):
        return False
    return bool(features.get("coordinatorMode") or features.get("coordinator_mode"))


def is_coordinator_mode(enable_flag: bool = False) -> bool:
    """双锁判定：feature flag 开放 且 环境变量显式开启。"""
    if not enable_flag:
        return False
    return _env_truthy(os.environ.get("MEWCODE_COORDINATOR_MODE"))


def match_session_mode(session_mode: str | None, enable_flag: bool = False) -> str | None:
    """恢复会话时调整当前模式以匹配之前的状态（重启后行为一致）。

    返回需要写回会话的模式值；None 表示无需变更。
    """
    current = is_coordinator_mode(enable_flag)
    session_is_coordinator = session_mode == "coordinator"
    if current == session_is_coordinator:
        return None
    if session_is_coordinator:
        os.environ["MEWCODE_COORDINATOR_MODE"] = "1"
        return "coordinator"
    os.environ.pop("MEWCODE_COORDINATOR_MODE", None)
    return ""


# ---------------------------------------------------------------------------
# 工具集收窄
# ---------------------------------------------------------------------------


def build_coordinator_registry(
    base_registry: ToolRegistry | None,
    team_tools: dict[str, Tool],
) -> ToolRegistry:
    """构建 Coordinator 模式的收窄注册中心。

    以白名单过滤宿主工具集，再注册团队工具。team_tools 里的
    TaskCreate/TaskGet/TaskList/TaskUpdate 是共享任务板版本，注册在
    白名单过滤之后——同名覆盖宿主的后台任务版本（Coordinator 关心的是
    团队任务板，不是后台子 Agent 任务）。
    """
    fresh = ToolRegistry()
    for name in list(fresh.names()):
        fresh.unregister(name)
    if base_registry is not None:
        for name, tool in base_registry._tools.items():  # noqa: SLF001 - 同包内复用内部表
            if name in COORDINATOR_MODE_ALLOWED_TOOLS:
                fresh.register(tool)
    for name, tool in team_tools.items():
        fresh.register(tool)
    return fresh


# ---------------------------------------------------------------------------
# 系统提示词与用户上下文
# ---------------------------------------------------------------------------

_COORDINATOR_PROMPT = """\
# Coordinator Mode（协调者模式）

你是团队的 Lead coordinator。你已经交出直接写代码的权力（没有 WriteFile/EditFile），
你的职责是理解、拆解、调度与判断。所有代码修改都由 worker（队员）完成。

## 四阶段工作流

把大部分任务分解为四个阶段推进：

1. **Research**（队员，可并行）：派出 worker 调查代码库、定位文件、理解问题。
   每个 worker 只负责一个聚焦的调查方向。
2. **Synthesis**（你自己）：阅读调查结果，理解问题，撰写实施规格。
   这是最关键的一步——理解能力不能委托出去。
3. **Implementation**（队员）：按规格修改代码、提交。规格必须具体到文件路径和行号。
4. **Verification**（队员）：验证必须由独立的 worker 执行——不要让改代码的
   worker 自己验证自己，用新的、无历史包袱的 worker 跑测试并报告结果。

## 反面模式：lazy delegation（头号反模式）

不要把理解委托给队员。你自己消化研究结果后，给出具体的、包含文件路径
和行号的指令：

反面（禁止）：
  Agent(prompt="基于你的调研结果，修复认证 bug")

正确：
  Agent(prompt="修复 src/auth/validate.ts:42 的空指针。Session 类型的 user 字段
  在 session 过期但 token 仍在缓存时为 undefined。在访问 user.id 之前加空值检查，
  如果为 null，返回 401 'Session expired'。提交并报告 commit hash。")

## 工作规则

- 用 TaskCreate 建任务、TaskUpdate 维护状态与依赖（addBlockedBy/addBlocks），
  让队员通过共享任务板认领工作；你的纯文本回复队员看不到。
- 用 SendMessage 给指定队员发指令（to="名字或ID"）；收工判断用成员空闲通知，
  全员空闲且任务完成后用 git 合并各队员 worktree 分支，然后 TeamDelete 清理。
- 队员完成任务后会以 idle 消息通知你；续写已停止的队员直接 SendMessage 给它，
  系统会从磁盘 transcript 恢复它的上下文。
- 派生队员：Agent 工具传 team_name（加入团队）与 name（队员名），队员自动
  获得独立 worktree 与协作工具（共享任务板 + SendMessage）。
"""


def get_coordinator_system_prompt() -> str:
    """Coordinator 系统提示词：四阶段工作流 + 反模式 + 工作规则。"""
    return _COORDINATOR_PROMPT


def get_coordinator_user_context(worker_tools: list[str] | None = None) -> dict[str, str]:
    """告诉 Lead 每个 worker 能用哪些工具，避免分配超出能力范围的任务。"""
    if worker_tools is None:
        from .teammate import IN_PROCESS_TEAMMATE_ALLOWED_TOOLS

        tools_str = ", ".join(sorted(IN_PROCESS_TEAMMATE_ALLOWED_TOOLS))
    else:
        tools_str = ", ".join(sorted(worker_tools))
    return {
        "workerToolsContext": (
            f"Workers spawned via the Agent tool have access to these tools: {tools_str}"
        )
    }


__all__ = [
    "COORDINATOR_MODE_ALLOWED_TOOLS",
    "build_coordinator_registry",
    "coordinator_env_enabled",
    "coordinator_feature_enabled",
    "get_coordinator_system_prompt",
    "get_coordinator_user_context",
    "is_coordinator_mode",
    "match_session_mode",
]

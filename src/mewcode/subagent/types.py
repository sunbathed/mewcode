"""第 13 章：SubAgent 系统的核心数据结构。

一个 AgentDef 是子 Agent 的完整「蓝图」：定义它是什么角色、能做什么、
不能做什么、用什么模型、最多跑多少轮。AgentTool 通过 subagent_type
参数引用它，加载器按优先级从多个来源收集定义。

- AgentDef         子 Agent 定义（frontmatter + body 解析后的结果）
- AgentToolParams  Agent 工具的参数（prompt/description 必填，其余可选）
- ForkError        Fork 路径的异常（嵌套检测等）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 定义来源优先级：离使用场景越近越有话语权
SOURCE_PROJECT = "project"
SOURCE_USER = "user"
SOURCE_BUILTIN = "builtin"
SOURCE_PLUGIN = "plugin"


@dataclass(slots=True)
class AgentDef:
    """子 Agent 定义：角色、能力边界、模型与执行参数。"""

    agent_type: str          # frontmatter 的 name
    when_to_use: str         # frontmatter 的 description
    system_prompt: str = ""  # Markdown body，注入一次伴随子 Agent 整个生命周期
    tools: list[str] = field(default_factory=list)          # 工具白名单（可空=不限）
    disallowed_tools: list[str] = field(default_factory=list)  # 工具黑名单
    model: str = "inherit"   # inherit / sonnet / opus / haiku
    max_turns: int = 50      # 最大循环次数
    permission_mode: str = "default"  # default / acceptEdits / dontAsk / bypassPermissions
    background: bool = False  # 定义级后台标记（如 Verification Agent）
    isolation: str = ""       # "" / "worktree"（下一章实现，字段先占位）
    file_path: Path | None = None
    source: str = SOURCE_BUILTIN


@dataclass(slots=True)
class AgentToolParams:
    """Agent 工具的调用参数（由模型生成）。"""

    prompt: str = ""          # 必填：给子 Agent 的任务指令
    description: str = ""     # 必填：任务用途说明（帮助 Agent 工具合理选择）
    subagent_type: str = ""   # 可选：预定义的 Agent 类型；留空走 Fork 路径
    model: str = ""           # 可选：覆盖定义文件的模型
    run_in_background: bool = False  # 可选：同步还是异步执行
    name: str = ""            # 可选：给子 Agent 命名（TaskManager 展示用）
    isolation: str = ""       # 可选："" / "worktree"（参数级覆盖定义级）
    team_name: str = ""       # 可选：第 15 章——加入该团队成为队员（team_name 非空走 Team 路径）


class ForkError(RuntimeError):
    """Fork 路径异常（Fork 嵌套、无活跃对话等）。"""


__all__ = [
    "AgentDef",
    "AgentToolParams",
    "ForkError",
    "SOURCE_BUILTIN",
    "SOURCE_PLUGIN",
    "SOURCE_PROJECT",
    "SOURCE_USER",
]
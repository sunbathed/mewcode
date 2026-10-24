"""第 13 章：SubAgent 工具过滤的多层防线。

子 Agent 的工具集不能和父 Agent 一样——否则 A 可以 spawn B，B 可以
spawn C，无限嵌套下去。过滤按五层递进，每层只能进一步缩小工具集：

  Layer 0：MCP 工具直通（mcp__ 前缀不参与过滤，最后加回结果）
  Layer 1：全局禁止列表 ALL_AGENT_DISALLOWED_TOOLS（所有子 Agent 都不能用）
  Layer 2：自定义 Agent 额外限制 CUSTOM_AGENT_DISALLOWED_TOOLS
  Layer 3：后台模式白名单 ASYNC_AGENT_ALLOWED_TOOLS
  Layer 4：定义级 tools（白名单）+ disallowedTools（黑名单）

过滤执行顺序：MCP 分离 → 全局黑名单 → 自定义限制 → 后台白名单 →
定义级黑名单排除 → 定义级白名单取交集 → MCP 加回。
"""

from __future__ import annotations

from ..tools import ToolRegistry
from .types import SOURCE_BUILTIN, AgentDef

# 第 1 层：全局禁止——任何 SubAgent 都不能用
# Agent 被禁防无限嵌套；AskUserQuestion 被禁因为子 Agent 没有直接面向
# 用户的 UI；ExitPlanMode/EnterPlanMode 防止子 Agent 切换模式影响全局状态。
ALL_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "TaskOutput",
        "ExitPlanMode",
        "EnterPlanMode",
        "Agent",
        "AskUserQuestion",
        "TaskStop",
        "Workflow",
    }
)

# 第 2 层：自定义 Agent 额外限制（当前与全局同内容，独立集合便于
# 将来给自定义 Agent 加额外限制而不影响全局列表）
CUSTOM_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "TaskOutput",
        "ExitPlanMode",
        "EnterPlanMode",
        "Agent",
        "AskUserQuestion",
        "TaskStop",
        "Workflow",
    }
)

# 第 3 层：后台 Agent 白名单——后台运行只能用基础工具
# 注意：没有 Agent（不能再 spawn 子 Agent）、没有 Task* 协调工具
ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "ReadFile",
        "WriteFile",
        "EditFile",
        "Glob",
        "Grep",
        "Bash",
        "ToolSearch",
        "LoadSkill",
        "Skill",
    }
)


def _is_mcp_tool(name: str) -> bool:
    """MCP 工具名带 mcp_ 前缀（由 MCPManager 注册：mcp_{server}_{tool}）。"""
    return name.startswith("mcp_")


def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDef,
    *,
    is_background: bool,
) -> ToolRegistry:
    """从父 Agent 工具集过滤出子 Agent 的工具注册中心（五层递进）。"""
    # Layer 0：MCP 工具直通——先分离，最后加回
    mcp_tools = {name: tool for name, tool in parent_registry._tools.items() if _is_mcp_tool(name)}

    # 以未缩减的完整工具集为起点（协调器模式父 Agent 的 registry 可能已缩减）
    all_tools: dict[str, object] = {
        name: tool for name, tool in parent_registry._tools.items() if not _is_mcp_tool(name)
    }

    # Layer 1：全局黑名单
    for name in ALL_AGENT_DISALLOWED_TOOLS:
        all_tools.pop(name, None)

    # Layer 2：自定义 Agent 额外限制
    if definition.source != SOURCE_BUILTIN:
        for name in CUSTOM_AGENT_DISALLOWED_TOOLS:
            all_tools.pop(name, None)

    # Layer 3：后台模式白名单
    if is_background:
        all_tools = {name: tool for name, tool in all_tools.items() if name in ASYNC_AGENT_ALLOWED_TOOLS}

    # Layer 4：定义级限制——黑名单先排除，白名单再取交集
    if definition.disallowed_tools:
        for name in definition.disallowed_tools:
            all_tools.pop(name, None)
    if definition.tools:
        allowed_set = set(definition.tools)
        all_tools = {name: tool for name, tool in all_tools.items() if name in allowed_set}

    # MCP 工具加回：始终可用（它们有自己独立的权限控制）
    all_tools.update(mcp_tools)

    # 组装新注册中心：先清掉构造时自动注册的基础工具，再注册过滤后的工具集
    # （工具实例来自父 Agent，自带 workdir，新 registry 的 workdir 仅影响临时工具）
    filtered = ToolRegistry()
    for name in list(filtered.names()):
        filtered.unregister(name)
    for name, tool in all_tools.items():
        try:
            filtered.register(tool)
        except Exception:  # noqa: BLE001 - 单个工具注册失败不影响整体
            continue
    return filtered


__all__ = [
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "CUSTOM_AGENT_DISALLOWED_TOOLS",
    "resolve_agent_tools",
]
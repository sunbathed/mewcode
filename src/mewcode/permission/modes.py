"""第四道防线：权限模式。

规则是细粒度的控制，但大多数场景下用户不想一条一条写规则，
只想说「我大概信任 Agent 到什么程度」。MewCode 支持四种权限模式，
覆盖从完全不信任到完全信任的整个光谱：

| 模式              | 只读工具 | 文件写工具 | Bash |
| default           | Allow    | Ask       | Ask  |
| acceptEdits       | Allow    | Allow     | Ask  |
| plan              | Allow    | Ask       | Ask  |
| bypassPermissions | Allow    | Allow     | Allow |

- default：只读自动放行，写操作和命令执行需用户确认（最常用）。
- acceptEdits：文件编辑自动放行，只有 Bash 还需确认。
- plan：矩阵与 default 一致；Plan Mode 的只读约束靠 System Prompt，
  即使模型不听话，Ask 机制仍会弹出确认框。
- bypassPermissions：跳过第 3-5 层（规则、模式、HITL），但黑名单和沙箱仍生效。

第 13 章：dontAsk 是 bypassPermissions 的语义别名（子 Agent 用）——
Fork 默认、自定义 Agent 可配。子 Agent 跑在后台没有 UI 可弹确认框，
而 disallowedTools 已锁死能力边界（排除写工具与命令），自动批准安全。

决策矩阵本身就是决策逻辑的完整表达，按表对照写判断即可。
"""

from __future__ import annotations

import enum
from typing import Any

# 工具安全分类（与 tools.base 一致）
SAFETY_READ = "read"
SAFETY_WRITE = "write"
SAFETY_COMMAND = "command"


class PermissionMode(enum.StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS_PERMISSIONS = "bypassPermissions"


# 别名：实战篇验证里把 bypassPermissions 叫做 YOLO 模式；
# 第 13 章子 Agent 用 dontAsk（后台无 UI 可弹确认框）
# 键统一全小写，parse_mode 入参会先 lower() 再匹配
MODE_ALIASES: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "acceptedits": PermissionMode.ACCEPT_EDITS,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "accept-edits": PermissionMode.ACCEPT_EDITS,
    "plan": PermissionMode.PLAN,
    "bypasspermissions": PermissionMode.BYPASS_PERMISSIONS,
    "bypass_permissions": PermissionMode.BYPASS_PERMISSIONS,
    "bypass": PermissionMode.BYPASS_PERMISSIONS,
    "yolo": PermissionMode.BYPASS_PERMISSIONS,
    "dontask": PermissionMode.BYPASS_PERMISSIONS,
}


def parse_mode(value: Any, default: PermissionMode = PermissionMode.DEFAULT) -> PermissionMode:
    """把字符串解析为权限模式；未知值返回 default。"""
    if isinstance(value, PermissionMode):
        return value
    return MODE_ALIASES.get(str(value).strip().lower(), default)


def decide_mode(mode: PermissionMode, safety: str) -> str:
    """根据模式与工具安全分类返回决策：allow / ask。

    注意：本函数只处理「模式层」的 Allow / Ask 决策；
    黑名单与沙箱是硬防线，不走这里。
    """
    if mode == PermissionMode.BYPASS_PERMISSIONS:
        return "allow"
    if safety == SAFETY_READ:
        return "allow"
    if safety == SAFETY_WRITE and mode == PermissionMode.ACCEPT_EDITS:
        return "allow"
    return "ask"


def infer_safety(tool_name: str, safety_map: dict[str, str] | None = None) -> str:
    """根据工具名推断安全分类；未知工具按 command 保守处理。

    优先使用调用方提供的 safety_map（例如从 ToolRegistry 的 spec 收集），
    未覆盖的工具名回退到内置推断。
    """
    if safety_map and tool_name in safety_map:
        return safety_map[tool_name]
    if tool_name in ("ReadFile", "Glob", "Grep", "LoadSkill", "ToolSearch"):
        # LoadSkill / ToolSearch：系统工具，只改 Agent 内部状态，走只读通道
        return SAFETY_READ
    if tool_name in ("WriteFile", "EditFile", "ReplaceInFile"):
        return SAFETY_WRITE
    return SAFETY_COMMAND


__all__ = [
    "SAFETY_READ",
    "SAFETY_WRITE",
    "SAFETY_COMMAND",
    "PermissionMode",
    "MODE_ALIASES",
    "parse_mode",
    "decide_mode",
    "infer_safety",
]
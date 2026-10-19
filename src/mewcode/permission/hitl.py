"""第五道防线：HITL 人在回路。

当前四层都无法做出明确决策时（规则没匹配上，模式说「需要确认」），
权限系统会暂停 Agent Loop，弹出确认对话框让用户亲自决定。

核心思路（理论篇）：
- Agent 在需要确认时发送 PermissionRequestEvent 到事件流，然后阻塞等待 response_future；
- UI 层收到事件后渲染确认对话框；
- 用户按下 y / n / a（或 1 / 2 / 3）后，UI 把决策 resolve 到 future；
- Agent 收到回复后继续执行。

「始终允许」是关键设计：系统自动生成一条 allow 规则追加到本地配置文件，
下次同类操作自动放行，形成权限学习循环。
"""

from __future__ import annotations

import enum
import json
from typing import Any


class PermissionResponse(enum.StrEnum):
    """用户在 HITL 对话框中的回复。"""

    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


def build_permission_description(tool_name: str, tool_input: dict[str, Any]) -> str:
    """从工具调用构建确认对话框里展示的描述信息。"""
    for key in ("command", "path", "pattern"):
        if key in tool_input:
            value = str(tool_input[key]).replace("\n", " ")
            if len(value) > 200:
                value = value[:200] + "…"
            return value
    return json.dumps(tool_input, ensure_ascii=False)


def build_allow_rule_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """根据工具调用生成「始终允许」规则的 rule 文本。

    例如 Bash(git commit -m "...") 会生成 Bash(git commit *)，
    让同类操作（git commit 开头）后续自动放行。
    文件类工具直接用 path 参数作为模式（精确匹配）。
    """
    if tool_name == "Bash":
        content = str(tool_input.get("command", "")).strip()
        if content:
            # 取前两个词作为前缀，后面用 * 通配
            words = content.split()
            prefix = " ".join(words[:2]) if len(words) >= 2 else words[0]
            return f"{tool_name}({prefix} *)"
        return f"{tool_name}(*)"
    # 文件/搜索类工具：用 path/pattern 作为精确匹配模式
    for key in ("path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}({value.strip()})"
    return f"{tool_name}(*)"


__all__ = ["PermissionResponse", "build_permission_description", "build_allow_rule_text"]
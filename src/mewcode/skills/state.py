"""Skill 激活状态：已激活 Skill 的 SOP 全文的持久源（env context）。

两阶段加载的第二阶段：LoadSkill 把完整 SKILL.md 钉到「环境上下文」，
每次 Agent Loop 迭代开头重新构建 request 时都从这里取内容，
已激活的 Skill 永远在最显眼的位置，不会被对话历史冲走。

激活的 Skill 跟着对话走：/clear 时调用 clear() 一并清空，
避免新对话里残留上一轮激活的 SOP。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SkillActivationState:
    """跨轮保存已激活 Skill 的 env context。"""

    # name -> 渲染后的 SOP 文本（含 $ARGUMENTS 替换结果）
    active: dict[str, str] = field(default_factory=dict)
    # name -> 该 Skill 的 allowedTools 白名单（用于工具过滤）
    active_allowed_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # name -> 该 Skill 声明的专属工具名列表（已注册进会话）
    active_tools: dict[str, list[str]] = field(default_factory=dict)

    def activate(
        self,
        skill_name: str,
        sop_text: str,
        tool_names: list[str] | None = None,
        allowed_tools: tuple[str, ...] = (),
    ) -> None:
        """把某个 Skill 的完整 SOP 钉到环境上下文。"""
        self.active[skill_name] = sop_text
        if allowed_tools:
            self.active_allowed_tools[skill_name] = tuple(allowed_tools)
        if tool_names:
            self.active_tools[skill_name] = list(tool_names)

    def is_active(self, skill_name: str) -> bool:
        return skill_name in self.active

    def names(self) -> list[str]:
        return list(self.active)

    def sop_texts(self) -> list[str]:
        """所有已激活 SOP 的文本列表（每次请求构建 env context 时注入）。"""
        return list(self.active.values())

    def allowed_tools_union(self) -> tuple[str, ...]:
        """所有激活 Skill 白名单的并集（供 allowedTools 过滤）。"""
        result: list[str] = []
        seen: set[str] = set()
        for tools in self.active_allowed_tools.values():
            for name in tools:
                if name not in seen:
                    seen.add(name)
                    result.append(name)
        return tuple(result)

    def active_tool_names(self) -> list[str]:
        """所有已激活 Skill 声明的专属工具名（用于注册进当前会话）。"""
        result: list[str] = []
        for names in self.active_tools.values():
            result.extend(names)
        return result

    def clear(self) -> None:
        """清空所有激活的 Skill（/clear 联动）。"""
        self.active.clear()
        self.active_allowed_tools.clear()
        self.active_tools.clear()


__all__ = ["SkillActivationState"]
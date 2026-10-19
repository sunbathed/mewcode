from __future__ import annotations

import fnmatch
from typing import Any

from .base import Tool, ToolResult, ToolSpec
from .bash import Bash
from .files import EditFile, ReadFile, ReplaceInFile, WriteFile
from .search import Glob, Grep
from .tool_search import ToolSearch


class ToolNotFoundError(KeyError):
    """请求的工具未注册。"""


class ToolDisabledError(RuntimeError):
    """请求的工具已注册但被禁用。"""


class ToolRegistry:
    """集中管理工具的注册、启用、禁用，并转换为 API 描述格式。

    支持工具延迟加载（第七章）：延迟工具（如 MCP 工具）的完整 schema
    默认不进模型工具列表，只登记名字；模型经 ToolSearch 发现后，
    从下一轮开始出现在正常工具列表里。
    """

    def __init__(self, *, workdir: str | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        # 已被 ToolSearch 发现的延迟工具名（之后作为完整 schema 交付给模型）
        self._discovered: set[str] = set()
        # 默认注册六个核心工具
        self.register(ReadFile(workdir=workdir))
        self.register(WriteFile(workdir=workdir))
        self.register(EditFile(workdir=workdir))
        self.register(Bash(workdir=workdir))
        self.register(Glob(workdir=workdir))
        self.register(Grep(workdir=workdir))
        # 延迟加载入口：始终可见（本身不延迟）
        self.register(ToolSearch(self))

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("工具必须设置名称")
        self._tools[tool.name] = tool
        self._disabled.discard(tool.name)
        self._discovered.discard(tool.name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._disabled.discard(name)
        self._discovered.discard(name)

    def enable(self, name: str) -> None:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        self._disabled.add(name)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return self._tools[name]

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if name in self._disabled:
            return ToolResult(is_error=True, error="工具已禁用，请向用户确认后重新请求")
        return await tool.run(args)

    def enabled_tools(self) -> list[Tool]:
        return [tool for name, tool in self._tools.items() if name not in self._disabled]

    def api_specs(self) -> list[dict[str, Any]]:
        """启用中的工具转换为模型可识别的 API 描述。

        延迟工具（未发现前）不出现：它们的完整 schema 每轮都带着会
        浪费大量 token，只在 system-reminder 里列出名字。
        """
        return [
            tool.to_api_format()
            for name, tool in self._tools.items()
            if name not in self._disabled and not self._is_deferred_undiscovered(name)
        ]

    def tool_specs(self) -> list[Any]:
        return [tool.spec for tool in self.enabled_tools()]

    def names(self) -> list[str]:
        return list(self._tools)

    # ------------------------------------------------------------------
    # 延迟加载（第七章：工具延迟加载）
    # ------------------------------------------------------------------

    def _is_deferred_undiscovered(self, name: str) -> bool:
        """该工具声明了延迟加载、且尚未被 ToolSearch 发现。"""
        tool = self._tools.get(name)
        return tool is not None and tool.should_defer() and name not in self._discovered

    def deferred_names(self) -> list[str]:
        """所有「已注册但尚未被发现」的延迟工具名列表。"""
        return [name for name, tool in self._tools.items() if tool.should_defer() and name not in self._discovered]

    def mark_discovered(self, name: str) -> None:
        """把工具标记为已发现：下一轮起其完整 schema 进入模型工具列表。"""
        if name in self._tools:
            self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    def search_deferred(self, query: str) -> list[ToolSpec]:
        """按名称或关键词搜索延迟工具，返回匹配的 spec 列表（不含已发现项）。

        支持两种查询模式（对应理论篇 ToolSearch）：
          - "select:名字1,名字2"：按名称精确拉取
          - 其他：在延迟工具的名称与描述里做关键词搜索
        """
        deferred = [
            tool for name, tool in self._tools.items() if tool.should_defer() and name not in self._discovered
        ]
        query = query.strip()
        if query.startswith("select:"):
            wanted = {item.strip() for item in query[len("select:") :].split(",") if item.strip()}
            return [tool.spec for tool in deferred if tool.name in wanted]
        if not query:
            return [tool.spec for tool in deferred]
        lowered = query.lower()
        return [
            tool.spec
            for tool in deferred
            if lowered in tool.name.lower() or lowered in (tool.description or "").lower()
        ]


__all__ = ["ToolRegistry", "ToolNotFoundError", "ToolDisabledError", "Tool", "ToolResult"]
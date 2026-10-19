"""MCPToolWrapper：把 MCP 工具「包装」成 MewCode 内部的 Tool 接口。

经典的适配器模式：两个接口不兼容，用一个中间层做转换。
MCP Server 返回的工具定义（名称、描述、参数 Schema）与 MewCode 的
Tool 接口结构一致，唯一区别是：内部工具在 MewCode 进程内执行，
MCP 工具在一个外部进程（或 HTTP 服务）里执行。

工具名加 mcp_ 前缀和 Server 名称，避免不同 Server 提供同名工具时冲突：
  mcp_github_search_issues、mcp_jira_search_ticket ...
"""

from __future__ import annotations

import json
from typing import Any

from ..tools.base import BaseTool, ToolError, ToolResult
from .client import MCPClient, extract_text_content


class MCPToolWrapper(BaseTool):
    """把远端 MCP 工具适配为 MewCode 内部 Tool。

    名称：mcp_{server_name}_{tool_name}（如 mcp_github_search_issues）。
    description / input schema 直接透传 Server 返回的原始值。
    执行时经 MCP Client 转发到外部 Server，结果文本返回给 Agent。
    """

    safety = "command"  # MCP 工具对外部系统可见，安全分类按命令级管控

    def __init__(self, client: MCPClient, server_name: str, tool_def: dict[str, Any]) -> None:
        tool_name = str(tool_def.get("name") or "unnamed")
        self.client = client
        self.server_name = server_name
        self.tool_def = tool_def
        self.name = f"mcp_{server_name}_{tool_name}"
        self.description = str(tool_def.get("description") or "")
        self.input_schema = tool_def.get("inputSchema") or {
            "type": "object",
            "properties": {},
        }

    async def _run(self, args: dict[str, Any]) -> str:
        """调用 MCP Server 上的工具，提取文本内容返回。"""
        tool_name = str(self.tool_def.get("name") or "")
        result = await self.client.call_tool(tool_name, args)
        if result.get("isError"):
            raise ToolError(extract_text_content(result) or f"MCP 工具 {tool_name} 执行失败")
        return extract_text_content(result)

    def should_defer(self) -> bool:
        """MCP 工具数量不可控（用户加一个 Server 就可能多出几十个工具），
        默认全部延迟加载，避免每轮对话携带所有完整 schema 浪费上下文。
        """
        return True


__all__ = ["MCPToolWrapper"]
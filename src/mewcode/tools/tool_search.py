from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .base import BaseTool, ToolError

if TYPE_CHECKING:
    from .registry import ToolRegistry


class ToolSearch(BaseTool):
    """工具延迟加载的入口：模型判断需要某个延迟工具时，先调用本工具拉取完整定义。

    支持两种查询模式（对应理论篇）：
      - "select:工具名1,工具名2"：按名称精确拉取
      - 直接输入关键词：在所有延迟工具的名称与描述里搜索

    调用后匹配到的工具会被标记为「已发现」，从下一轮开始出现在正常工具列表里。
    """

    name = "ToolSearch"
    description = (
        "搜索并加载延迟工具（例如 MCP 工具）的完整定义。查询格式："
        "'select:工具名1,工具名2' 按名称精确拉取，或直接输入关键词搜索。"
        "加载后的工具下一轮起可直接调用。"
    )
    safety = "read"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，或 select: 前缀加逗号分隔的工具名列表",
            },
        },
        "required": ["query"],
    }

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def _run(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolError("缺少 query 参数")
        matches = self.registry.search_deferred(query)
        if not matches:
            return "未找到匹配的延迟工具。"
        # 标记为已发现
        for spec in matches:
            self.registry.mark_discovered(spec.name)
        payload = [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
            }
            for spec in matches
        ]
        return json.dumps(payload, ensure_ascii=False)


__all__ = ["ToolSearch"]
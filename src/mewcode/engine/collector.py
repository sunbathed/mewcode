from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from ..session import ToolUseBlock


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """解析工具入参 JSON；解析失败时以 _json_error 字段兜底，不让循环崩掉。"""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_json_error": raw[:200]}
    return parsed if isinstance(parsed, dict) else {"_json_error": raw[:200]}


@dataclass(frozen=True, slots=True)
class CollectedTurn:
    """一轮流式响应收集完成后的完整内容。"""

    text: str
    tool_uses: tuple[ToolUseBlock, ...]
    stop_reason: str | None
    usage: dict[str, Any] | None = None


class StreamCollector:
    """流式收集器：实时积攒文本碎片与工具请求碎片，攒出完整响应。

    用法：把 client.stream() 吐出的每个事件喂给 feed()，结束后取 result()。
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        # index -> {id, name, args}：同一工具的 JSON 入参按流式顺序拼接
        self._pending: dict[int, dict[str, Any]] = {}
        self.stop_reason: str | None = None
        self.usage: dict[str, Any] | None = None

    def feed(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self._text_parts.append(event.text)
        elif isinstance(event, ToolUseStart):
            slot = self._pending.setdefault(event.index, {"id": event.id, "name": event.name, "args": ""})
            if event.id:
                slot["id"] = event.id
            if event.name:
                slot["name"] = event.name
        elif isinstance(event, ToolUseDelta):
            slot = self._pending.setdefault(event.index, {"id": "", "name": "", "args": ""})
            slot["args"] += event.args_fragment
        elif isinstance(event, StreamEnd):
            if event.stop_reason:
                self.stop_reason = event.stop_reason
            if event.usage:
                self.usage = event.usage

    def result(self) -> CollectedTurn:
        uses: list[ToolUseBlock] = []
        for index in sorted(self._pending):
            slot = self._pending[index]
            uses.append(
                ToolUseBlock(
                    id=slot["id"] or f"tool_{index}",
                    name=slot["name"],
                    input=parse_tool_arguments(slot["args"]),
                )
            )
        return CollectedTurn("".join(self._text_parts), tuple(uses), self.stop_reason, self.usage)



__all__ = ["StreamCollector", "CollectedTurn", "parse_tool_arguments"]

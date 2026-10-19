from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_session_id() -> str:
    """生成会话 ID：YYYYMMDD-HHMMSS-xxxx（时间戳一眼看出创建时间，
    末尾 4 位十六进制随机后缀避免同秒冲突）。"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{secrets.token_hex(2)}"


@dataclass(slots=True)
class TextBlock:
    type: str = "text"
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(slots=True)
class ToolUseBlock:
    """模型发起的工具请求。"""

    id: str  # 请求标识（tool_call_id）
    name: str
    input: dict[str, Any]
    type: str = "tool_use"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass(slots=True)
class ToolResultBlock:
    """工具执行结果，按请求标识回传。"""

    tool_use_id: str
    content: str
    type: str = "tool_result"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": self.tool_use_id, "content": self.content}


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


def _block_from_dict(data: Any) -> ContentBlock:
    if not isinstance(data, dict):
        raise ValueError("内容块格式无效")
    kind = data.get("type")
    if kind == "text":
        return TextBlock(text=str(data.get("text", "")))
    if kind == "tool_use":
        tool_input = data.get("input")
        return ToolUseBlock(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            input=tool_input if isinstance(tool_input, dict) else {},
        )
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=str(data.get("tool_use_id", "")),
            content=str(data.get("content", "")),
        )
    raise ValueError(f"未知内容块类型：{kind}")


def _text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


@dataclass(slots=True)
class Message:
    role: str
    content: str = ""
    blocks: list[ContentBlock] | None = None  # 为空时保持纯文本消息
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content, "created_at": self.created_at}
        if self.blocks:
            data["blocks"] = [block.to_dict() for block in self.blocks]
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Message":
        if not isinstance(data, dict) or data.get("role") not in {"user", "assistant", "system", "tool"}:
            raise ValueError("消息格式无效")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("消息内容无效")
        raw_blocks = data.get("blocks")
        blocks = [_block_from_dict(item) for item in raw_blocks] if isinstance(raw_blocks, list) else None
        return cls(data["role"], content, blocks, str(data.get("created_at") or utc_now()))

    def has_tool_use(self) -> bool:
        return any(isinstance(b, ToolUseBlock) for b in self.blocks or [])

    def has_tool_result(self) -> bool:
        return any(isinstance(b, ToolResultBlock) for b in self.blocks or [])

    def to_api(self) -> list[dict[str, Any]]:
        """转换为 OpenAI 兼容协议的请求消息（可能一条消息展开为多条）。"""
        if not self.blocks:
            return [{"role": self.role, "content": self.content}]

        text_parts = [b.text for b in self.blocks if isinstance(b, TextBlock)]
        tool_uses = [b for b in self.blocks if isinstance(b, ToolUseBlock)]
        tool_results = [b for b in self.blocks if isinstance(b, ToolResultBlock)]

        if tool_results:
            return [{"role": "tool", "tool_call_id": b.tool_use_id, "content": b.content} for b in tool_results]

        messages: list[dict[str, Any]] = []
        text = "\n".join(text_parts)
        if tool_uses:
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": b.id,
                            "type": "function",
                            "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)},
                        }
                        for b in tool_uses
                    ],
                }
            )
        else:
            messages.append({"role": self.role, "content": text})
        return messages


@dataclass(slots=True)
class Session:
    id: str = field(default_factory=generate_session_id)
    model: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    messages: list[Message] = field(default_factory=list)

    def add_message(self, role: str, content: str = "", blocks: list[ContentBlock] | None = None) -> Message:
        message = Message(role, content, blocks or None)
        self.messages.append(message)
        self.updated_at = utc_now()
        return message

    def add_tool_results(self, results: list[tuple[str, str]]) -> Message:
        """把一次工具执行的全部结果作为一条用户消息追加（内部多个 tool_result 块）。"""
        message = Message("user", "", blocks=[ToolResultBlock(tool_use_id, content) for tool_use_id, content in results])
        self.messages.append(message)
        self.updated_at = utc_now()
        return message

    def add_tool_result(self, tool_use_id: str, content: str) -> Message:
        message = Message("user", "", blocks=[ToolResultBlock(tool_use_id, content)])
        self.messages.append(message)
        self.updated_at = utc_now()
        return message

    def clear(self) -> None:
        self.messages.clear()
        self.updated_at = utc_now()

    def api_messages(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in self.messages:
            result.extend(message.to_api())
        return result

    def last_tool_use(self) -> ToolUseBlock | None:
        for message in reversed(self.messages):
            for block in reversed(message.blocks or []):
                if isinstance(block, ToolUseBlock):
                    return block
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Session":
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ValueError("会话格式无效")
        raw_messages = data.get("messages", [])
        # 兼容旧格式：dict 列表或新版对象
        if isinstance(raw_messages, dict):
            raw_messages = []
        messages = [Message.from_dict(item) for item in raw_messages]
        return cls(
            id=data["id"],
            model=str(data.get("model", "")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            messages=messages,
        )
"""JSON-RPC 2.0 协议层：三种消息类型的编解码。

MCP 的消息格式统一使用 JSON-RPC 2.0，只有三种消息类型：
  - 请求（Request）：有 id、method、params。期望响应。
  - 响应（Response）：有 id（对应请求），有 result 或 error。
  - 通知（Notification）：有 method 但无 id。不需要响应。

注意 id 是可选的：通知没有 id，需要区分「没有 id」和「id 为 0」。
序列化时若 id 为空则省略该字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION = "2.0"

# 标准错误码（JSON-RPC 2.0 规范）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class MCPError(RuntimeError):
    """MCP 请求失败：Server 返回了 error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class JSONRPCRequest:
    """请求消息：富含 id、method、params。"""

    method: str
    params: dict[str, Any] | None = None
    id: int | None = None
    jsonrpc: str = JSONRPC_VERSION

    @property
    def is_notification(self) -> bool:
        return self.id is None


@dataclass(frozen=True, slots=True)
class JSONRPCResponse:
    """响应消息：有 id（对应请求），有 result 或 error。"""

    id: int | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    jsonrpc: str = JSONRPC_VERSION

    @property
    def is_error(self) -> bool:
        return self.error is not None


JSONRPCNotification = JSONRPCRequest


def encode_message(message: JSONRPCRequest | JSONRPCResponse) -> str:
    """把消息序列化为 JSON 字符串（stdio 传输的行格式）。"""
    return json.dumps(to_dict(message), ensure_ascii=False)


def to_dict(message: JSONRPCRequest | JSONRPCResponse) -> dict[str, Any]:
    """把消息转为字典，供 HTTP POST 或编码使用。"""
    if isinstance(message, JSONRPCResponse):
        base: dict[str, Any] = {"jsonrpc": message.jsonrpc, "id": message.id}
        if message.is_error:
            base["error"] = message.error
        else:
            base["result"] = message.result
        return base
    # Request / Notification
    base = {"jsonrpc": message.jsonrpc, "method": message.method}
    if message.id is not None:
        base["id"] = message.id
    if message.params is not None:
        base["params"] = message.params
    return base


def parse_message(payload: dict[str, Any]) -> JSONRPCRequest | JSONRPCResponse:
    """从字典解析一条 JSON-RPC 消息。

    判定规则：
      - 有 method 且有 id -> 请求（Request）
      - 有 method 且无 id -> 通知（Notification）
      - 无 method -> 响应（Response）
    """
    if not isinstance(payload, dict):
        raise ValueError("JSON-RPC 消息必须是对象")
    if "method" in payload:
        request = JSONRPCRequest(
            method=str(payload.get("method", "")),
            params=payload.get("params"),
            id=payload.get("id"),  # 通知时 id 为 None
            jsonrpc=str(payload.get("jsonrpc", JSONRPC_VERSION)),
        )
        return JSONRPCNotification(request.method, request.params, request.id, request.jsonrpc)
    return JSONRPCResponse(
        id=payload.get("id"),
        result=payload.get("result"),
        error=payload.get("error"),
        jsonrpc=str(payload.get("jsonrpc", JSONRPC_VERSION)),
    )


def parse_line(line: str) -> JSONRPCRequest | JSONRPCResponse:
    """从一行 JSON 解析消息（stdio 行分隔格式）。"""
    return parse_message(json.loads(line))


__all__ = [
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "MCPError",
    "JSONRPCRequest",
    "JSONRPCNotification",
    "JSONRPCResponse",
    "encode_message",
    "to_dict",
    "parse_message",
    "parse_line",
]
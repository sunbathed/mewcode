"""MCP Client：负责与某一个 MCP Server 建立连接、发送请求、接收响应。

一次完整的 MCP 会话分三个阶段（对应理论篇）：
  1. 初始化握手：initialize（声明协议版本、能力、身份）+ notifications/initialized
  2. 工具发现：tools/list 获取 Server 提供的所有工具定义
  3. 工具调用：tools/call 执行某个工具

核心机制是请求-响应的异步匹配：每个请求带 id，回包按 id 关联。
Client 发请求时创建一个 Future 放进 pending 字典；一个读取循环持续
从 transport 读消息，根据 id 分发到对应的 Future。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .jsonrpc import MCPError, encode_message, parse_message
from .transport import Transport, TransportError

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-11-25"
CLIENT_NAME = "MewCode"
CLIENT_VERSION = "0.1.0"


class MCPClientError(RuntimeError):
    """MCP Client 层错误。"""


class MCPClient:
    """面向单个 MCP Server 的客户端：握手、工具发现、工具调用、异步匹配。"""

    def __init__(self, server_name: str, transport: Transport, *, timeout: float = 60.0) -> None:
        self.server_name = server_name
        self.transport = transport
        self.timeout = timeout
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._reader_task: asyncio.Task[None] | None = None
        # 握手后由 Server 返回的信息
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.client_info = {"name": CLIENT_NAME, "version": CLIENT_VERSION}
        # 已发现的工具定义（name -> tool dict）
        self.tools: dict[str, dict[str, Any]] = {}
        self._connected = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> dict[str, Any]:
        """建立传输连接并完成初始化握手，返回 Server 的 initialize 响应。"""
        if self._connected:
            return self._server_initialize_result
        await self.transport.connect()
        self._reader_task = asyncio.create_task(self._read_loop())
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {}},
                "clientInfo": self.client_info,
            },
        )
        self._server_initialize_result = result
        self.protocol_version = result.get("protocolVersion", PROTOCOL_VERSION)
        self.capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})
        # 通知 Server：客户端已初始化完成（无 id，不需要响应）
        await self.notify("notifications/initialized", {})
        self._connected = True
        return result

    async def close(self) -> None:
        """关闭连接，取消读取循环。"""
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        # 失败等待中的请求统一报错
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPClientError("MCP Server 连接已关闭"))
        self._pending.clear()
        await self.transport.close()

    # ------------------------------------------------------------------
    # 请求 / 通知 / 响应分发
    # ------------------------------------------------------------------

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送请求并等待响应（请求-响应异步匹配）。"""
        if not self._connected and method != "initialize":
            # initialize 是第一个请求，连接尚未标记为 connected
            pass
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            await self.transport.send(message)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise MCPClientError(f"请求 {method} 超时（{self.timeout:g}s）") from exc
        except TransportError as exc:
            raise MCPClientError(str(exc)) from exc
        finally:
            self._pending.pop(request_id, None)
        if response.get("error"):
            error = response["error"]
            raise MCPError(error.get("code", -1), error.get("message", "未知错误"), error.get("data"))
        return response.get("result") or {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发送通知（没有 id，不需要响应）。"""
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        await self.transport.send(message)

    async def _read_loop(self) -> None:
        """持续读取 transport 消息，根据 id 分发到 pending 的 Future。

        通知类消息（无 id）暂不处理。读取循环退出说明 Server 连接关闭。
        """
        try:
            while True:
                message = await self.transport.receive()
                if message is None:
                    break
                parsed = parse_message(message)
                msg_id = getattr(parsed, "id", None)
                if msg_id is not None and msg_id in self._pending:
                    self._pending[msg_id].set_result(message)
                # 通知类消息暂不处理
        except TransportError:
            pass
        except Exception:  # noqa: BLE001 - 读取循环任何异常都视为连接中断
            pass
        finally:
            # 读取循环结束说明 Server 退出了：统一报错 pending 请求
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPClientError("MCP Server 连接中断"))
            self._pending.clear()

    # ------------------------------------------------------------------
    # 工具发现与调用
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """tools/list：获取 Server 提供的所有工具定义。"""
        result = await self.request("tools/list")
        raw_tools = result.get("tools", [])
        self.tools = {t.get("name"): t for t in raw_tools if isinstance(t, dict)}
        return list(self.tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """tools/call：执行 Server 上的某个工具，返回原始 result。"""
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return result

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """resources/read：读取 Server 暴露的可读数据源。"""
        result = await self.request("resources/read", {"uri": uri})
        return result

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected and self.transport.is_connected()

    def tool_names(self) -> list[str]:
        return list(self.tools)

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return f"<MCPClient {self.server_name} ({state}, {len(self.tools)} tools)>"


def _suppress(exc_type: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(exc_type)


def extract_text_content(result: dict[str, Any]) -> str:
    """从 tools/call 返回的 content 数组中提取所有 text 类型的块，拼接返回。"""
    if not result:
        return ""
    if "content" in result:
        parts: list[str] = []
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return json.dumps(result, ensure_ascii=False)


__all__ = [
    "MCPClient",
    "MCPClientError",
    "PROTOCOL_VERSION",
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "extract_text_content",
]
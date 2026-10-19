"""Transport 层：消息在 Host 与 Server 之间怎么传。

MCP 的 Data Layer（JSON-RPC 消息语义）与 Transport Layer 解耦：
transport 只负责消息的收发，上层 initialize / tools/list / tools/call
完全不关心底层是子进程管道还是 HTTP。把这个边界守好，新增 transport 就是一个文件的事。

当前支持两种官方标准传输：
  - stdio：把 MCP Server 作为子进程启动，通过 stdin/stdout 管道读写消息。
    stderr 不参与协议通信，专门输出日志和调试信息。
  - Streamable HTTP：Server 是独立 HTTP 服务，Client 用 HTTP POST 发 JSON-RPC 消息，
    响应直接返回 application/json（SSE 流式推送不在本章实现）。

stdio 消息是 UTF-8 编码的 JSON-RPC，以换行分隔；Server 的 stdout 上
不能混入任何非协议内容，否则 Client 解析失败。所以日志必须走 stderr。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .jsonrpc import parse_line


class TransportError(RuntimeError):
    """传输层错误：消息无法发送/接收。"""


class Transport(ABC):
    """传输抽象：只负责消息的收发。"""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接（stdio 启动子进程 / HTTP 准备客户端）。"""

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None:
        """发送一条 JSON-RPC 消息（字典形式）。"""

    @abstractmethod
    async def receive(self) -> dict[str, Any] | None:
        """读取一条 JSON-RPC 消息；连接关闭返回 None。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。"""

    @abstractmethod
    def is_connected(self) -> bool:
        """连接是否可用。"""


class StdioTransport(Transport):
    """stdio 传输：把 MCP Server 作为子进程启动，管道通信。

    环境变量隔离：子进程只拿到「系统必需变量 + 显式声明的 env」，
    不继承父进程的完整环境（避免泄露 API Key 等敏感信息）。
    """

    # 子进程必须保留的系统变量（跨平台最小集）
    REQUIRED_ENV_KEYS = ("PATH", "SYSTEMROOT", "USERPROFILE", "HOME", "TEMP", "TMP", "PATHEXT", "COMSPEC")

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        include_env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.workdir = workdir or os.getcwd()
        self.include_env = dict(include_env or {})
        self._process: asyncio.subprocess.Process | None = None

    def _build_env(self) -> dict[str, str]:
        """环境变量隔离：系统必需变量 + 显式声明 + 可选额外注入。"""
        merged: dict[str, str] = {}
        for key in self.REQUIRED_ENV_KEYS:
            if key in os.environ:
                merged[key] = os.environ[key]
        # Windows 上 PATH 分隔符
        merged.update(self.env)
        merged.update(self.include_env)
        return merged

    def _dump_message(self, message: dict[str, Any]) -> bytes:
        try:
            return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TransportError(f"无法序列化消息：{exc}") from exc

    async def connect(self) -> None:
        if self._process is not None:
            return
        try:
            executable, cmd_args = _resolve_executable(self.command, self.args)
            self._process = await asyncio.create_subprocess_exec(
                executable,
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
                cwd=self.workdir,
            )
        except (OSError, FileNotFoundError) as exc:
            raise TransportError(f"启动 MCP Server 子进程失败：{exc}") from exc

    async def send(self, message: dict[str, Any]) -> None:
        self._ensure_process()
        assert self._process is not None and self._process.stdin is not None
        payload = self._dump_message(message)
        try:
            self._process.stdin.write(payload)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportError("写入 MCP Server 管道失败（子进程可能已退出）") from exc

    async def receive(self) -> dict[str, Any] | None:
        self._ensure_process()
        assert self._process is not None and self._process.stdout is not None
        line = await self._process.stdout.readline()
        if not line:
            return None
        text = self._decode_line(line)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise TransportError(f"解析 Server 输出失败（stdout 混入了非协议内容？）：{exc}") from exc

    @staticmethod
    def _decode_line(line: bytes) -> str:
        """解码一行：先按 UTF-8（协议规范），失败时回退 locale 编码。

        真实的 MCP Server 应输出 UTF-8；但 Windows 上部分未显式
        reconfigure 的 Server 会按 GBK 输出，做一层兜底更健壮。
        """
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError:
            try:
                import locale

                encoding = locale.getpreferredencoding(False) or "gbk"
                return line.decode(encoding)
            except Exception:
                return line.decode("utf-8", errors="replace")

    def _ensure_process(self) -> None:
        if self._process is None or self._process.returncode is not None:
            raise TransportError("MCP Server 子进程未运行或已退出")

    def is_connected(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        # 先关 stdin 写端（EOF 让子进程自然退出），超时再强杀
        if process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001 - 关闭管道失败可忽略
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
        # 读尽 stdout/stderr 剩余内容至 EOF，让底层 pipe transport 干净回收
        # （Windows Proactor 下若不读完，transport 会在 GC 时触发 ResourceWarning）
        for pipe in (process.stdout, process.stderr):
            if pipe is None:
                continue
            try:
                while await pipe.read(4096):
                    pass
            except Exception:  # noqa: BLE001 - drain 失败不影响关闭
                pass


class HTTPTransport(Transport):
    """Streamable HTTP 传输：把 JSON-RPC 消息 POST 到 Server 固定端点。

    为了与 stdio 的「reader 循环 + id 匹配」架构统一，HTTP 采用内部队列：
    send() 发出 POST 请求，把响应（含 id 的 message）放入队列；
    receive() 从队列取数据。notification 没有 id，响应不入队（通知不需要响应）。

    Client 发请求时 Accept 头必须同时声明两种：
      Accept: application/json, text/event-stream
    Server 因此可以选择直接返回 JSON，或先用 SSE 流式推送（后者本章不消费）。
    远程 Server 通常需要认证，支持自定义 headers。
    """

    PROTOCOL_VERSION = "2025-11-25"
    MAX_RESPONSE_BYTES = 40 * 1024 * 1024  # 40MB 响应上限

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.protocol_version: str | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._client = http_client
        self._owns_client = http_client is None
        self._connected = False

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        self._connected = True

    def set_protocol_version(self, version: str) -> None:
        """handshake 结束后设置协议版本，后续请求携带 MCP-Protocol-Version 头。"""
        self.protocol_version = version

    def _request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.protocol_version is not None:
            headers["MCP-Protocol-Version"] = self.protocol_version
        headers.update(self.headers)
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        if not self._connected or self._client is None:
            raise TransportError("HTTP 传输未连接")
        try:
            response = await self._client.post(self.url, json=message, headers=self._request_headers())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TransportError(f"MCP Server 返回 HTTP {exc.response.status_code}") from exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise TransportError(f"请求 MCP Server 失败：{exc}") from exc
        # 通知（无 id）不需要响应：仅响应类消息入队
        if "id" not in message or message["id"] is None:
            return
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # SSE 流式推送：本章不做消费，读取首段文本并忽略
            # （理论篇明确：SSE 流式推送不在本章实现范围）
            return
        if not response.content:
            return
        try:
            if len(response.content) > self.MAX_RESPONSE_BYTES:
                raise TransportError("MCP Server 响应过大")
            data = json.loads(response.content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TransportError(f"解析 MCP Server 响应失败：{exc}") from exc
        await self._queue.put(data)

    async def receive(self) -> dict[str, Any] | None:
        if not self._connected:
            raise TransportError("HTTP 传输未连接")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=self.timeout * 4)
        except asyncio.TimeoutError:
            return None

    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    async def close(self) -> None:
        self._connected = False
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def encode_message_from_dict(message: dict[str, Any]) -> str:
    """把 dict 消息编码为 JSON 字符串（供 stdio 写入行）。"""
    return json.dumps(message, ensure_ascii=False)


def _resolve_executable(command: str, args: list[str]) -> tuple[str, list[str]]:
    """解析要启动的命令，兼容 Windows 上 .cmd/.exe 后缀缺失的情况。

    Windows 上 `create_subprocess_exec` 不会像 shell 那样自动尝试 .cmd 后缀：
    比如 `npx` 实际是 `npx.cmd`。这里在 Windows 平台对命令做后缀探测：
      1. 命令含路径或后缀 → 直接用；
      2. 否则尝试 command.cmd / command.exe（沿用 PATH 查找）。
    """
    if os.name != "nt":
        return command, args
    # 已含后缀或路径分隔符：交给系统解析
    if command.endswith((".exe", ".cmd", ".bat", ".com")) or any(sep in command for sep in ("/", "\\")):
        return command, args
    # 在 PATH 中查找裸命令；找不到则尝试追加 .cmd / .exe
    found = shutil.which(command)
    if found:
        return found, args
    for suffix in (".cmd", ".exe", ".bat"):
        candidate = shutil.which(command + suffix)
        if candidate:
            return candidate, args
    return command, args


__all__ = [
    "Transport",
    "TransportError",
    "StdioTransport",
    "HTTPTransport",
    "encode_message_from_dict",
]
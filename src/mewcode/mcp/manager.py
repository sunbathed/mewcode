"""MCP Manager：连接缓存、配置加载、生命周期管理。

职责（对应理论篇「完整流程：从配置到调用」）：
  - load_configs：读取配置（项目级 + 用户级合并），获取 MCP Server 列表
  - select transport：根据 Server 配置选择 stdio 或 Streamable HTTP
  - connect_all：启动时异步连接所有配置的 Server（连接缓存供复用）
  - register_all_tools：为每个 Server 调 tools/list，包装 MCPToolWrapper 注册到 ToolRegistry

关键设计：部分 Server 连接失败不阻止启动，只打警告——用户配置了 5 个
Server，其中一个因为 Token 过期连不上，不应影响其他 4 个的正常使用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..tools import ToolRegistry
from .client import MCPClient
from .config import MCPServerConfig, load_mcp_config
from .transport import HTTPTransport, StdioTransport, Transport, TransportError
from .wrapper import MCPToolWrapper

logger = logging.getLogger(__name__)


class MCPManager:
    """管理所有 MCP Server 连接：创建、缓存、注销。"""

    def __init__(
        self,
        *,
        workdir: str | None = None,
        configs: dict[str, MCPServerConfig] | None = None,
    ) -> None:
        self.workdir = workdir
        self.configs = configs if configs is not None else load_mcp_config(workdir=workdir)
        # server_name -> MCPClient（连接缓存）
        self._clients: dict[str, MCPClient] = {}
        # server_name -> 注册的工具 wrapper 列表
        self._registered: dict[str, list[MCPToolWrapper]] = {}
        # 连接失败的 server 名 -> 错误信息
        self.failures: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 工具注册
    # ------------------------------------------------------------------

    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        """连接所有 Server 并把工具注册进 ToolRegistry。

        返回连接失败的 server 名列表。失败只打警告，不抛异常。
        """
        failed: list[str] = []
        self.failures.clear()
        for name, config in self.configs.items():
            try:
                wrappers = await self._connect_and_collect(name, config)
                self._registered[name] = wrappers
                for wrapper in wrappers:
                    registry.register(wrapper)
                logger.info("MCP Server %s 已连接，注册 %d 个工具", name, len(wrappers))
            except Exception as exc:  # noqa: BLE001 - 单个 Server 失败不应阻止启动
                logger.warning("MCP Server %s 连接失败：%s", name, exc)
                self.failures[name] = str(exc)
                failed.append(name)
        return failed

    async def _connect_and_collect(
        self, name: str, config: MCPServerConfig
    ) -> list[MCPToolWrapper]:
        """连接单个 Server，发现工具并包装成 MewCode 内部工具。"""
        client = await self.connect(name, config)
        tools = await client.list_tools()
        wrappers = [MCPToolWrapper(client, name, tool) for tool in tools]
        return wrappers

    # ------------------------------------------------------------------
    # 连接缓存
    # ------------------------------------------------------------------

    async def connect(self, name: str, config: MCPServerConfig | None = None) -> MCPClient:
        """连接一个 Server 并缓存（已连接则复用）。"""
        cached = self._clients.get(name)
        if cached is not None and cached.is_connected:
            return cached
        config = config or self.configs.get(name)
        if config is None:
            raise TransportError(f"未找到 MCP Server 配置：{name}")
        transport = build_transport(config, workdir=self.workdir)
        client = MCPClient(name, transport, timeout=config.timeout)
        await client.connect()
        self._clients[name] = client
        return client

    def get_client(self, name: str) -> MCPClient | None:
        """获取已连接的客户端（连接缓存复用）。"""
        return self._clients.get(name)

    def connected_servers(self) -> list[str]:
        return [name for name, client in self._clients.items() if client.is_connected]

    def registered_count(self) -> int:
        return sum(len(wrappers) for wrappers in self._registered.values())

    def registered_tools(self) -> list[MCPToolWrapper]:
        return [w for wrappers in self._registered.values() for w in wrappers]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """关闭所有客户端连接。"""
        clients = list(self._clients.values())
        self._clients.clear()
        self._registered.clear()
        for client in clients:
            with _suppress(Exception):
                await client.close()


def build_transport(config: MCPServerConfig, *, workdir: str | None = None) -> Transport:
    """根据配置选择 transport：有 command -> stdio；有 url -> Streamable HTTP。"""
    if config.transport_type == "http":
        return HTTPTransport(
            config.url or "",
            headers=config.expanded_headers(),
            timeout=config.timeout,
        )
    return StdioTransport(
        config.command or "",
        config.args,
        env=config.expanded_env(),
        workdir=workdir,
    )


def _suppress(exc_type: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(exc_type)


__all__ = ["MCPManager", "build_transport"]
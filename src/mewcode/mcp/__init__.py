"""第七章：MCP 协议与开放工具生态。

MCP（Model Context Protocol）是一个开放协议，定义 AI 应用如何和外部能力
进行标准化通信。MewCode 是 Host，内部创建多个 MCP Client，每个 Client
对应一个 Server。

模块结构：
  - jsonrpc：JSON-RPC 2.0 三种消息类型的编解码
  - transport：Transport 抽象 + stdio（子进程管道）+ Streamable HTTP
  - client：MCPClient（握手 / 工具发现 / 工具调用 / 请求-响应异步匹配）
  - config：MCP Server 配置加载与合并（项目级优先 + env 展开）
  - wrapper：MCPToolWrapper 适配器（把 MCP 工具包装成内部 Tool 接口）
  - manager：MCPManager（连接缓存、生命周期、注册工具到 ToolRegistry）
"""

from .client import MCPClient, MCPClientError, extract_text_content
from .config import MCPServerConfig, MCPConfigError, expand_env_vars, load_mcp_config
from .jsonrpc import (
    MCPError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    parse_line,
    parse_message,
)
from .manager import MCPManager
from .transport import HTTPTransport, StdioTransport, Transport, TransportError
from .wrapper import MCPToolWrapper

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPError",
    "MCPManager",
    "MCPServerConfig",
    "MCPConfigError",
    "MCPToolWrapper",
    "JSONRPCRequest",
    "JSONRPCNotification",
    "JSONRPCResponse",
    "HTTPTransport",
    "StdioTransport",
    "Transport",
    "TransportError",
    "expand_env_vars",
    "extract_text_content",
    "load_mcp_config",
    "parse_line",
    "parse_message",
]
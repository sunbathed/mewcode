from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# 工具安全分类：只读 / 写入 / 执行命令
SAFETY_READ = "read"
SAFETY_WRITE = "write"
SAFETY_COMMAND = "command"


class ToolTimeoutError(TimeoutError):
    """Raised by a tool that detects its own timeout (e.g. after killing a process)."""


class ToolError(RuntimeError):
    """工具执行失败：BaseTool 会转换为失败的 ToolResult。"""


@dataclass(slots=True)
class ToolResult:
    """统一工具执行结果，供 UI 展示并回传给模型。"""

    is_error: bool = False
    output: str = ""
    error: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return not self.is_error

    def to_text(self) -> str:
        """回传给模型的结果文本。"""
        if self.is_error:
            reason = "超时" if self.timed_out else "执行失败"
            return f"错误（{reason}，耗时 {self.duration:g} 秒）：{self.error}"
        return self.output

    def __str__(self) -> str:
        return self.to_text()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具元信息：名称、描述、入参结构与安全分类。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    safety: str = SAFETY_READ


class Tool(ABC):
    """所有工具的公共契约：元信息 + 执行方法。"""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    safety: str = SAFETY_READ

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema, self.safety)

    def should_defer(self) -> bool:
        """是否为延迟工具：完整 schema 默认不进模型工具列表，需经 ToolSearch 加载。

        内置工具数量少、使用频率高，没必要延迟；MCP 工具数量不可控，
        MCPToolWrapper 固定返回 True（默认策略：内置常驻、MCP 延迟）。
        """
        return False

    @abstractmethod
    async def _run(self, args: dict[str, Any]) -> str:
        """子类实现具体逻辑，返回输出文本。"""

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> ToolResult:
        """执行工具并返回统一结果。"""

    def to_api_format(self) -> dict[str, Any]:
        """转换为模型可识别的 OpenAI 兼容工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class BaseTool(Tool):
    """通用实现：统一处理执行超时与异常转换。"""

    timeout: float = 60.0

    async def run(self, args: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        result = ToolResult()
        try:
            output = await asyncio.wait_for(self._run(args), timeout=self.timeout)
        except asyncio.TimeoutError:
            result.is_error = True
            result.timed_out = True
            result.error = f"{self.name} 执行超时（超过 {self.timeout:g} 秒）"
        except ToolTimeoutError as exc:
            result.is_error = True
            result.timed_out = True
            result.error = str(exc)
        except ToolError as exc:
            result.is_error = True
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 所有异常统一转换为失败结果
            result.is_error = True
            result.error = str(exc) or f"{self.name} 执行失败"
        else:
            result.output = output if isinstance(output, str) else str(output)
        result.duration = round(time.monotonic() - start, 2)
        return result
from __future__ import annotations

import json

import pytest

from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.tools.base import BaseTool

# Windows ProactorEventLoop 下子进程 pipe transport 在 loop 关闭后的 GC
# 会触发已知的 PytestUnraisableExceptionWarning（_ProactorBasePipeTransport.__del__）。
# 这是 CPython asyncio 的平台级已知噪音（bpo-37318 等），与应用代码无关，统一忽略。
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning",
)


class DelayRead(BaseTool):
    """只读工具：支持延迟参数，用于并发时序测试。"""

    name = "DelayRead"
    description = "带延迟的只读工具"
    safety = "read"

    async def _run(self, args):
        import asyncio

        await asyncio.sleep(args.get("delay", 0.01))
        return f"read:{args.get('tag', '')}"


class DelayWrite(BaseTool):
    """写类工具：支持延迟参数，用于串行时序测试。"""

    name = "DelayWrite"
    description = "带延迟的写工具"
    safety = "write"

    async def _run(self, args):
        import asyncio

        await asyncio.sleep(args.get("delay", 0.01))
        return f"wrote:{args.get('tag', '')}"


class ScriptedClient:
    """按脚本回放 LLM 流式响应的假客户端。

    script 是一个列表，每项对应一轮请求：
      - 事件列表：按顺序 yield
      - Exception 实例：直接抛出
    脚本用完后（超出轮次），自动输出收尾文本，让 Agent 自然 END_TURN。
    """

    def __init__(self, script=None):
        self.script = script or []
        self.requests: list[list[dict]] = []
        self.tools = None
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.script and self.calls <= len(self.script):
            entry = self.script[self.calls - 1]
        else:
            entry = [TextDelta("（脚本结束，收尾）"), StreamEnd("stop")]
        if isinstance(entry, Exception):
            raise entry
        for event in entry:
            yield event


def text_turn(*parts: str, stop: str = "stop") -> list:
    """构造一轮纯文本响应。"""
    events: list = [TextDelta(part) for part in parts]
    events.append(StreamEnd(stop))
    return events


def tools_turn(tools: list[tuple[str, str, dict]], *text_parts: str, stop: str = "tool_calls") -> list:
    """构造一轮含工具请求的响应。

    tools 每项为 (id, name, args)。args 会以 JSON 分片形式流出。
    """
    events: list = [TextDelta(part) for part in text_parts]
    for index, (tool_id, name, args) in enumerate(tools):
        events.append(ToolUseStart(index=index, id=tool_id, name=name))
        raw = json.dumps(args, ensure_ascii=False)
        # 模拟流式分片：按 2 字符切
        for start in range(0, len(raw), 2):
            events.append(ToolUseDelta(index=index, args_fragment=raw[start : start + 2]))
    events.append(StreamEnd(stop))
    return events


def make_registry(tmp_path, extra=True):
    """构造测试用注册中心：真实六个工具 + 可选的两个延迟测试工具。"""
    from mewcode.tools import ToolRegistry

    registry = ToolRegistry(workdir=str(tmp_path))
    if extra:
        registry.register(DelayRead())
        registry.register(DelayWrite())
    return registry


@pytest.fixture
def tmp_registry(tmp_path):
    return make_registry(tmp_path)


__all__ = [
    "DelayRead",
    "DelayWrite",
    "ScriptedClient",
    "text_turn",
    "tools_turn",
    "make_registry",
    "tmp_registry",
]

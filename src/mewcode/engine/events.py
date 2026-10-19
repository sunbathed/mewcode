from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# 停止条件：Agent Loop 的 while 循环只有这些出口（对应第四章理论篇）。
#   END_TURN          模型主动结束（无工具调用）
#   MAX_ITERATIONS    达到最大轮数上限（安全网）
#   USER_CANCELLED    用户取消（Esc / cancel 信号）
#   TIMEOUT           整个循环超时
#   TOOL_ERROR_LIMIT  连续多轮工具失败，判定模型迷失
#   INTERNAL_ERROR    LLM 请求失败等内部错误
# ---------------------------------------------------------------------------


class AgentStopReason(enum.StrEnum):
    END_TURN = "end_turn"
    MAX_ITERATIONS = "max_iterations"
    USER_CANCELLED = "user_cancelled"
    TIMEOUT = "timeout"
    TOOL_ERROR_LIMIT = "tool_error_limit"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class TextChunkEvent:
    """模型流式输出的文字增量（stream_text）。"""

    kind: ClassVar[str] = "stream_text"
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseEvent:
    """模型请求调用某个工具（tool_use）。"""

    kind: ClassVar[str] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    """某个工具执行完成（tool_result）。"""

    kind: ClassVar[str] = "tool_result"
    tool_use_id: str
    output: str
    is_error: bool
    error: str = ""
    duration: float = 0.0
    persisted: bool = False
    persisted_path: str = ""


@dataclass(frozen=True, slots=True)
class TurnCompleteEvent:
    """一轮 LLM 调用完成（turn_complete）。"""

    kind: ClassVar[str] = "turn_complete"
    turn: int
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LoopCompleteEvent:
    """整个 Agent Loop 结束（loop_complete），携带停止原因与总轮数。"""

    kind: ClassVar[str] = "loop_complete"
    total_turns: int
    stop_reason: AgentStopReason


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """Token 用量更新：本轮的估算值/真实值与累计值（usage），支持 Prompt Cache 统计。"""

    kind: ClassVar[str] = "usage"
    turn: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cached_tokens: int = 0
    total_cached_tokens: int = 0



@dataclass(frozen=True, slots=True)
class ContextCompactedEvent:
    """上下文被压缩（context_compacted）：第 2 层全量摘要或第 1 层聚合替换发生。

    reason 取 auto（自动触发）/ manual（用户 /compact）/ force（撞墙后的
    紧急压缩）。携带压缩前后的 Token 估算值，供 UI 展示「压缩了多少」。
    """

    kind: ClassVar[str] = "context_compacted"
    reason: str
    before_tokens: int
    after_tokens: int
    summary_preview: str = ""


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """循环过程中发生错误（error）。"""

    kind: ClassVar[str] = "error"
    message: str


@dataclass(frozen=True, slots=True)
class PermissionRequestEvent:
    """第 5 层 HITL：Agent 需要用户授权工具调用（permission_request）。

    Agent Loop 发出该事件后阻塞等待 `response_future`，
    UI 层渲染确认对话框，用户回复后 resolve future 唤醒 Agent。
    response_future 是 asyncio.Future[PermissionResponse]。
    """

    kind: ClassVar[str] = "permission_request"
    request_id: str
    tool_name: str
    description: str
    response_future: Any = None


AgentEvent = (
    TextChunkEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnCompleteEvent
    | LoopCompleteEvent
    | UsageEvent
    | ErrorEvent
    | PermissionRequestEvent
    | ContextCompactedEvent
)

__all__ = [
    "AgentStopReason",
    "AgentEvent",
    "TextChunkEvent",
    "ToolUseEvent",
    "ToolResultEvent",
    "TurnCompleteEvent",
    "LoopCompleteEvent",
    "UsageEvent",
    "ErrorEvent",
    "PermissionRequestEvent",
    "ContextCompactedEvent",
]

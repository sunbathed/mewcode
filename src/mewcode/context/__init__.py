"""第 8 章：上下文管理（记忆层）。

两层压缩策略（能轻则轻）：
  - 第 1 层 大结果存磁盘（budget）：几乎零损失零成本，挡单次超大工具输出
  - 第 2 层 全量摘要 Auto-Compact（compact）：昂贵但强力，兜底长对话

对外主要入口是 ContextManager：每次 API 请求前按顺序执行两层，
压缩结果写回 Session 外层对话历史。
"""

from .budget import (
    ContentReplacementState,
    ToolResultBudget,
    build_preview,
    persist_result,
    persisted_output_dir,
)
from .compact import (
    BOUNDARY_MESSAGE,
    SUMMARY_PROMPT,
    is_prompt_too_long_error,
    parse_summary,
)
from .manager import (
    CompactError,
    CompactOutcome,
    ContextConfig,
    ContextManager,
    DEFAULT_CONTEXT_WINDOW,
)
from .tokens import (
    estimate_messages_tokens,
    estimate_payload_tokens,
    estimate_tokens,
    estimate_tools_tokens,
)

__all__ = [
    "CompactError",
    "CompactOutcome",
    "ContentReplacementState",
    "ContextConfig",
    "ContextManager",
    "DEFAULT_CONTEXT_WINDOW",
    "ToolResultBudget",
    "BOUNDARY_MESSAGE",
    "SUMMARY_PROMPT",
    "build_preview",
    "estimate_messages_tokens",
    "estimate_payload_tokens",
    "estimate_tokens",
    "estimate_tools_tokens",
    "is_prompt_too_long_error",
    "parse_summary",
    "persist_result",
    "persisted_output_dir",
]
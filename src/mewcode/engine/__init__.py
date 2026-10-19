"""第四章：Agent Loop 引擎。

ReAct 范式（Think → Act → Observe）的 while 循环，
对外暴露 AgentEvent 事件流，与 UI 完全解耦。
"""

from .agent import Agent, AgentConfig, _default_system_prompt, _estimate_tokens
from .collector import StreamCollector, parse_tool_arguments
from .events import (
    AgentEvent,
    AgentStopReason,
    ContextCompactedEvent,
    ErrorEvent,
    LoopCompleteEvent,
    PermissionRequestEvent,
    TextChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from .builder import (
    BuildOptions,
    EnvironmentContext,
    PromptBuilder,
    Section,
    build_system_prompt,
    detect_environment,
)
from .partition import Batch, execute_batches, partition_tool_calls, run_batch_concurrently, run_batch_serially
from .plan_mode import (
    PLAN_DIR_NAME,
    PLAN_FILE_SUFFIX,
    PLAN_MODE_EXIT_REMINDER,
    PLAN_MODE_FULL_REMINDER,
    PLAN_MODE_INSTRUCTIONS,
    PLAN_MODE_SPARSE_REMINDER,
    PLAN_MODE_TOOLS,
    PLAN_WRITE_TOOL,
    PlanModeState,
    build_plan_mode_reminder,
    build_plan_mode_tools,
    ensure_plan_dir,
    intercept_write,
    list_plan_files,
    read_plan_file,
    save_plan_file,
)
from .prompt_pipeline import assemble_api_payload, system_reminder_message

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentEvent",
    "AgentStopReason",
    "Batch",
    "BuildOptions",
    "ContextCompactedEvent",
    "EnvironmentContext",
    "ErrorEvent",
    "LoopCompleteEvent",
    "PermissionRequestEvent",
    "PLAN_MODE_EXIT_REMINDER",
    "PLAN_MODE_FULL_REMINDER",
    "PLAN_MODE_SPARSE_REMINDER",
    "PlanModeState",
    "PromptBuilder",
    "Section",
    "StreamCollector",
    "TextChunkEvent",
    "ToolResultEvent",
    "ToolUseEvent",
    "TurnCompleteEvent",
    "UsageEvent",
    "_default_system_prompt",
    "_estimate_tokens",
    "assemble_api_payload",
    "build_plan_mode_reminder",
    "build_plan_mode_tools",
    "build_system_prompt",
    "detect_environment",
    "ensure_plan_dir",
    "execute_batches",
    "intercept_write",
    "list_plan_files",
    "parse_tool_arguments",
    "partition_tool_calls",
    "read_plan_file",
    "run_batch_concurrently",
    "run_batch_serially",
    "save_plan_file",
    "system_reminder_message",
]
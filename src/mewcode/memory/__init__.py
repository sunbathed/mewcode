"""第 9 章：记忆层（跨会话记忆与会话持久化）。

三个模块各管一层长期记忆：
  - instructions：项目指令文件（MEWCODE.md 多层优先级 + @include 模块化）
  - session：JSONL 会话持久化（存储、恢复、链校验、过期清理）
  - auto_memory：自动记忆（LLM 提取长期知识，用户级/项目级分拣）
"""

from .auto_memory import MemoryManager, MemoryManagerError, MEMORY_EXTRACTION_PROMPT
from .instructions import MAX_INCLUDE_DEPTH, load_instructions, process_includes
from .session import (
    RECORD_TYPES,
    RecordType,
    SessionManager,
    SessionMeta,
    SessionRecord,
    build_time_gap_message,
    generate_session_id,
    records_to_messages,
    validate_message_chain,
)

__all__ = [
    "MAX_INCLUDE_DEPTH",
    "MEMORY_EXTRACTION_PROMPT",
    "RECORD_TYPES",
    "RecordType",
    "SessionManager",
    "SessionMeta",
    "SessionRecord",
    "build_time_gap_message",
    "generate_session_id",
    "load_instructions",
    "process_includes",
    "records_to_messages",
    "validate_message_chain",
    "MemoryManager",
    "MemoryManagerError",
]
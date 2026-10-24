"""第 13 章：Fork 路径——继承父 Agent 对话历史的子 Agent。

Fork 是 SubAgent 的第二种创建模式：调用 Agent 工具时不指定
subagent_type 就走 Fork。与定义式（空白对话）的根本区别是：
Fork 子 Agent 继承父 Agent 的完整对话历史，第一次 API 调用几乎
100% 命中 prompt cache，不需要重新处理之前的对话内容。

build_forked_messages 做三件事：
  1. 防嵌套检测：历史里已有 <fork_boilerplate> 标记直接拒绝（Fork 不能 Fork）
  2. 补全未完成的工具调用：父 Agent 可能正在执行工具调用，消息末尾是
     assistant 发的请求但没有结果；用 "interrupted" 占位符补全，否则
     LLM API 会因为工具调用缺结果而报错
  3. 在末尾追加子 Agent 的任务指令作为 user 消息

Fork Boilerplate 是注入到历史首部的硬指令，约束 Fork 子 Agent 的
行为：不能再 Fork、不要对话提问、直接干活、报告简短。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .types import ForkError

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..session import Message, Session, ToolResultBlock, ToolUseBlock

logger = logging.getLogger(__name__)

FORK_BOILERPLATE_TAG = "<fork_boilerplate>"

FORK_BOILERPLATE = f"""{FORK_BOILERPLATE_TAG}
你是一个 Fork 出来的工作进程。你不是主 Agent。
规则（不可协商）：
1. 不能再 Fork。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在你被分配的任务范围内。
5. 最终报告控制在 500 字以内，以「Scope:」开头。
</fork_boilerplate>
"""


def _has_fork_marker(message: Any) -> bool:
    """检查一条消息里是否含 Fork Boilerplate 标记。"""
    content = getattr(message, "content", "")
    if FORK_BOILERPLATE_TAG in str(content):
        return True
    blocks = getattr(message, "blocks", None)
    if blocks:
        for block in blocks:
            text = getattr(block, "text", "")
            if FORK_BOILERPLATE_TAG in str(text):
                return True
    return False


def build_forked_messages(
    parent_session: Session,
    task: str,
    task_message: str | None = None,
) -> Session:
    """从父 Agent 的会话构建 Fork 子 Agent 的会话。

    返回一个新 Session，包含：
      - Fork Boilerplate 指令（钉在首部 user 消息）
      - 父 Agent 的完整对话历史（含 "interrupted" 占位符补全未完成调用）
      - 末尾追加任务指令 user 消息
    """
    # 防嵌套：历史里任何一条消息含 Fork 标记都拒绝
    for message in parent_session.messages:
        if _has_fork_marker(message):
            raise ForkError(
                "Cannot fork from a forked agent. Fork nesting is not allowed."
            )

    from ..session import Session, TextBlock, ToolResultBlock, ToolUseBlock

    fork_conv = Session(model=parent_session.model)
    # 1. Fork Boilerplate 钉在首部（覆盖父 Agent 的「交互式助手」默认行为）
    fork_conv.add_message("user", FORK_BOILERPLATE)

    # 2. 复制父对话历史
    for message in parent_session.messages:
        fork_conv.messages.append(message)

    # 3. 补全最后一条 assistant 消息里未完成的 tool_use 占位结果
    existing_result_ids: set[str] = set()
    for message in fork_conv.messages:
        for block in message.blocks or []:
            block_type = getattr(block, "type", "")
            if block_type == "tool_result":
                existing_result_ids.add(getattr(block, "tool_use_id", ""))

    if fork_conv.messages:
        last = fork_conv.messages[-1]
        if getattr(last, "role", "") == "assistant":
            pending = [
                block
                for block in last.blocks or []
                if getattr(block, "type", "") == "tool_use"
                and getattr(block, "id", "") not in existing_result_ids
            ]
            if pending:
                placeholders = [
                    ToolResultBlock(tool_use_id=block.id, content="interrupted")
                    for block in pending
                ]
                fork_conv.add_message("user", "", blocks=placeholders)

    # 4. 追加任务指令
    message_text = task_message if task_message is not None else task
    fork_conv.add_message("user", message_text)
    return fork_conv


__all__ = [
    "FORK_BOILERPLATE",
    "FORK_BOILERPLATE_TAG",
    "build_forked_messages",
]
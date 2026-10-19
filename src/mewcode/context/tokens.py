"""第 8 章：Token 近似估算。

本章不做精确 tokenizer（理论篇明确 Out of Scope）。中英文混合的近似规则：
  - 中文等非 ASCII 字符：1 字符 ≈ 1 token
  - ASCII 字符：4 字符 ≈ 1 token

误差对「自动压缩阈值判断」与「压缩前后 Token 统计」足够，真实用量以 API
返回的 usage 为准（Agent Loop 里已有该逻辑）。
"""

from __future__ import annotations

from typing import Any, Iterable


def estimate_tokens(text: str | None) -> int:
    """粗略估算一段文本的 token 数。"""
    if not text:
        return 0
    ascii_count = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_count
    return other + (ascii_count // 4) + (1 if ascii_count % 4 else 0)


def estimate_messages_tokens(messages: Iterable[Any]) -> int:
    """估算一组 API 消息（dict 列表）的 token 数。

    role / content / tool_call_id / tool_calls 全部计入，覆盖
    固定开销里的消息结构与内容本身。
    """
    total = 0
    for message in messages or []:
        if isinstance(message, dict):
            total += estimate_tokens(str(message.get("role", "")))
            content = message.get("content")
            if content is not None:
                total += estimate_tokens(str(content))
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                total += estimate_tokens(str(tool_call_id))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                total += estimate_tokens(str(tool_calls))
        else:
            total += estimate_tokens(str(message))
    return total


def estimate_tools_tokens(tools: Iterable[Any]) -> int:
    """估算工具描述列表的 token 数（固定开销中的工具定义部分）。"""
    total = 0
    for tool in tools or []:
        total += estimate_tokens(str(tool))
    return total


def estimate_payload_tokens(system_prompt: str, messages: Iterable[Any], tools: Iterable[Any]) -> int:
    """估算一次完整 API 请求的 token 数：system prompt + 消息 + 工具定义。"""
    return estimate_tokens(system_prompt) + estimate_messages_tokens(messages) + estimate_tools_tokens(tools)


__all__ = [
    "estimate_tokens",
    "estimate_messages_tokens",
    "estimate_tools_tokens",
    "estimate_payload_tokens",
]

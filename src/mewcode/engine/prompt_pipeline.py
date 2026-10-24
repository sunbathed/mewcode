from __future__ import annotations

from typing import Any


def system_reminder_message(content: str) -> dict[str, Any]:
    """生成带有 <system-reminder> 标签的系统级提醒消息。

    以 role="user" 注入对话通道，模型会视其为系统补充指令而非用户交谈输入。
    """
    clean_content = content.strip()
    return {
        "role": "user",
        "content": f"<system-reminder>\n{clean_content}\n</system-reminder>",
    }


def hook_notification_message(content: str) -> dict[str, Any]:
    """生成 Hook prompt 执行器注入的 <hook-notification> 标签消息。

    与 generic 的 <system-reminder> 区分开：Hook 注入的提示词可以被
    模型识别为「运行期钩子的动态指令」，不污染对话历史的结构，
    也让 prompt 来源（Skill / Hook / Plan Mode）在请求里可追踪。
    """
    clean_content = content.strip()
    return {
        "role": "user",
        "content": f"<hook-notification>\n{clean_content}\n</hook-notification>",
    }


def assemble_api_payload(
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    enable_cache: bool = True,
) -> dict[str, Any]:
    """将 system 指令、messages 对话历史与 tools 工具描述分发组装成 API 请求体。

    静态稳定的 system prompt 位于首部，若开启 cache，则附加 cache_control 标记以利用 Prompt Cache。
    动态变化的信息（如 system-reminder）随 messages 传递，不破坏 system 缓存。
    """
    system_msg: dict[str, Any] = {
        "role": "system",
        "content": system_prompt,
    }
    if enable_cache:
        # 兼容 Anthropic / OpenAI 扩展格式的 cache_control 标记
        system_msg["cache_control"] = {"type": "ephemeral"}

    api_messages = [system_msg] + list(messages)

    payload: dict[str, Any] = {
        "system": system_prompt,
        "messages": api_messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


__all__ = [
    "hook_notification_message",
    "system_reminder_message",
    "assemble_api_payload",
]

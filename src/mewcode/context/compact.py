"""第 8 章第 2 层：全量摘要（Auto-Compact）的 Prompt 与工具函数。

这一层是上下文快爆时的终极兜底：调用 LLM 自己生成结构化摘要替换旧消息，
能把十几万 token 的对话压到几千 token。代价是一次额外 API 调用且必然有
信息损失，所以只在前缀预防（第 1 层）挡不住时才用。

关键设计（理论篇）：
- 摘要 Prompt 明确禁止调用工具，首尾各强调一次；MewCode 走「摘要请求
  不传 tools 参数」的彻底路线，模型根本看不到工具定义。
- 两阶段生成：先输出 <analysis> 草稿块梳理思路，再输出正式 <summary>
  块；只保留 summary，analysis 用完即弃。
- 摘要必须按 9 个固定部分组织，其中「用户消息原文保留」优先级最高。
- 压缩阈值 = 上下文窗口 - 预留摘要输出（20K）- 安全余量（自动 13K /
  手动 3K），固定值而非百分比。
"""

from __future__ import annotations

from typing import Any

# 摘要输出预留：复杂会话的 9 段式摘要约 15K-18K token，预留 20K 防截断
SUMMARY_OUTPUT_RESERVE = 20_000
# 自动压缩安全余量：约等于一个大源文件的 token 量，防两轮检查之间的单轮波动
AUTO_SAFETY_MARGIN = 13_000
# 手动压缩安全余量：用户主动触发，知道自己在做什么，3K 足够
MANUAL_SAFETY_MARGIN = 3_000
# 连续失败熔断上限：3 次后停止自动触发，避免死循环
MAX_COMPACT_FAILURES = 3
# Prompt Too Long 重试：最多重试 3 次，还不行就丢掉 20% 的消息组再试
COMPACT_MAX_RETRIES = 3

# 压缩后的边界消息：防止模型根据摘要「脑补」出不存在的代码细节
BOUNDARY_MESSAGE = (
    "上面是之前对话的摘要。如果需要文件的具体内容，请用 ReadFile 重新读取，"
    "不要根据摘要猜测代码细节。"
)

SUMMARY_PROMPT = """你是 MewCode 的上下文压缩器。下面是一段 Agent 与用户的完整对话历史。
请你生成一份结构化摘要，尽可能保留所有对继续工作有用的信息。

生成时必须遵守：
1. 严禁调用任何工具，只输出纯文本。
2. 先输出一个 <analysis> 草稿块：梳理这段对话里发生了什么、哪些信息重要。
   草稿不会保留，只是你的思考过程，请把最重要的信息都在这里过一遍。
3. 再基于草稿输出正式的 <summary> 摘要块。摘要必须包含以下 9 个部分（用编号小标题）：
   1. 主要请求和意图：用户到底想做什么
   2. 关键技术概念：讨论过的重要技术点
   3. 文件和代码段：涉及哪些文件，关键代码片段要尽量保留原文
   4. 错误和修复：遇到了什么错，怎么解决的
   5. 问题解决过程：解决问题的思路和方法
   6. 所有用户消息：用户说过的所有非工具结果的话，尽量原文保留，不要改写
   7. 待办任务：还没完成的事
   8. 当前工作：最近在做什么（要最详细）
   9. 可能的下一步：接下来打算做什么

关键要求：
- 第 6 部分最重要：用户的原话是意图、偏好和语气的载体，不要用自己的话转述。
  空间不够时，优先保证用户原话，其次再压缩其他部分。
- 只输出 <analysis> 与 <summary> 两个块，不要输出其他内容。

提醒：不要调用任何工具，工具调用会被拒绝，你会失败。只输出纯文本。"""


def auto_compact_threshold(window: int, *, reserve: int = SUMMARY_OUTPUT_RESERVE, margin: int = AUTO_SAFETY_MARGIN) -> int:
    """自动压缩触发阈值：窗口 - 摘要预留 - 安全余量。"""
    return max(0, window - reserve - margin)


def manual_compact_threshold(window: int, *, reserve: int = SUMMARY_OUTPUT_RESERVE, margin: int = MANUAL_SAFETY_MARGIN) -> int:
    """手动压缩的安全余量小得多：用户主动触发，知道自己要做什么。"""
    return max(0, window - reserve - margin)


def is_prompt_too_long_error(exc: BaseException) -> bool:
    """判断异常是否为上下文超限（Prompt Too Long / context length exceeded）。

    正常对话请求撞墙（如估算偏差）时需要触发 ForceCompact 自救。
    """
    message = str(exc).lower()
    markers = (
        "prompt_too_long",
        "prompt is too long",
        "too many tokens",
        "context length",
        "maximum context",
        "context window",
    )
    return any(marker in message for marker in markers)


def parse_summary(raw: str) -> str:
    """从摘要输出中提取 <summary> 块；找不到时退回全文（防解析失败白费一次调用）。"""
    if not raw:
        return raw
    start = raw.find("<summary>")
    end = raw.rfind("</summary>")
    if start == -1 or end == -1 or end <= start:
        # 清理 <analysis> 残留：无 summary 块时至少去掉分析草稿
        clean = raw
        a_start = clean.find("<analysis>")
        a_end = clean.rfind("</analysis>")
        if a_start != -1 and a_end != -1 and a_end > a_start:
            clean = clean[:a_start] + clean[a_end + len("</analysis>") :]
        return clean.strip()
    return raw[start + len("<summary>") : end].strip()


def group_by_round(api_messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把 API 消息按「用户轮次」分组，供 PTL 重试时丢弃最旧的消息组。

    每一组从一条非工具结果的 user 消息开始，直到下一条 user 消息前为止
    （含中间的 assistant / tool 消息）。
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in api_messages or []:
        role = message.get("role")
        is_user_round_start = role == "user" and not message.get("tool_call_id")
        if is_user_round_start and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


__all__ = [
    "SUMMARY_OUTPUT_RESERVE",
    "AUTO_SAFETY_MARGIN",
    "MANUAL_SAFETY_MARGIN",
    "MAX_COMPACT_FAILURES",
    "COMPACT_MAX_RETRIES",
    "BOUNDARY_MESSAGE",
    "SUMMARY_PROMPT",
    "auto_compact_threshold",
    "manual_compact_threshold",
    "is_prompt_too_long_error",
    "parse_summary",
    "group_by_round",
]
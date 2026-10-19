"""/review：审查当前 git diff 中的代码变更。

这是唯一一个 PROMPT 类型命令：命令系统构造一段精心设计的审查 prompt
发给 Agent，实际干活的还是 LLM。带参数时可指定额外关注点。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType

REVIEW_PROMPT = (
    "请审查当前 git diff 中的代码变更。"
    "重点关注：\n"
    "1. 逻辑错误\n"
    "2. 安全问题\n"
    "3. 性能问题\n"
    "4. 代码风格"
)


async def handle_review(ctx: CommandContext) -> None:
    prompt = REVIEW_PROMPT
    if ctx.args:
        prompt += f"\n\n额外关注：{ctx.args}"
    ctx.ui.send_user_message(prompt)


REVIEW_COMMAND = Command(
    name="review",
    description="审查 git diff 代码变更",
    type=CommandType.PROMPT,
    handler=handle_review,
    usage="/review [额外关注点]",
    arg_prompt="用法：/review [额外关注点]",
)

__all__ = ["REVIEW_COMMAND", "REVIEW_PROMPT"]
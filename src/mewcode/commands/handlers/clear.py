"""/clear：开启新对话（LOCAL_UI）。

当前会话被关闭并保存到会话历史，然后创建一个全新会话。
因为会重置整个界面状态，归为 LOCAL_UI 类型。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_clear(ctx: CommandContext) -> None:
    new_session = ctx.config.get("new_session_worker")
    if new_session is None:
        ctx.ui.add_system_message("新建会话功能不可用。")
        return
    await new_session(ctx)


CLEAR_COMMAND = Command(
    name="clear",
    description="清除对话，创建新会话",
    type=CommandType.LOCAL_UI,
    handler=handle_clear,
    usage="/clear",
    arg_prompt="用法：/clear",
)

__all__ = ["CLEAR_COMMAND"]
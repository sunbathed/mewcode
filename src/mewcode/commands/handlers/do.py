"""/do：切换回执行模式（与 /plan 是一对）。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_do(ctx: CommandContext) -> None:
    ctx.ui.set_plan_mode(False)
    ctx.ui.add_system_message("已退出规划模式，恢复正常工具。")


DO_COMMAND = Command(
    name="do",
    description="切换到执行模式",
    type=CommandType.LOCAL_UI,
    handler=handle_do,
    usage="/do",
    arg_prompt="用法：/do",
)

__all__ = ["DO_COMMAND"]
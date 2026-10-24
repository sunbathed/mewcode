"""/plan：切换到 Plan 只读模式（LOCAL_UI）。

不带参数单纯切换模式；带参数则切换模式的同时把参数作为任务描述
发送给 Agent（/plan 设计用户认证模块 一步到位）。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_plan(ctx: CommandContext) -> None:
    ctx.ui.set_plan_mode(True)
    ctx.ui.add_system_message("已进入规划模式（只读探索，禁止写入和命令执行）。")
    if ctx.args:
        ctx.ui.send_user_message(ctx.args)


PLAN_COMMAND = Command(
    name="plan",
    aliases=["p"],
    description="切换到 Plan 模式",
    type=CommandType.LOCAL_UI,
    handler=handle_plan,
    usage="/plan [任务描述]",
)

__all__ = ["PLAN_COMMAND"]
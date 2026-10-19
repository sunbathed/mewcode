"""第 10 章：命令执行上下文与分发入口。

把「解析 → 查找 → 分发」串起来：命令系统的核心分派逻辑，
供 App / TUI 在用户按下回车后优先于 Agent 调用执行。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .parser import parse_command
from .registry import CommandContext, CommandRegistry


def build_command_context(
    registry: CommandRegistry,
    args: str,
    *,
    session: Any = None,
    session_manager: Any = None,
    memory_manager: Any = None,
    ui: Any = None,
    config: dict[str, Any] | None = None,
) -> CommandContext:
    """构造 handler 运行时上下文：把命令执行所需资源打包。"""
    return CommandContext(
        args=args,
        session=session,
        session_manager=session_manager,
        memory_manager=memory_manager,
        ui=ui,
        config=config or {},
    )


async def dispatch_command(
    ctx: CommandContext,
    name: str,
    registry: CommandRegistry,
) -> bool:
    """按命令名查找并执行 handler；返回 False 表示命令未找到。

    未知命令统一引导到 /help（MewCode 的错误提示永远带引导）。
    """
    command = registry.find(name)
    if command is None:
        if ctx.ui is not None:
            ctx.ui.add_system_message(f"未知命令：/{name}，输入 /help 查看可用命令")
        return False
    result = command.handler(ctx)
    if asyncio.iscoroutine(result):
        await result
    return True


__all__ = ["build_command_context", "dispatch_command"]
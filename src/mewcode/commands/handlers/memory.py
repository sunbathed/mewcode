"""/memory：记忆管理（show / list / add / clear / path）。

不带参数显示记忆概要；list 列出完整记忆；add <类别> <内容> 手动添加；
clear 清空全部记忆（带确认提示）；path 显示记忆目录。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_memory(ctx: CommandContext) -> None:
    parts = ctx.args.split(None, 1) if ctx.args else []
    sub = parts[0] if parts else ""
    memory = ctx.memory_manager
    if memory is None:
        ctx.ui.add_system_message("记忆管理不可用。")
        return

    if sub == "" or sub == "show":
        view = memory.view()
        ctx.ui.add_system_message(view if view else "暂无记忆。")
    elif sub == "list":
        index = memory.load_index()
        ctx.ui.add_system_message(index if index else "暂无记忆。")
    elif sub == "add":
        if len(parts) < 2:
            ctx.ui.add_system_message("用法：/memory add <内容>")
            return
        try:
            written = memory.remember(parts[1])
            ctx.ui.add_system_message(f"已记住：{parts[1]}" if written else "记忆写入失败。")
        except Exception as exc:  # noqa: BLE001 - 记忆写入失败提示
            ctx.ui.add_system_message(f"记忆写入失败：{exc}")
    elif sub == "clear":
        confirm = ctx.config.get("confirm_worker")
        if confirm is not None and not confirm("确定清空全部记忆？此操作不可恢复。输入 y 确认："):
            ctx.ui.add_system_message("已取消清空。")
            return
        removed = memory.clear()
        ctx.ui.add_system_message(f"已清空 {removed} 条记忆。")
    elif sub == "path":
        ctx.ui.add_system_message(f"用户级：{memory.user_dir}")
        ctx.ui.add_system_message(f"项目级：{memory.project_dir}")
    else:
        ctx.ui.add_system_message("未知子命令：/memory show|list|add|clear|path")


MEMORY_COMMAND = Command(
    name="memory",
    description="记忆管理（show/list/add/clear/path）",
    type=CommandType.LOCAL,
    handler=handle_memory,
    usage="/memory [show|list|add <内容>|clear|path]",
    arg_prompt="用法：/memory [show|list|add <内容>|clear|path]",
)

__all__ = ["MEMORY_COMMAND"]
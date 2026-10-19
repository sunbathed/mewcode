"""/session：会话管理（list / resume / new / delete）。

不带参数显示当前会话概要；list 列出历史会话；
resume <id> 恢复会话（注入时间间隔提示）；new 新建会话；delete <id> 删除。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_session(ctx: CommandContext) -> None:
    parts = ctx.args.split(None, 1) if ctx.args else []
    sub = parts[0] if parts else ""
    manager = ctx.session_manager
    if manager is None:
        ctx.ui.add_system_message("会话管理不可用。")
        return

    if sub == "":
        session = ctx.session
        if session is None:
            ctx.ui.add_system_message("暂无当前会话。")
            return
        ctx.ui.add_system_message(
            f"当前会话：{session.id}（消息数 {len(getattr(session, 'messages', []))}）"
        )
    elif sub == "list":
        metas = manager.list_metas() if hasattr(manager, "list_metas") else []
        if not metas:
            ctx.ui.add_system_message("暂无历史会话。")
            return
        lines = ["历史会话："]
        for meta in metas:
            title = f"（{meta.title[:24]}）" if getattr(meta, "title", "") else ""
            lines.append(f"  {meta.id}  {meta.last_active.isoformat()}{title}")
        ctx.ui.add_system_message("\n".join(lines))
    elif sub == "resume":
        if len(parts) < 2:
            ctx.ui.add_system_message("用法：/session resume <会话ID>")
            return
        resume_worker = ctx.config.get("resume_worker")
        if resume_worker is None:
            ctx.ui.add_system_message("恢复会话功能不可用。")
            return
        await resume_worker(parts[1])
    elif sub == "new":
        new_worker = ctx.config.get("new_session_worker")
        if new_worker is None:
            ctx.ui.add_system_message("新建会话功能不可用。")
            return
        await new_worker(ctx)
    elif sub == "delete":
        if len(parts) < 2:
            ctx.ui.add_system_message("用法：/session delete <会话ID>")
            return
        session_id = parts[1]
        if ctx.session is not None and session_id == ctx.session.id:
            ctx.ui.add_system_message("不能删除当前会话。")
            return
        removed = manager.delete(session_id) if hasattr(manager, "delete") else False
        ctx.ui.add_system_message("已删除会话。" if removed else "会话不存在。")
    else:
        ctx.ui.add_system_message("未知子命令：/session list|resume|new|delete")


SESSION_COMMAND = Command(
    name="session",
    description="会话管理（list/resume/new/delete）",
    type=CommandType.LOCAL,
    handler=handle_session,
    usage="/session [list|resume <id>|new|delete <id>]",
    arg_prompt="用法：/session [list|resume <id>|new|delete <id>]",
)

__all__ = ["SESSION_COMMAND"]
"""/permission：权限管理（mode / rules / add / reset）。

不带参数显示当前权限模式和规则数量；mode <模式> 切换；
rules 显示当前生效规则；add <规则> <效果> 添加本地规则；reset 清空本地规则。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_permission(ctx: CommandContext) -> None:
    parts = ctx.args.split(None, 1) if ctx.args else []
    sub = parts[0] if parts else ""
    permission = ctx.config.get("permission")
    if permission is None:
        ctx.ui.add_system_message("权限管理不可用。")
        return

    if sub == "":
        rules = permission.rules if hasattr(permission, "rules") else None
        local_n = len(rules.rules.local) if rules else 0
        project_n = len(rules.rules.project) if rules else 0
        user_n = len(rules.rules.user) if rules else 0
        ctx.ui.add_system_message(
            f"当前权限模式：{permission.mode.value}"
            f"（本地 {local_n} 条 / 项目 {project_n} 条 / 用户 {user_n} 条规则）"
        )
    elif sub == "mode":
        mode_worker = ctx.config.get("mode_worker")
        if mode_worker is None:
            ctx.ui.add_system_message("切换权限模式不可用。")
            return
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            ctx.ui.add_system_message("用法：/permission mode <模式>")
            return
        await mode_worker(args)
    elif sub == "rules":
        rules_text = ctx.config.get("rules_worker")
        if rules_text is not None:
            ctx.ui.add_system_message(rules_text())
        else:
            ctx.ui.add_system_message("暂无规则展示。")
    elif sub == "add":
        add_worker = ctx.config.get("add_rule_worker")
        if add_worker is None or len(parts) < 2:
            ctx.ui.add_system_message("用法：/permission add <规则> <效果>")
            return
        await add_worker(parts[1])
    elif sub == "reset":
        reset_worker = ctx.config.get("reset_worker")
        if reset_worker is None:
            ctx.ui.add_system_message("重置权限规则不可用。")
            return
        await reset_worker()
    else:
        ctx.ui.add_system_message("未知子命令：/permission mode|rules|add|reset")


PERMISSION_COMMAND = Command(
    name="permission",
    aliases=["perm", "permissions"],
    description="权限管理（mode/rules/add/reset）",
    type=CommandType.LOCAL,
    handler=handle_permission,
    usage="/permission [mode|rules|add|reset]",
)

__all__ = ["PERMISSION_COMMAND"]
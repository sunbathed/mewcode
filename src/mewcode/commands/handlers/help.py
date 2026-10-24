"""/help：显示帮助信息。

不带参数时列出所有可用命令的名称、别名和简要说明；
带参数时显示某个命令的详细用法（/help <命令名>）。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


def _format_aliases(cmd: Command) -> str:
    names = [f"/{cmd.name}"]
    names.extend(f"/{alias}" for alias in cmd.aliases)
    return ", ".join(names)


async def handle_help(ctx: CommandContext) -> None:
    registry = ctx.config.get("registry")
    if registry is None:
        ctx.ui.add_system_message("命令注册中心不可用。")
        return
    if ctx.args:
        name = ctx.args.strip().lower().lstrip("/")
        cmd = registry.find(name)
        if cmd is None:
            ctx.ui.add_system_message(f"未知命令：{ctx.args}")
            return
        lines = [f"/{cmd.name}  {cmd.description}"]
        if cmd.aliases:
            lines.append(f"别名：{', '.join('/' + a for a in cmd.aliases)}")
        if cmd.usage:
            lines.append(f"用法：{cmd.usage}")
        ctx.ui.add_system_message("\n".join(lines))
        return
    lines = ["可用命令："]
    for cmd in registry.list_commands():
        lines.append(f"  {_format_aliases(cmd):<28} {cmd.description}")
    lines.append("")
    lines.append("输入 /help <命令名> 查看详细用法。")
    ctx.ui.add_system_message("\n".join(lines))


HELP_COMMAND = Command(
    name="help",
    aliases=["h", "?"],
    description="显示帮助信息",
    type=CommandType.LOCAL,
    handler=handle_help,
    usage="/help [命令名]",
)

__all__ = ["HELP_COMMAND"]

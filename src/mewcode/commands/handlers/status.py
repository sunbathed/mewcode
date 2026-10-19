"""/status：显示当前综合状态信息。

调试和确认配置时特别有用：模式、Token 用量、工具数、记忆数、工作目录、版本。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_status(ctx: CommandContext) -> None:
    lines = ["MewCode 状态", "─────────────"]
    # 模式：从 config 里拿（App/TUI 注入）
    mode = ctx.config.get("mode", "default")
    lines.append(f"模式：{mode}")
    # Token：通过 UIController 拿（当前/窗口上限）
    try:
        used, limit = ctx.ui.get_token_count()
        percent = f"{used / limit * 100:.0f}%" if limit else "-"
        lines.append(f"Token：{used:,} / {limit:,}（{percent}）")
    except (TypeError, ValueError):
        lines.append("Token：-")
    # 工具：从 config 拿注册中心
    registry = ctx.config.get("registry")
    if registry is not None:
        lines.append(f"工具：{len(registry.tools_count())} 个已启用" if hasattr(registry, "tools_count") else "工具：-")
    # 记忆：从 memory_manager 拿
    memory = ctx.memory_manager
    if memory is not None:
        try:
            user_n = len(memory.list_memory_files(memory.user_dir))
            project_n = len(memory.list_memory_files(memory.project_dir))
            lines.append(f"记忆：user {user_n} 条，project {project_n} 条")
        except Exception:
            lines.append("记忆：-")
    # 工作目录
    workdir = ctx.config.get("workdir")
    if workdir:
        lines.append(f"工作目录：{workdir}")
    # 版本
    version = ctx.config.get("version")
    if version:
        lines.append(f"版本：{version}")
    ctx.ui.add_system_message("\n".join(lines))


STATUS_COMMAND = Command(
    name="status",
    aliases=["s"],
    description="显示状态信息（模式/Token/工具/记忆/目录）",
    type=CommandType.LOCAL,
    handler=handle_status,
    usage="/status",
    arg_prompt="用法：/status",
)

__all__ = ["STATUS_COMMAND"]

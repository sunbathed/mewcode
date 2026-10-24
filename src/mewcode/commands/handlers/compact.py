"""/compact：手动触发上下文压缩。

不带参数执行标准压缩；带参数可指定保留重点（追加到压缩请求）。
token 不足 5000 时压缩没有意义，直接提示无需压缩。
压缩完成后显示前后的 token 对比。
"""

from __future__ import annotations

from ..registry import Command, CommandContext, CommandType


async def handle_compact(ctx: CommandContext) -> None:
    worker = ctx.config.get("compact_worker")
    if worker is None:
        ctx.ui.add_system_message("压缩功能不可用（未注入 compact worker）。")
        return
    await worker(ctx.args)


COMPACT_COMMAND = Command(
    name="compact",
    aliases=["c"],
    description="压缩上下文",
    type=CommandType.LOCAL,
    handler=handle_compact,
    usage="/compact [保留重点]",
)

__all__ = ["COMPACT_COMMAND"]
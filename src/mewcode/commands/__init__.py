"""第 10 章：命令包。

Slash Command 内置命令框架：注册、解析、分发、补全。
所有以 / 开头的输入都走这里本地处理，绕过 Agent Loop，不消耗 token。

- parser.py     命令解析：/ 前缀、命令名 + 参数拆分
- registry.py   注册中心：Command 定义、双表查找、别名冲突检查
- context.py    执行上下文与分发入口
- handlers/     十个内置命令：help/status/compact/clear/plan/do/session/memory/permission/review
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import build_command_context, dispatch_command
from .handlers import ALL_COMMANDS, register_all_commands
from .parser import parse_command as parse_command_parts
from .registry import (
    Command,
    CommandContext,
    CommandHandler,
    CommandRegistry,
    CommandType,
    UIController,
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """兼容旧 API 的解析结果：name / argument / is_command。"""

    name: str
    argument: str = ""
    is_command: bool = True


def parse_command(text: str) -> ParsedCommand | None:
    """解析用户输入（兼容旧 API）。

    以 / 开头的输入返回 ParsedCommand（name / argument），否则返回 None。
    底层复用框架的 parse_command_parts（返回三元组）。
    """
    name, args, is_command = parse_command_parts(text)
    if not is_command:
        return None
    return ParsedCommand(name=name, argument=args)


def build_default_registry() -> CommandRegistry:
    """创建并注册全部内置命令的注册中心。"""
    registry = CommandRegistry()
    register_all_commands(registry)
    return registry


# 兼容旧 API：已知命令名集合（框架核心命令 + 历史遗留命令）。
# App/TUI 的命令分发用它判断命令是否合法。
LEGACY_COMMANDS = {"new", "sessions", "use", "exit", "init", "mcp", "resume"}

KNOWN_COMMANDS: set[str] = (
    {cmd.name for cmd in ALL_COMMANDS}
    | {alias for cmd in ALL_COMMANDS for alias in cmd.aliases}
    | LEGACY_COMMANDS
)


def command_usage(command: ParsedCommand | None) -> str:
    """命令用法提示（兼容旧 API）。

    - 未知命令：引导到 /help
    - 需要参数的命令：显示用法
    - 无需参数的命令：提示不需要参数
    """
    if command is None or not command.name:
        return "未知命令。可用命令：/help 查看完整列表。"
    name = command.name
    usage_by_name = {
        "use": "用法：/use <会话ID>",
        "permission": "用法：/permission [mode|rules|add|reset]",
        "session": "用法：/session [list|resume <id>|new|delete <id>]",
        "memory": "用法：/memory [show|list|add <内容>|clear|path]",
        "compact": "用法：/compact [保留重点]",
        "help": "用法：/help [命令名]",
        "review": "用法：/review [额外关注点]",
        "plan": "用法：/plan [任务描述]",
        "status": "用法：/status",
        "clear": "/clear 不需要参数",
        "do": "/do 不需要参数",
        "new": "/new 不需要参数",
        "sessions": "/sessions 不需要参数",
        "init": "/init 不需要参数",
        "mcp": "/mcp 不需要参数",
        "exit": "/exit 不需要参数",
        "resume": "用法：/resume 展示会话列表并选择恢复",
    }
    if name in usage_by_name:
        return usage_by_name[name]
    return f"未知命令：/{name}。可用命令：/help 查看完整列表。"


__all__ = [
    "ALL_COMMANDS",
    "Command",
    "CommandContext",
    "CommandHandler",
    "CommandRegistry",
    "CommandType",
    "KNOWN_COMMANDS",
    "ParsedCommand",
    "UIController",
    "build_command_context",
    "build_default_registry",
    "command_usage",
    "dispatch_command",
    "parse_command",
    "parse_command_parts",
    "register_all_commands",
]
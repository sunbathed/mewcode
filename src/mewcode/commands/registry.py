"""第 10 章：命令注册中心。

集中管理命令的定义（名称、别名、描述、用法、类型、处理函数），
提供注册、查找、列举与补全。注册时做双向冲突检查，防止
/h 既是 /help 的别名又是另一个命令的主名字的混乱局面。

后面 Skill 系统会运行时动态注册新命令，所以提供带锁的
register() 异步版本（当前内置命令都在启动时用 register_sync 注册）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from .parser import parse_command

CommandHandler = Callable[["CommandContext"], Awaitable[None] | None]


class CommandType(str, Enum):
    """命令的执行模式。

    LOCAL：纯本地逻辑，执行完以系统消息展示结果（/help、/status）
    LOCAL_UI：本地执行但会改变 UI 的行为状态（/clear、/plan、/do）
    PROMPT：把一段预设 prompt 发给 Agent，由 LLM 实际干活（/review）
    """

    LOCAL = "local"
    LOCAL_UI = "local_ui"
    PROMPT = "prompt"


@dataclass(frozen=True, slots=True)
class CommandContext:
    """传给 handler 的运行时上下文：命令执行所需的一切资源打包在一起。"""

    args: str
    session: Any = None
    session_manager: Any = None
    memory_manager: Any = None
    ui: Any = None
    config: dict[str, Any] = field(default_factory=dict)


class UIController:
    """UI 层暴露给命令的操作接口。

    命令不需要知道 UI 底层用的是什么框架、怎么渲染，只需要知道
    自己能显示一条系统消息、能发一条消息给 Agent 就够了。
    """

    def add_system_message(self, text: str) -> None: ...
    def send_user_message(self, text: str) -> None: ...
    def set_plan_mode(self, enabled: bool) -> None: ...
    def get_token_count(self) -> tuple[int, int]: ...
    def refresh_status(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Command:
    """一条命令的完整定义（命令的身份证）。"""

    name: str
    description: str
    type: CommandType
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    arg_prompt: str = ""
    hidden: bool = False


class CommandRegistry:
    """命令注册中心：主命令表 + 别名表双表查找。"""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._alias_map: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_sync(self, command: Command) -> None:
        """同步注册：检查命令名与所有别名的双向冲突，冲突直接抛错。"""
        if command.name in self._commands or command.name in self._alias_map:
            raise ValueError(f"Command name '{command.name}' conflicts")
        for alias in command.aliases:
            if alias in self._alias_map or alias in self._commands:
                raise ValueError(f"Alias '{alias}' conflicts")
        self._commands[command.name] = command
        for alias in command.aliases:
            self._alias_map[alias] = command.name

    async def register(self, command: Command) -> None:
        """异步注册（为 Skill 系统运行时动态注册做准备）。"""
        async with self._lock:
            self.register_sync(command)

    # ------------------------------------------------------------------
    # 查找 / 列举
    # ------------------------------------------------------------------

    def find(self, name: str) -> Command | None:
        """先按命令名精确匹配，再按别名匹配。"""
        if name in self._commands:
            return self._commands[name]
        canon = self._alias_map.get(name)
        if canon:
            return self._commands.get(canon)
        return None

    def list_commands(self) -> list[Command]:
        """按注册顺序返回所有非隐藏命令。"""
        return [cmd for cmd in self._commands.values() if not cmd.hidden]

    def all_commands(self) -> list[Command]:
        return list(self._commands.values())

    # ------------------------------------------------------------------
    # 补全
    # ------------------------------------------------------------------

    def complete(self, prefix: str) -> list[str]:
        """Tab 补全：匹配命令名或别名前缀，隐藏命令不参与。"""
        prefix = prefix.lstrip("/")
        matches: list[str] = []
        for cmd in self.list_commands():
            if cmd.name.startswith(prefix):
                matches.append("/" + cmd.name)
            for alias in cmd.aliases:
                if alias.startswith(prefix):
                    matches.append("/" + alias)
        matches.sort()
        return matches


__all__ = [
    "Command",
    "CommandContext",
    "CommandHandler",
    "CommandRegistry",
    "CommandType",
    "UIController",
    "parse_command",
]

"""内置命令聚合注册：把十个核心命令批量注册进注册中心。"""

from __future__ import annotations

from ..registry import CommandRegistry
from .clear import CLEAR_COMMAND
from .compact import COMPACT_COMMAND
from .do import DO_COMMAND
from .help import HELP_COMMAND
from .memory import MEMORY_COMMAND
from .permission import PERMISSION_COMMAND
from .plan import PLAN_COMMAND
from .review import REVIEW_COMMAND
from .session import SESSION_COMMAND
from .status import STATUS_COMMAND

ALL_COMMANDS = [
    HELP_COMMAND,
    STATUS_COMMAND,
    COMPACT_COMMAND,
    CLEAR_COMMAND,
    PLAN_COMMAND,
    DO_COMMAND,
    SESSION_COMMAND,
    MEMORY_COMMAND,
    PERMISSION_COMMAND,
    REVIEW_COMMAND,
]


def register_all_commands(registry: CommandRegistry) -> None:
    """批量注册所有内置命令；别名冲突会在注册时抛 ValueError。"""
    for command in ALL_COMMANDS:
        registry.register_sync(command)


__all__ = ["ALL_COMMANDS", "register_all_commands"]
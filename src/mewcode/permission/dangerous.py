"""第一道防线：危险命令黑名单（仅对 Bash 工具生效）。

黑名单的实现就是一组正则表达式。每当 Bash 工具要执行一条命令时，
先拿命令字符串跟所有模式匹配一遍，匹配上了就直接拒绝，返回原因。
黑名单优先级最高，任何配置、任何权限模式都无法覆盖。

黑名单要做的就一件事：拦住最明显、后果最严重的操作（不可逆的系统损毁）。
剩下的交给后面几层防线处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# 默认危险命令模式：(正则模式, 拦截原因)
# 说明：
# - rm\s+-(([a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*)\s+/\s*$ 匹配 rm -rf /、rm -fr /、rm -rfv / 等
# - (ba)?sh 同时匹配 bash 和 sh
# - of=/dev/ 锁定写入目标是裸设备
DEFAULT_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+-(([a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*)\s+/\s*$", "递归强制删除根目录"),
    (r"mkfs\.", "格式化磁盘"),
    (r"dd\s+if=.*of=/dev/", "直接写磁盘设备"),
    (r"chmod\s+-R\s+777\s+/", "递归修改根目录权限"),
    (r":\(\)\s*\{.*:\|.*:&.*\};:", "fork bomb 进程炸弹"),
    (r"curl\s+.*\|\s*(ba)?sh", "管道执行远程脚本"),
    (r"wget\s+.*\|\s*(ba)?sh", "管道执行远程脚本"),
    (r">\s*/dev/sd", "覆盖磁盘设备"),
)


@dataclass(frozen=True, slots=True)
class DangerousPattern:
    """一条黑名单规则：编译后的正则 + 拦截原因。"""

    regex: re.Pattern[str]
    reason: str


class DangerousCommandDetector:
    """危险命令检测器：对命令字符串做黑名单正则匹配。

    匹配命中即返回拦截原因；未命中返回 None（交由下一层防线决策）。
    """

    def __init__(self, patterns: Iterable[tuple[str, str]] | None = None) -> None:
        source = list(patterns) if patterns is not None else list(DEFAULT_DANGEROUS_PATTERNS)
        self._compiled = [DangerousPattern(re.compile(pattern), reason) for pattern, reason in source]

    def match(self, command: str) -> str | None:
        """对命令字符串做黑名单正则匹配，命中返回拦截原因。"""
        if not command:
            return None
        for entry in self._compiled:
            if entry.regex.search(command):
                return entry.reason
        return None

    def is_dangerous(self, command: str) -> bool:
        return self.match(command) is not None


__all__ = ["DangerousCommandDetector", "DangerousPattern", "DEFAULT_DANGEROUS_PATTERNS"]
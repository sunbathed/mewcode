"""第 14 章：Slug 安全校验——防 LLM 输入触发路径遍历。

Worktree 的名称会被用作文件系统目录名和 Git 分支名，而 LLM 生成的
输入不可信：不做校验，../../etc/passwd 这类名称一旦拼进路径，
worktree 就可能被创建到系统目录下。这是经典的路径遍历攻击。

校验规则：
  - 允许大小写字母、数字、点号、连字符、下划线
  - 允许 / 作为嵌套 slug 的分隔符（team-refactor/alice），按 / 分段逐段校验
  - . 和 .. 作为独立段必须显式拒绝（正则会放行它们，不拦等于放行遍历）
  - 总长度不超过 64
"""

from __future__ import annotations

import re

MAX_SLUG_LENGTH = 64

_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_slug(name: str) -> str | None:
    """校验 slug 合法性。返回 None 表示通过，返回字符串表示错误原因。

    返回错误字符串而不是抛异常，调用方可以把原因直接展示给用户/LLM。
    """
    if not name:
        return "name cannot be empty"
    if len(name) > MAX_SLUG_LENGTH:
        return f"name too long (max {MAX_SLUG_LENGTH} characters)"
    segments = name.split("/")
    for seg in segments:
        if not seg:
            return "name contains empty segment"
        if seg in (".", ".."):
            return "name must not contain '.' or '..' as a segment"
        if not _SEGMENT_RE.match(seg):
            return (
                f"invalid segment: {seg!r} "
                "(only letters, digits, '.', '-', '_' are allowed)"
            )
    return None


def flatten_slug(name: str) -> str:
    """嵌套 slug 转平铺目录/分支名：/ 替换为 +（feature/login → feature+login）。

    斜杠不能出现在目录名里；分支名里替换成 + 还能避免 Git 的
    目录/文件同名冲突（D/F conflict，refs/heads/a 和 refs/heads/a/b 不能共存）。
    """
    return name.replace("/", "+")


__all__ = ["MAX_SLUG_LENGTH", "flatten_slug", "validate_slug"]

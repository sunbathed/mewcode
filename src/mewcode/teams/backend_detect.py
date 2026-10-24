"""第 15 章：执行后端自动检测。

队员在哪里运行？按优先级走：
  1. 用户指定 in-process，或非交互模式（测试 / CI）→ in-process
  2. 已在 tmux 会话里 → tmux
  3. 在 iTerm2 里且 it2 CLI 可用 → iterm2
  4. 本地装了 tmux 二进制 → tmux（拉外部 session）
  5. 都不满足 → 抛 BackendDetectionError，不静默退化

检测只看环境变量和 PATH，在 spawn 开始之前一次性决定；没有
「Pane spawn 失败再切 in-process」的运行时回退分支。调用方若希望
在检测失败时兜底（如 Windows 桌面环境），用 detect_backend_or_fallback
显式表达这个意图。
"""

from __future__ import annotations

import os
import shutil

from .models import BackendType


class BackendDetectionError(RuntimeError):
    """没有任何可用的终端后端。"""


def _in_tmux_session() -> bool:
    return bool(os.environ.get("TMUX"))


def _in_iterm2() -> bool:
    return os.environ.get("TERM_PROGRAM") == "iTerm.app"


def _it2_available() -> bool:
    return shutil.which("it2") is not None


def _tmux_installed() -> bool:
    return shutil.which("tmux") is not None


def detect_backend(teammate_mode: str = "", is_interactive: bool = True) -> BackendType:
    """按环境优先级选择执行后端；不可用时抛异常而不是静默退化。"""
    mode = (teammate_mode or "").strip().lower()
    if mode in ("in-process", "in_process", "inprocess"):
        return BackendType.IN_PROCESS
    if mode == "tmux":
        if not _tmux_installed():
            raise BackendDetectionError("teammate_mode='tmux' but tmux is not installed")
        return BackendType.TMUX
    if mode in ("iterm2", "iterm"):
        if not (_in_iterm2() and _it2_available()):
            raise BackendDetectionError("teammate_mode='iterm2' requires iTerm2 with it2 CLI")
        return BackendType.ITERM2
    if not is_interactive:
        return BackendType.IN_PROCESS
    if _in_tmux_session():
        return BackendType.TMUX
    if _in_iterm2() and _it2_available():
        return BackendType.ITERM2
    if _tmux_installed():
        return BackendType.TMUX
    raise BackendDetectionError(
        "No suitable terminal backend found (tmux / iTerm2+it2). "
        "Use teammate_mode='in-process' to run teammates in this process."
    )


def detect_backend_or_fallback(teammate_mode: str = "", is_interactive: bool = True) -> tuple[BackendType, str]:
    """detect_backend 的产品路径包装：检测失败兜底 in-process 并说明原因。

    返回 (后端, 提示文本)；提示文本非空表示发生了显式兜底（不是静默的）。
    """
    try:
        backend = detect_backend(teammate_mode, is_interactive)
        return backend, ""
    except BackendDetectionError as exc:
        return BackendType.IN_PROCESS, f"（无可用终端后端，已回退 in-process：{exc}）"


__all__ = [
    "BackendDetectionError",
    "detect_backend",
    "detect_backend_or_fallback",
]

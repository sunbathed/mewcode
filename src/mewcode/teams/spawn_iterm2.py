"""第 15 章：iTerm2 后端——it2 CLI 方案。

macOS 用户在 iTerm2 中运行且安装了 it2 CLI 工具时，用 iTerm2 原生
split pane 作为队员后端。隔离特性与 Tmux 一致：独立进程、完整工具集、
生命周期不依赖 Lead；区别只在底层的终端多路复用实现。

复用 tmux 后端的 build_cli_command 构建命令，然后用 it2 split-pane
创建新窗格。用 it2 CLI 而不是 AppleScript 操控 iTerm2，代码更简洁。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .models import BackendType
from .spawn_tmux import build_cli_command


class ITermSpawnError(RuntimeError):
    """it2 命令执行失败。"""


@dataclass(slots=True)
class ITermPaneInfo:
    """一个 iTerm2 队员的窗格信息。"""

    session_id: str
    window_name: str
    backend: BackendType = BackendType.ITERM2


def _run_it2(args: list[str], timeout: int = 10) -> str:
    """执行 it2 子命令，返回 stdout；失败抛 ITermSpawnError。"""
    try:
        result = subprocess.run(
            ["it2", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ITermSpawnError(f"it2 {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise ITermSpawnError(f"it2 {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def spawn_iterm2_teammate(
    team_name: str,
    teammate_name: str,
    worktree_path: str,
    prompt: str,
    agent_type: str = "",
    model: str = "",
) -> ITermPaneInfo:
    """在 iTerm2 split pane 里启动一个队员 CLI 实例。"""
    from .spawn_tmux import shell_quote

    window_name = f"{team_name}-{teammate_name}"
    cli_cmd = build_cli_command(
        team_name=team_name,
        teammate_name=teammate_name,
        worktree_path=worktree_path,
        prompt=prompt,
        agent_type=agent_type,
        model=model,
    )
    zsh_command = "/bin/zsh -c " + shell_quote(cli_cmd)
    session_id = _run_it2(["split-pane", "--command", zsh_command])
    return ITermPaneInfo(session_id=session_id, window_name=window_name)


def wake_iterm2_pane(session_id: str) -> bool:
    """iTerm2 没有跨进程 send-keys 的等价物；消息靠队员自己的 mailbox 轮询。"""
    return False


def kill_iterm2_pane(session_id: str) -> bool:
    """it2 CLI 不提供关闭会话的能力；窗格由队员进程结束后自行退出。"""
    return False


__all__ = [
    "ITermPaneInfo",
    "ITermSpawnError",
    "kill_iterm2_pane",
    "spawn_iterm2_teammate",
    "wake_iterm2_pane",
]

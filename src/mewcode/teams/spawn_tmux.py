"""第 15 章：Tmux 后端——split-window 方案。

每个队员在独立的 tmux pane 中运行一个完整的 MewCode CLI 实例，拥有
自己的进程、内存空间和配置，和 Lead 完全隔离。队员之间通过 mailbox
文件通信；Lead 发消息时额外 send-keys 唤醒目标 pane。

三级降级策略保证无论当前 tmux 状态如何，都能找到地方放新队友：
  1. 在已有的 tmux session 里 split window
  2. 失败 → 新建 window 再 split
  3. 再失败 → 新建整个 session

窗格创建后统一用 send-keys 把 CLI 命令敲进去。CLI 命令通过环境变量
传递团队信息（MEWCODE_TEAM_NAME / MEWCODE_TEAMMATE_NAME）；prompt
里的单引号要转义，防止破坏 pane 里 shell 的解析。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .models import BackendType


class TmuxSpawnError(RuntimeError):
    """tmux 命令执行失败。"""


@dataclass(slots=True)
class TmuxPaneInfo:
    """一个 tmux 队员的窗格信息。"""

    pane_id: str
    window_name: str
    backend: BackendType = BackendType.TMUX


def _run_tmux(args: list[str], timeout: int = 10) -> str:
    """执行 tmux 子命令，返回 stdout；失败抛 TmuxSpawnError。"""
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TmuxSpawnError(f"tmux {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise TmuxSpawnError(f"tmux {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def shell_quote(value: str) -> str:
    """单引号包裹并转义内容里的单引号（POSIX shell 安全）。"""
    return "'" + value.replace("'", "'\\''") + "'"


def build_cli_command(
    team_name: str,
    teammate_name: str,
    worktree_path: str,
    prompt: str,
    agent_type: str = "",
    model: str = "",
) -> str:
    """构建 pane 里执行的完整 CLI 命令。

    形如：MEWCODE_TEAM_NAME=x MEWCODE_TEAMMATE_NAME=y mewcode --teammate
    --work-dir /path 'prompt'。团队信息走环境变量，工作目录与提示词走
    CLI 参数；prompt 里的单引号转义为 '\\''，防止提前结束引号。
    """
    parts = ["mewcode", "--teammate", "--work-dir", worktree_path]
    if agent_type:
        parts.extend(["--agent-type", agent_type])
    if model:
        parts.extend(["--model", model])
    env_parts = [
        f"MEWCODE_TEAM_NAME={team_name}",
        f"MEWCODE_TEAMMATE_NAME={teammate_name}",
    ]
    return f"{' '.join(env_parts)} {' '.join(parts)} {shell_quote(prompt)}"


def spawn_tmux_teammate(
    team_name: str,
    teammate_name: str,
    worktree_path: str,
    prompt: str,
    agent_type: str = "",
    model: str = "",
) -> TmuxPaneInfo:
    """在 tmux pane 里启动一个队员 CLI 实例（三级降级）。"""
    window_name = f"{team_name}-{teammate_name}"
    cli_cmd = build_cli_command(
        team_name=team_name,
        teammate_name=teammate_name,
        worktree_path=worktree_path,
        prompt=prompt,
        agent_type=agent_type,
        model=model,
    )
    try:
        # 一级：在当前 session 里水平分屏；-P -F "#{pane_id}" 让 tmux
        # 创建窗格后打印其 ID，后续 send-keys 与关闭都靠它
        pane_id = _run_tmux(["split-window", "-h", "-P", "-F", "#{pane_id}", "-t", f"{team_name}:"]).strip()
    except TmuxSpawnError:
        try:
            # 二级：新建 window 再 split
            _run_tmux(["new-window", "-t", team_name, "-n", window_name])
            pane_id = _run_tmux(
                ["split-window", "-h", "-P", "-F", "#{pane_id}", "-t", f"{team_name}:{window_name}"]
            ).strip()
        except TmuxSpawnError:
            # 三级：新建整个 session
            _run_tmux(["new-session", "-d", "-s", team_name, "-n", window_name])
            listing = _run_tmux(["list-panes", "-t", f"{team_name}:{window_name}", "-F", "#{pane_id}"])
            pane_id = listing.strip().split("\n")[0]
    # 窗格就绪后统一 send-keys 启动队员 CLI（cli_cmd 已含环境变量前缀）
    _run_tmux(["send-keys", "-t", pane_id, cli_cmd, "Enter"])
    return TmuxPaneInfo(pane_id=pane_id, window_name=window_name)


def wake_tmux_pane(pane_id: str) -> bool:
    """send-keys 空串唤醒目标 pane（消息已写入 mailbox，这里只是提醒）。"""
    try:
        _run_tmux(["send-keys", "-t", pane_id, "", "Enter"])
        return True
    except TmuxSpawnError:
        return False


def kill_tmux_pane(pane_id: str) -> bool:
    """关闭队员所在的 tmux 窗格。"""
    try:
        _run_tmux(["kill-pane", "-t", pane_id])
        return True
    except TmuxSpawnError:
        return False


__all__ = [
    "TmuxPaneInfo",
    "TmuxSpawnError",
    "build_cli_command",
    "kill_tmux_pane",
    "shell_quote",
    "spawn_tmux_teammate",
    "wake_tmux_pane",
]

"""第 14 章：Worktree 会话状态持久化。

session 写入 {repo}/.mewcode/worktree_session.json（JSON 格式），
进程崩溃后下次启动可以恢复到正确的状态——这是 --resume 恢复能力的基础。

微妙的选择：退出时传 None 会写入一个空 JSON 对象，而不是删除文件。
空文件比「文件不存在」更容易区分「从未使用过 worktree」和「已退出
worktree」；且退出时及时把记录清成空对象，是 --resume 能正确工作的
前提——残留的记录会让恢复逻辑去尝试切回一个已退出的目录。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import WorktreeSession

logger = logging.getLogger(__name__)

SESSION_FILE = "worktree_session.json"

_SESSION_FIELDS = (
    "original_cwd",
    "worktree_path",
    "worktree_name",
    "original_branch",
    "original_head_commit",
    "session_id",
    "hook_based",
)


def _session_path(mewcode_dir: Path) -> Path:
    return mewcode_dir / SESSION_FILE


def save_worktree_session(mewcode_dir: Path, session: WorktreeSession | None) -> None:
    """把 session 持久化到 JSON；传 None 写入空对象（表示已退出）。"""
    path = _session_path(mewcode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if session is None:
        path.write_text("{}", encoding="utf-8")
        return
    data = {field_name: getattr(session, field_name) for field_name in _SESSION_FIELDS}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_worktree_session(mewcode_dir: Path) -> WorktreeSession | None:
    """从 JSON 恢复 session；文件缺失或为空对象返回 None。"""
    path = _session_path(mewcode_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Worktree session file unreadable: %s", exc)
        return None
    if not isinstance(data, dict) or not data:
        return None
    kwargs = {key: data.get(key) for key in _SESSION_FIELDS}
    return WorktreeSession(
        original_cwd=str(kwargs["original_cwd"] or ""),
        worktree_path=str(kwargs["worktree_path"] or ""),
        worktree_name=str(kwargs["worktree_name"] or ""),
        original_branch=str(kwargs["original_branch"] or ""),
        original_head_commit=str(kwargs["original_head_commit"] or ""),
        session_id=str(kwargs["session_id"] or ""),
        hook_based=bool(kwargs["hook_based"]),
    )


__all__ = ["SESSION_FILE", "load_worktree_session", "save_worktree_session"]

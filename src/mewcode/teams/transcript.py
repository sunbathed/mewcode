"""第 15 章：Transcript——队员对话记录的磁盘持久化。

每个队友的完整对话历史按 agent_id 存在团队目录的 transcripts/ 子目录。
这是「空闲与续写」机制的基础：队员停止后上下文不丢弃，Lead 通过
SendMessage 发新指令时，系统从磁盘恢复对话，队员带着完整的历史
上下文继续工作，而不是重头再 spawn。

序列化直接复用 Session.to_dict 的消息结构（role/content/blocks，
含 tool_use 与 tool_result 块），反序列化用 Session.from_dict 重建，
保证往返无损。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..session import Session
from .models import resolve_team_dir

logger = logging.getLogger(__name__)


def transcript_path(team_name: str, agent_id: str) -> Path:
    """某个队员对话记录的落盘位置。"""
    return resolve_team_dir(team_name) / "transcripts" / f"{agent_id}.json"


def save_transcript(team_name: str, agent_id: str, conversation: Session) -> Path:
    """把队员的对话记录写入磁盘，返回文件路径。"""
    path = transcript_path(team_name, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "agent_id": agent_id,
        "model": conversation.model,
        "messages": [m.to_dict() for m in conversation.messages],
    }
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_transcript(team_name: str, agent_id: str) -> Session | None:
    """从磁盘恢复队员的对话记录；文件不存在或损坏返回 None。"""
    path = transcript_path(team_name, agent_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Transcript %s is corrupted, starting fresh conversation", path)
        return None
    session = Session(model=str(data.get("model", "")))
    from ..session import Message

    for item in data.get("messages", []):
        try:
            session.messages.append(Message.from_dict(item))
        except ValueError:
            continue  # 单条坏消息跳过，尽量恢复其余历史
    return session


__all__ = ["load_transcript", "save_transcript", "transcript_path"]

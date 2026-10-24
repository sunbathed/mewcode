"""第 15 章：Mailbox——基于文件的消息传递。

每个收件人一个目录（{team_dir}/mailbox/{agent_id}/），每条消息一个独立
JSON 文件，文件名以时间戳开头——sorted(d.iterdir()) 天然按时间排序。

这个设计消除了并发写入的锁需求：两个进程同时写不同的文件不会冲突，
操作系统层面的文件创建本身就是原子的。in-process 和 Pane 后端共用
同一套机制，队员之间可以直接寻址，通信本质上是异步的——消息会在
收件人下一轮 Agent Loop 开头被读出来注入成 system reminder。

consume 读取后立即删除，是「消费」语义；解析失败的文件跳过不删，
避免坏文件把整个收件箱卡死。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 结构化消息类型：协议化的生命周期 / 审批通信，避免靠「理解自然语言」协调
MESSAGE_TEXT = "text"
MESSAGE_SHUTDOWN_REQUEST = "shutdown_request"
MESSAGE_SHUTDOWN_RESPONSE = "shutdown_response"
MESSAGE_PLAN_APPROVAL_RESPONSE = "plan_approval_response"

KNOWN_MESSAGE_TYPES = {
    MESSAGE_TEXT,
    MESSAGE_SHUTDOWN_REQUEST,
    MESSAGE_SHUTDOWN_RESPONSE,
    MESSAGE_PLAN_APPROVAL_RESPONSE,
}


@dataclass(slots=True)
class MailboxMessage:
    """一条团队消息：唯一 ID、发件人、收件人、内容、时间戳。"""

    id: str
    from_agent: str
    to_agent: str
    content: str
    summary: str = ""
    message_type: str = MESSAGE_TEXT
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "content": self.content,
            "summary": self.summary,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MailboxMessage":
        return cls(
            id=str(data.get("id", "")),
            from_agent=str(data.get("from_agent", "")),
            to_agent=str(data.get("to_agent", "")),
            content=str(data.get("content", "")),
            summary=str(data.get("summary", "")),
            message_type=str(data.get("message_type", MESSAGE_TEXT)),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )


def create_message(
    from_agent: str,
    to_agent: str,
    content: str,
    summary: str = "",
    message_type: str = MESSAGE_TEXT,
    metadata: dict[str, Any] | None = None,
) -> MailboxMessage:
    """消息工厂：UUID 前 12 位做 ID，时间戳缺省取当前时刻。"""
    return MailboxMessage(
        id=uuid.uuid4().hex[:12],
        from_agent=from_agent,
        to_agent=to_agent,
        content=content,
        summary=summary,
        message_type=message_type if message_type in KNOWN_MESSAGE_TYPES else MESSAGE_TEXT,
        timestamp=time.time(),
        metadata=metadata or {},
    )


class Mailbox:
    """文件收件箱：write / consume / broadcast。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    def _agent_dir(self, agent_id: str) -> Path:
        return self._base_dir / agent_id

    def write(self, agent_id: str, message: MailboxMessage) -> Path:
        """投递一条消息到指定收件人的邮箱（一消息一文件，原子创建）。"""
        d = self._agent_dir(agent_id)
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{message.timestamp:.6f}_{message.id}.json"
        path = d / filename
        path.write_text(
            json.dumps(message.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def consume(self, agent_id: str) -> list[MailboxMessage]:
        """读取并删除收件人的全部未读消息（按时间戳排序）。"""
        d = self._agent_dir(agent_id)
        if not d.exists():
            return []
        messages: list[MailboxMessage] = []
        for f in sorted(d.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                messages.append(MailboxMessage.from_dict(data))
                f.unlink()
            except (json.JSONDecodeError, KeyError, OSError, ValueError):
                continue  # 坏文件跳过不删，避免数据丢失
        return messages

    def broadcast(
        self,
        team_members: list[str],
        message: MailboxMessage,
        exclude: str = "",
    ) -> int:
        """给所有成员发消息，可排除发件人自己；返回投递人数。"""
        count = 0
        for agent_id in team_members:
            if agent_id == exclude:
                continue
            self.write(agent_id, message)
            count += 1
        return count


__all__ = [
    "KNOWN_MESSAGE_TYPES",
    "Mailbox",
    "MailboxMessage",
    "MESSAGE_PLAN_APPROVAL_RESPONSE",
    "MESSAGE_SHUTDOWN_REQUEST",
    "MESSAGE_SHUTDOWN_RESPONSE",
    "MESSAGE_TEXT",
    "create_message",
]

"""第 9 章：JSONL 会话持久化。

会话存档用 JSONL（JSON Lines）格式：每行一个 JSON 对象，代表一条消息记录。
为什么是 JSONL 而不是 SQLite 或普通 JSON：
  - 追加写入 O(1)：每条新消息直接追加到文件末尾，不需要读回整个文件
  - 崩溃安全：已经写入的完整行不受影响，最坏情况是最后一行不完整，恢复时跳过
  - 增量加载：逐行读取，遇到解析错误的行跳过继续，不会因一行坏数据丢掉整个会话

每个会话除了 .jsonl 文件还有一个 .meta 文件存元数据（ID/标题/摘要/消息数/
token 量/创建时间/最后活跃），列出会话时只读小的 meta 文件，不用扫描整个 JSONL。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..session import Message, TextBlock, ToolResultBlock, ToolUseBlock, _block_from_dict, generate_session_id

# 标题取用户第一条消息的前 N 个字符
TITLE_MAX_LENGTH = 50

# 恢复时距上次活跃超过该时长就插入时间跨度提示
TIME_GAP_THRESHOLD = timedelta(hours=24)

# 过期会话清理：默认清理 30 天前的会话
DEFAULT_MAX_AGE_DAYS = 30


class RecordType(str, Enum):
    """JSONL 记录的类型标签。"""

    SYSTEM_PROMPT = "system_prompt"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    COMPRESSION = "compression"


RECORD_TYPES = tuple(item.value for item in RecordType)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SessionRecord:
    """一条 JSONL 记录：类型 + 原始内容 + 时间戳。

    content 的类型是 Any：纯文本消息是字符串；带工具调用的助手回复是
    content block 数组；tool_result 的 content 是结果文本。
    """

    type: RecordType
    content: Any
    timestamp: datetime = field(default_factory=utc_now)
    tool_use_id: str | None = None
    is_error: bool = False

    def to_jsonl(self) -> str:
        data: dict[str, Any] = {
            "type": self.type.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.tool_use_id is not None:
            data["tool_use_id"] = self.tool_use_id
        if self.type == RecordType.TOOL_RESULT:
            data["is_error"] = self.is_error
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "SessionRecord | None":
        """解析一行 JSONL；解析失败返回 None（恢复时跳过该行）。"""
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        raw_type = data.get("type")
        try:
            record_type = RecordType(raw_type)
        except ValueError:
            return None
        timestamp = data.get("timestamp")
        parsed_ts = _parse_timestamp(timestamp)
        return cls(
            type=record_type,
            content=data.get("content"),
            timestamp=parsed_ts,
            tool_use_id=data.get("tool_use_id") if isinstance(data.get("tool_use_id"), str) else None,
            is_error=bool(data.get("is_error", False)),
        )

    @classmethod
    def from_message(cls, message: Message) -> list["SessionRecord"]:
        """一条内存消息可能展开为多条记录。

        - 纯文本 user / assistant / system：一条记录
        - assistant 的 text + tool_use blocks：打包成一个 content block 数组记录
        - user 的 tool_result blocks：每个工具结果单独一条 TOOL_RESULT 记录
        """
        now = utc_now()
        blocks = message.blocks or []

        # 纯工具结果消息（user 角色带 ToolResultBlock，无文本）
        if message.role == "user" and blocks and all(isinstance(b, ToolResultBlock) for b in blocks):
            return [
                cls(
                    type=RecordType.TOOL_RESULT,
                    content=b.content,
                    timestamp=now,
                    tool_use_id=b.tool_use_id,
                    is_error=False,
                )
                for b in blocks
            ]

        # assistant 消息：文本与 tool_use 必须打包在同一条记录里（API 要求）
        if blocks:
            content_blocks = [b.to_dict() for b in blocks if isinstance(b, (TextBlock, ToolUseBlock))]
            if content_blocks:
                return [cls(type=RecordType.ASSISTANT, content=content_blocks, timestamp=now)]

        if message.role == "system":
            return [cls(type=RecordType.SYSTEM_PROMPT, content=message.content, timestamp=now)]
        if message.role == "assistant":
            return [cls(type=RecordType.ASSISTANT, content=message.content, timestamp=now)]
        return [cls(type=RecordType.USER, content=message.content, timestamp=now)]


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return utc_now()
    return utc_now()


def validate_message_chain(records: list[SessionRecord]) -> int:
    """验证消息链完整性，返回最后一个「所有工具调用都有结果」的位置。

    JSONL 文件可能因崩溃而截断：assistant 发了工具调用但 tool_result 还没
    写入。恢复时追踪所有未完成的工具调用，截断到最后完整位置。
    """
    last_valid = 0
    pending_tool_uses: set[str] = set()
    for i, record in enumerate(records):
        if record.type == RecordType.ASSISTANT and isinstance(record.content, list):
            for block in record.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if isinstance(tool_id, str):
                        pending_tool_uses.add(tool_id)
        elif record.type == RecordType.TOOL_RESULT:
            if record.tool_use_id:
                pending_tool_uses.discard(record.tool_use_id)
        if not pending_tool_uses:
            last_valid = i + 1
    return last_valid


def records_to_messages(records: list[SessionRecord]) -> list[Message]:
    """把 JSONL 记录转回内存消息列表。

    工具结果在文件里是一条一条的 TOOL_RESULT 记录，恢复时攒进缓冲区，
    遇到下一条非工具结果记录时才一起打包成一条 user 消息。
    COMPRESSION 记录转成带 [摘要] 前缀的 user 消息（当作对话历史的一部分）。
    """
    messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []

    def flush_pending() -> None:
        if pending_tool_results:
            messages.append(Message("user", "", blocks=list(pending_tool_results)))
            pending_tool_results.clear()

    for record in records:
        if record.type == RecordType.TOOL_RESULT:
            pending_tool_results.append(
                ToolResultBlock(tool_use_id=record.tool_use_id or "", content=str(record.content or ""))
            )
            continue
        flush_pending()

        if record.type == RecordType.COMPRESSION:
            messages.append(Message("user", f"[摘要] {record.content}"))
        elif record.type == RecordType.ASSISTANT:
            if isinstance(record.content, list):
                blocks = [_block_from_dict(b) for b in record.content if isinstance(b, dict)]
                text = "\n".join(b.text for b in blocks if isinstance(b, TextBlock))
                messages.append(Message("assistant", text, blocks=blocks or None))
            else:
                messages.append(Message("assistant", str(record.content or "")))
        elif record.type == RecordType.USER:
            messages.append(Message("user", str(record.content or "")))
        elif record.type == RecordType.SYSTEM_PROMPT:
            messages.append(Message("system", str(record.content or "")))
    flush_pending()
    return messages


@dataclass(slots=True)
class SessionMeta:
    """会话元数据：列表页只读 meta 文件，不用扫描整个 JSONL。"""

    id: str
    title: str = ""
    summary: str = ""
    message_count: int = 0
    total_tokens: int = 0
    model: str = ""
    created_at: datetime = field(default_factory=utc_now)
    last_active: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SessionMeta":
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ValueError("会话 meta 格式无效")
        return cls(
            id=data["id"],
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            message_count=int(data.get("message_count", 0) or 0),
            total_tokens=int(data.get("total_tokens", 0) or 0),
            model=str(data.get("model", "")),
            created_at=_parse_timestamp(data.get("created_at")),
            last_active=_parse_timestamp(data.get("last_active")),
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SessionMeta | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError):
            return None


def build_title(messages: list[Message]) -> str:
    """标题自动生成：取用户第一条消息的前 50 个字符。"""
    for message in messages:
        if message.role == "user" and message.content and message.content.strip():
            text = message.content.strip().replace("\n", " ")
            return text[:TITLE_MAX_LENGTH]
    return ""


def build_time_gap_message(last_active: datetime) -> Message | None:
    """距上次活跃超过阈值时插入提醒：提醒 Agent 代码可能已经变了。"""
    gap = utc_now() - last_active
    if gap < TIME_GAP_THRESHOLD:
        return None
    hours = int(gap.total_seconds() // 3600)
    gap_text = f"{hours // 24} 天" if hours >= 48 else f"{hours} 小时"
    return Message(
        role="user",
        content=(
            f"[系统提示] 距离上次会话已过去 {gap_text}。"
            "期间代码可能有变更，建议在操作前重新读取相关文件。"
        ),
    )


class SessionManager:
    """会话的创建、恢复、列表、删除、过期清理。

    维护会话目录，提供所有会话操作的入口。会话 ID 格式
    YYYYMMDD-HHMMSS-xxxx：时间戳一眼看出创建时间，随机后缀避免同秒冲突。
    """

    def __init__(self, work_dir: str | Path | None = None, sessions_dir: str | Path | None = None) -> None:
        if sessions_dir is not None:
            self.sessions_dir = Path(sessions_dir).expanduser()
        elif work_dir is not None:
            self.sessions_dir = Path(work_dir).expanduser() / ".mewcode" / "sessions"
        else:
            self.sessions_dir = Path.home() / ".mewcode" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def jsonl_path(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self.sessions_dir / f"{session_id}.jsonl"

    def meta_path(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self.sessions_dir / f"{session_id}.meta"

    @staticmethod
    def _validate_id(session_id: str) -> None:
        if not session_id or Path(session_id).name != session_id:
            raise ValueError("会话 ID 无效")

    # ------------------------------------------------------------------
    # 创建 / 追加
    # ------------------------------------------------------------------

    def create(self, model: str = "") -> tuple[str, Path]:
        """创建新会话：生成唯一 ID，打开（创建）JSONL 文件。"""
        session_id = generate_session_id()
        path = self.jsonl_path(session_id)
        with open(path, "a", encoding="utf-8"):
            pass
        meta = SessionMeta(id=session_id, model=model)
        meta.save(self.meta_path(session_id))
        return session_id, path

    def append_record(self, session_id: str, record: SessionRecord) -> None:
        """追加写入一条记录（先写文件再更新 meta：崩溃时下次从文件重建）。"""
        path = self.jsonl_path(session_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(record.to_jsonl() + "\n")
            f.flush()
        meta = self.load_meta(session_id)
        if meta is None:
            meta = SessionMeta(id=session_id)
        meta.message_count += 1
        meta.last_active = utc_now()
        meta.save(self.meta_path(session_id))

    def append_messages(self, session_id: str, messages: list[Message]) -> None:
        """批量追加：每条消息展开为一条或多条记录。"""
        for message in messages:
            for record in SessionRecord.from_message(message):
                self.append_record(session_id, record)

    @staticmethod
    def records_from_message(message: Message) -> list[SessionRecord]:
        """一条内存消息展开为 JSONL 记录（供全量重写与追加共用）。"""
        return SessionRecord.from_message(message)

    @staticmethod
    def meta_to_json(meta: SessionMeta) -> str:
        return json.dumps(meta.to_dict(), ensure_ascii=False)

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def load_meta(self, session_id: str) -> SessionMeta | None:
        meta = SessionMeta.load(self.meta_path(session_id))
        return meta

    def read_records(self, session_id: str) -> list[SessionRecord]:
        """逐行读取 JSONL：解析失败的行跳过继续（崩溃只丢半行，不丢整个会话）。"""
        path = self.jsonl_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"会话不存在：{session_id}")
        records: list[SessionRecord] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                record = SessionRecord.from_jsonl(line)
                if record is not None:
                    records.append(record)
        return records

    def resume(self, session_id: str) -> list[Message]:
        """恢复会话：读取 JSONL → 校验消息链 → 截断到最后一个完整位置。"""
        records = self.read_records(session_id)
        valid_count = validate_message_chain(records)
        records = records[:valid_count]
        return records_to_messages(records)

    # ------------------------------------------------------------------
    # 列表 / 删除 / 清理
    # ------------------------------------------------------------------

    def list_metas(self) -> list[SessionMeta]:
        """扫描 meta 文件（快）：列表页不需要解析 JSONL。"""
        metas: list[SessionMeta] = []
        for meta_path in sorted(self.sessions_dir.glob("*.meta")):
            meta = SessionMeta.load(meta_path)
            if meta is not None:
                metas.append(meta)
        return sorted(metas, key=lambda m: m.last_active, reverse=True)

    def delete(self, session_id: str) -> bool:
        removed = False
        for path in (self.jsonl_path(session_id), self.meta_path(session_id)):
            if path.exists():
                path.unlink()
                removed = True
        return removed

    def cleanup(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
        """清理超过 max_age_days 没有活跃的会话（遍历 meta 文件判断 last_active）。"""
        cutoff = utc_now() - timedelta(days=max_age_days)
        removed = 0
        for meta in self.list_metas():
            if meta.last_active < cutoff:
                if self.delete(meta.id):
                    removed += 1
        return removed


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "RECORD_TYPES",
    "RecordType",
    "SessionManager",
    "SessionMeta",
    "SessionRecord",
    "TIME_GAP_THRESHOLD",
    "TITLE_MAX_LENGTH",
    "build_time_gap_message",
    "build_title",
    "generate_session_id",
    "records_to_messages",
    "validate_message_chain",
]

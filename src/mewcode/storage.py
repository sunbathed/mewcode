from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .memory.session import (
    SessionManager,
    SessionMeta,
    build_title,
    records_to_messages,
    validate_message_chain,
)
from .session import Session
from .context import estimate_messages_tokens


class SessionStorageError(RuntimeError):
    """Raised when a session cannot be read or written."""


class SessionStorage:
    """JSONL 会话存储（第 9 章）。

    每个会话一对文件：
      - {id}.jsonl：全量聊天记录，每行一条 JSON（追加 O(1)、崩溃只丢半行）
      - {id}.meta ：会话元信息（ID/标题/消息数/token 量/创建时间/最后活跃）

    save() 全量重写 JSONL + meta（幂等）；load() 逐行解析、坏行跳过、
    消息链校验截断，保证崩溃后的会话也能恢复。
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        configured = directory or os.getenv("MEWCODE_SESSION_DIR")
        if configured:
            self.directory = Path(configured).expanduser()
        else:
            self.directory = Path.home() / ".mewcode" / "sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._manager = SessionManager(sessions_dir=self.directory)

    def path_for(self, session_id: str) -> Path:
        if not session_id or Path(session_id).name != session_id:
            raise SessionStorageError("会话 ID 无效")
        return self.directory / f"{session_id}.jsonl"

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save(self, session: Session) -> None:
        target = self.path_for(session.id)
        temporary = target.with_suffix(".jsonl.tmp")
        try:
            # 全量重写 JSONL：每条消息按类型展开成一条或多条记录
            lines = []
            for message in session.messages:
                for record in SessionManager.records_from_message(message):
                    lines.append(record.to_jsonl())
            temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            os.replace(temporary, target)
            self._save_meta(session)
        except OSError as exc:
            raise SessionStorageError("会话保存失败") from exc

    def _save_meta(self, session: Session) -> None:
        meta = SessionMeta(
            id=session.id,
            title=build_title(session.messages),
            summary="",
            message_count=len(session.messages),
            total_tokens=estimate_messages_tokens(session.api_messages()),
            model=session.model,
            created_at=_parse_dt(session.created_at),
            last_active=_parse_dt(session.updated_at),
        )
        meta_path = self.directory / f"{session.id}.meta"
        temporary = meta_path.with_suffix(".meta.tmp")
        try:
            temporary.write_text(
                SessionManager.meta_to_json(meta),
                encoding="utf-8",
            )
            os.replace(temporary, meta_path)
        except OSError:
            pass  # meta 写入失败不影响会话主体（列表页只展示较旧信息）

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> Session:
        path = self.path_for(session_id)
        if not path.exists():
            # 兼容旧格式：jsonl 不存在时尝试旧 .json 文件
            legacy = self.directory / f"{session_id}.json"
            if legacy.exists():
                try:
                    return self._load_legacy_json(legacy)
                except (OSError, ValueError, TypeError):
                    raise SessionStorageError("会话文件损坏或格式无效") from None
            raise SessionStorageError("会话不存在")
        try:
            records = self._manager.read_records(session_id)
        except FileNotFoundError as exc:
            raise SessionStorageError("会话不存在") from exc
        if self.path_for(session_id).stat().st_size == 0:
            # 空文件：从未写入过消息的空会话，合法
            messages: list = []
        elif not records:
            # 文件有内容但一行有效记录都没有：整文件损坏
            raise SessionStorageError("会话文件损坏或格式无效")
        else:
            valid_count = validate_message_chain(records)
            messages = records_to_messages(records[:valid_count])
        meta = self._manager.load_meta(session_id)
        session = Session(
            id=session_id,
            model=meta.model if meta else "",
            created_at=meta.created_at.isoformat() if meta else "",
            updated_at=meta.last_active.isoformat() if meta else "",
        )
        session.messages = messages
        return session

    @staticmethod
    def _load_legacy_json(path: Path) -> Session:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return Session.from_dict(data)

    def load_meta(self, session_id: str) -> SessionMeta | None:
        """读取会话元信息（列表/恢复流程判断时间跨度时用）。"""
        return self._manager.load_meta(session_id)

    def list_metas(self) -> list[SessionMeta]:
        """会话元信息列表（按最后活跃倒序）；展示标题/摘要时比 Session 更完整。"""
        return self._manager.list_metas()

    # ------------------------------------------------------------------
    # 列表 / 最新
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[Session]:
        """扫描 meta 文件（快）：列表展示不需要解析 JSONL。

        返回轻量 Session（仅 id/model/时间戳，messages 为空）；
        latest() 会补全完整消息。
        """
        sessions: list[Session] = []
        for meta in self._manager.list_metas():
            sessions.append(
                Session(
                    id=meta.id,
                    model=meta.model,
                    created_at=meta.created_at.isoformat(),
                    updated_at=meta.last_active.isoformat(),
                )
            )
        return sessions

    def latest(self) -> Session | None:
        metas = self._manager.list_metas()
        if not metas:
            return None
        try:
            return self.load(metas[0].id)
        except SessionStorageError:
            return None

    def delete(self, session_id: str) -> bool:
        return self._manager.delete(session_id)

    def cleanup(self, max_age_days: int = 30) -> int:
        return self._manager.cleanup(max_age_days=max_age_days)


__all__ = ["SessionStorage", "SessionStorageError"]


def _parse_dt(value: str | None) -> datetime:
    """把 ISO 字符串时间戳解析为 datetime；无效时回退到当前时间。"""
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now().astimezone()

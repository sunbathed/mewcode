"""第 9 章：JSONL 会话持久化（记录模型、链校验、恢复、meta、过期清理）。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from mewcode.memory.session import (
    DEFAULT_MAX_AGE_DAYS,
    SessionManager,
    SessionMeta,
    SessionRecord,
    build_time_gap_message,
    generate_session_id,
    records_to_messages,
    validate_message_chain,
)
from mewcode.memory.session import RecordType
from mewcode.session import Message, Session, TextBlock, ToolResultBlock, ToolUseBlock


def test_session_id_format():
    sid = generate_session_id()
    # YYYYMMDD-HHMMSS-xxxx：前 15 字符时间戳 + 4 位十六进制随机后缀
    assert len(sid) == 20
    assert sid[8] == "-" and sid[15] == "-"
    assert len(set(generate_session_id() for _ in range(100))) == 100


def test_record_roundtrip():
    record = SessionRecord(
        type=RecordType.ASSISTANT,
        content=[{"type": "text", "text": "好的"}, {"type": "tool_use", "id": "t1", "name": "ReadFile", "input": {}}],
        timestamp=datetime.now(timezone.utc),
        tool_use_id=None,
    )
    restored = SessionRecord.from_jsonl(record.to_jsonl())
    assert restored.type == RecordType.ASSISTANT
    assert restored.content[1]["id"] == "t1"


def test_bad_line_skipped():
    assert SessionRecord.from_jsonl("{broken json") is None
    assert SessionRecord.from_jsonl("not json at all") is None
    assert SessionRecord.from_jsonl('{"type": "unknown_type", "content": "x"}') is None


def test_message_to_records_and_back():
    session = Session()
    session.add_message("user", "读一下文件")
    session.add_message(
        "assistant",
        "好的",
        blocks=[TextBlock("好的"), ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.py"})],
    )
    session.add_tool_results([("call_1", "完整文件内容")])

    records = [record for message in session.messages for record in SessionRecord.from_message(message)]
    assert [r.type for r in records] == [
        RecordType.USER,
        RecordType.ASSISTANT,
        RecordType.TOOL_RESULT,
    ]
    # assistant 的 text + tool_use 打包在同一条记录里
    assert isinstance(records[1].content, list)
    assert records[1].content[0]["type"] == "text"
    assert records[1].content[1]["type"] == "tool_use"

    restored = records_to_messages(records)
    assert [m.role for m in restored] == ["user", "assistant", "user"]
    # 工具结果被合并回一条 user 消息（多个 ToolResultBlock 块）
    assert restored[2].blocks[0].tool_use_id == "call_1"
    assert restored[2].blocks[0].content == "完整文件内容"


def test_multiple_tool_results_batched():
    records = [
        SessionRecord(RecordType.USER, "问题", datetime.now(timezone.utc)),
        SessionRecord(RecordType.TOOL_RESULT, "r1", datetime.now(timezone.utc), tool_use_id="a"),
        SessionRecord(RecordType.TOOL_RESULT, "r2", datetime.now(timezone.utc), tool_use_id="b"),
        SessionRecord(RecordType.USER, "下一问", datetime.now(timezone.utc)),
    ]
    restored = records_to_messages(records)
    assert len(restored) == 3
    assert [m.role for m in restored] == ["user", "user", "user"]
    assert len(restored[1].blocks) == 2


def test_compression_record_becomes_summary_message():
    records = [SessionRecord(RecordType.COMPRESSION, "之前的摘要", datetime.now(timezone.utc))]
    restored = records_to_messages(records)
    assert restored[0].role == "user"
    assert restored[0].content.startswith("[摘要]")


def test_validate_message_chain_truncates_incomplete_tail():
    """assistant 发了工具调用但没有结果：截断到最后一个完整位置。"""
    now = datetime.now(timezone.utc)
    records = [
        SessionRecord(RecordType.USER, "问题", now),
        SessionRecord(RecordType.ASSISTANT, [{"type": "tool_use", "id": "t1", "name": "ReadFile", "input": {}}], now),
        SessionRecord(RecordType.TOOL_RESULT, "结果", now, tool_use_id="t1"),
        SessionRecord(RecordType.ASSISTANT, [{"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}], now),
    ]
    last_valid = validate_message_chain(records)
    assert last_valid == 3  # t1 闭环后的位置，t2 没有结果被截断


def test_manager_create_resume(tmp_path):
    manager = SessionManager(work_dir=tmp_path)
    session_id, path = manager.create(model="demo")
    assert path.name == f"{session_id}.jsonl"
    assert (manager.sessions_dir / f"{session_id}.meta").exists()

    manager.append_messages(
        session_id,
        [Message("user", "你好"), Message("assistant", "你好！")],
    )
    resumed = manager.resume(session_id)
    assert [m.content for m in resumed] == ["你好", "你好！"]
    meta = manager.load_meta(session_id)
    assert meta.message_count == 2
    assert meta.model == "demo"


def test_manager_list_and_delete(tmp_path):
    manager = SessionManager(work_dir=tmp_path)
    id1, _ = manager.create()
    id2, _ = manager.create()
    manager.append_messages(id1, [Message("user", "会话一")])
    manager.append_messages(id2, [Message("user", "会话二")])
    metas = manager.list_metas()
    assert len(metas) == 2
    assert manager.delete(id1) is True
    assert manager.delete(id1) is False  # 已删除
    assert len(manager.list_metas()) == 1


def test_manager_cleanup_expired(tmp_path, monkeypatch):
    manager = SessionManager(work_dir=tmp_path)
    session_id, _ = manager.create()
    manager.append_messages(session_id, [Message("user", "旧会话")])

    old = datetime.now(timezone.utc) - timedelta(days=DEFAULT_MAX_AGE_DAYS + 1)
    meta = manager.load_meta(session_id)
    meta.last_active = old
    meta.save(manager.meta_path(session_id))

    removed = manager.cleanup(max_age_days=DEFAULT_MAX_AGE_DAYS)
    assert removed == 1
    assert len(manager.list_metas()) == 0


def test_time_gap_message_recent_none():
    assert build_time_gap_message(datetime.now(timezone.utc)) is None


def test_time_gap_message_old():
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    message = build_time_gap_message(old)
    assert message is not None
    assert "30 小时" in message.content or "1 天" in message.content
    assert "重新读取相关文件" in message.content


def test_session_meta_roundtrip(tmp_path):
    meta = SessionMeta(id="abc", title="标题", message_count=3, model="demo")
    path = tmp_path / "abc.meta"
    meta.save(path)
    restored = SessionMeta.load(path)
    assert restored.id == "abc"
    assert restored.title == "标题"
    assert restored.message_count == 3
    assert restored.model == "demo"


def test_session_meta_bad_file_none(tmp_path):
    path = tmp_path / "bad.meta"
    path.write_text("{broken", encoding="utf-8")
    assert SessionMeta.load(path) is None

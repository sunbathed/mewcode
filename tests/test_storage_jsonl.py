"""第 9 章：SessionStorage 的 JSONL 格式改造（坏行跳过、链校验、meta 列表）。"""

import json

import pytest

from mewcode.session import Session, ToolResultBlock, ToolUseBlock
from mewcode.storage import SessionStorage, SessionStorageError


def test_save_load_and_latest(tmp_path):
    storage = SessionStorage(tmp_path)
    session = Session(id="abc", model="demo")
    session.add_message("user", "你好")
    session.add_message("assistant", "你好！")
    storage.save(session)
    loaded = storage.load("abc")
    assert loaded.api_messages() == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]
    assert storage.latest().id == "abc"
    # JSONL + meta 双文件
    assert (tmp_path / "abc.jsonl").exists()
    assert (tmp_path / "abc.meta").exists()


def test_jsonl_is_line_delimited(tmp_path):
    storage = SessionStorage(tmp_path)
    session = Session(id="abc", model="demo")
    session.add_message("user", "你好")
    session.add_message("assistant", "你好！")
    storage.save(session)
    lines = (tmp_path / "abc.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["type"] in {"user", "assistant"}


def test_tool_use_and_result_roundtrip(tmp_path):
    storage = SessionStorage(tmp_path)
    session = Session(id="abc", model="demo")
    session.add_message("user", "读一下文件")
    session.add_message(
        "assistant",
        "好的",
        blocks=[
            __import__("mewcode.session", fromlist=["TextBlock"]).TextBlock("好的"),
            ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.py"}),
        ],
    )
    session.add_tool_results([("call_1", "完整文件内容")])
    storage.save(session)
    loaded = storage.load("abc")
    assert len(loaded.messages) == 3
    tool_use = loaded.messages[1].blocks[1]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.id == "call_1"
    tool_result = loaded.messages[2].blocks[0]
    assert isinstance(tool_result, ToolResultBlock)
    assert tool_result.content == "完整文件内容"


def test_corrupt_session(tmp_path):
    storage = SessionStorage(tmp_path)
    (tmp_path / "bad.jsonl").write_text("{bad", encoding="utf-8")
    with pytest.raises(SessionStorageError, match="损坏"):
        storage.load("bad")


def test_partial_bad_lines_skipped(tmp_path):
    """坏行跳过继续：一行崩溃半行不影响前面的完整消息。"""
    storage = SessionStorage(tmp_path)
    good = json.dumps({"type": "user", "content": "完整消息", "timestamp": "2024-01-01T00:00:00+00:00"})
    (tmp_path / "mix.jsonl").write_text(f"{good}\n{{broken half line\n", encoding="utf-8")
    loaded = storage.load("mix")
    assert [m.content for m in loaded.messages] == ["完整消息"]


def test_incomplete_tool_tail_truncated_on_load(tmp_path):
    """JSONL 末尾是没配上结果的工具调用：恢复时截断到最后一个完整位置。"""
    storage = SessionStorage(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    lines = [
        json.dumps({"type": "user", "content": "问题", "timestamp": now}),
        json.dumps(
            {
                "type": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "ReadFile", "input": {}}],
                "timestamp": now,
            }
        ),
        json.dumps({"type": "tool_result", "content": "结果", "tool_use_id": "t1", "timestamp": now}),
        # 不完整尾部：t2 没有结果
        json.dumps(
            {
                "type": "assistant",
                "content": [{"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}],
                "timestamp": now,
            }
        ),
    ]
    (tmp_path / "trunc.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    loaded = storage.load("trunc")
    assert len(loaded.messages) == 3  # 最后一条不完整 assistant 被截断


def test_empty_jsonl_is_valid_empty_session(tmp_path):
    storage = SessionStorage(tmp_path)
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    loaded = storage.load("empty")
    assert loaded.messages == []


def test_list_sessions_reads_meta_only(tmp_path):
    storage = SessionStorage(tmp_path)
    for sid in ("s1", "s2"):
        session = Session(id=sid, model="demo")
        session.add_message("user", f"会话 {sid}")
        storage.save(session)
    sessions = storage.list_sessions()
    assert [s.id for s in sessions] == ["s2", "s1"]  # 最后活跃倒序


def test_delete(tmp_path):
    storage = SessionStorage(tmp_path)
    session = Session(id="abc", model="demo")
    session.add_message("user", "你好")
    storage.save(session)
    assert storage.delete("abc") is True
    assert storage.delete("abc") is False
    with pytest.raises(SessionStorageError, match="不存在"):
        storage.load("abc")


def test_legacy_json_compat(tmp_path):
    """旧 .json 格式会话仍可读取（无缝迁移）。"""
    storage = SessionStorage(tmp_path)
    session = Session(id="old", model="demo")
    session.add_message("user", "旧消息")
    (tmp_path / "old.json").write_text(
        json.dumps(session.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    loaded = storage.load("old")
    assert [m.content for m in loaded.messages] == ["旧消息"]

"""第四章：流式收集器测试（实时积攒 + 分片 JSON 解析）。"""

import pytest

from mewcode.engine.collector import StreamCollector, parse_tool_arguments
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart


def test_empty_collector():
    collector = StreamCollector()
    result = collector.result()
    assert result.text == ""
    assert result.tool_uses == ()
    assert result.stop_reason is None


def test_collects_text_and_stop_reason():
    collector = StreamCollector()
    collector.feed(TextDelta("你好"))
    collector.feed(TextDelta("，世界"))
    collector.feed(StreamEnd("stop"))
    result = collector.result()
    assert result.text == "你好，世界"
    assert result.stop_reason == "stop"


def test_collects_single_tool_use():
    collector = StreamCollector()
    collector.feed(ToolUseStart(index=0, id="call_1", name="ReadFile"))
    collector.feed(ToolUseDelta(index=0, args_fragment='{"path":'))
    collector.feed(ToolUseDelta(index=0, args_fragment='"a.txt"}'))
    collector.feed(StreamEnd("tool_calls"))
    result = collector.result()
    assert len(result.tool_uses) == 1
    use = result.tool_uses[0]
    assert use.id == "call_1"
    assert use.name == "ReadFile"
    assert use.input == {"path": "a.txt"}
    assert result.stop_reason == "tool_calls"


def test_collects_multiple_tools_in_streaming_order():
    collector = StreamCollector()
    # 乱序到达（碎片交错）：index 0 与 1 交替
    collector.feed(ToolUseStart(index=1, id="c1", name="EditFile"))
    collector.feed(ToolUseDelta(index=1, args_fragment='{"path":"a.txt","edits":[]}'))
    collector.feed(ToolUseStart(index=0, id="c0", name="ReadFile"))
    collector.feed(ToolUseDelta(index=0, args_fragment='{"path":"a.txt"}'))
    result = collector.result()
    assert [u.id for u in result.tool_uses] == ["c0", "c1"]  # 按 index 排序
    assert result.tool_uses[0].input == {"path": "a.txt"}
    assert result.tool_uses[1].name == "EditFile"


def test_missing_id_gets_fallback():
    collector = StreamCollector()
    collector.feed(ToolUseStart(index=3, id="", name="Bash"))
    collector.feed(ToolUseDelta(index=3, args_fragment='{"command":"ls"}'))
    result = collector.result()
    assert result.tool_uses[0].id == "tool_3"


def test_invalid_json_args_become_json_error():
    collector = StreamCollector()
    collector.feed(ToolUseStart(index=0, id="c", name="Bash"))
    collector.feed(ToolUseDelta(index=0, args_fragment='{"command": broken'))
    result = collector.result()
    assert "_json_error" in result.tool_uses[0].input


def test_parse_tool_arguments():
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert parse_tool_arguments("not json") == {"_json_error": "not json"}
    # 解析出非 dict 内容也兜底
    assert parse_tool_arguments("[1,2]") == {"_json_error": "[1,2]"}


def test_text_and_tools_mixed():
    collector = StreamCollector()
    collector.feed(TextDelta("我先看看。"))
    collector.feed(ToolUseStart(index=0, id="c", name="Glob"))
    collector.feed(ToolUseDelta(index=0, args_fragment='{"pattern":"*.py"}'))
    collector.feed(StreamEnd("tool_calls"))
    result = collector.result()
    assert result.text == "我先看看。"
    assert len(result.tool_uses) == 1
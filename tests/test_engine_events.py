"""第四章：AgentEvent 事件流的事件类型测试。"""

from mewcode.engine.events import (
    AgentStopReason,
    ErrorEvent,
    LoopCompleteEvent,
    TextChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)


def test_text_chunk_event_fields():
    event = TextChunkEvent("你好")
    assert event.kind == "stream_text"
    assert event.text == "你好"
    # frozen dataclass 不可变
    try:
        event.text = "x"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("TextChunkEvent 应为不可变")


def test_tool_use_event_fields():
    event = ToolUseEvent(id="call_1", name="ReadFile", input={"path": "a.txt"})
    assert event.kind == "tool_use"
    assert event.id == "call_1"
    assert event.name == "ReadFile"
    assert event.input == {"path": "a.txt"}


def test_tool_result_event_fields_and_defaults():
    event = ToolResultEvent(tool_use_id="call_1", output="内容", is_error=False, error="", duration=0.5)
    assert event.kind == "tool_result"
    assert event.tool_use_id == "call_1"
    assert event.output == "内容"
    assert event.is_error is False
    assert event.error == ""
    assert event.duration == 0.5

    defaulted = ToolResultEvent(tool_use_id="c", output="", is_error=True)
    assert defaulted.error == ""
    assert defaulted.duration == 0.0


def test_turn_complete_event():
    event = TurnCompleteEvent(turn=2, stop_reason="tool_calls")
    assert event.kind == "turn_complete"
    assert event.turn == 2
    assert event.stop_reason == "tool_calls"

    no_reason = TurnCompleteEvent(turn=1)
    assert no_reason.stop_reason is None


def test_loop_complete_event_fields():
    event = LoopCompleteEvent(total_turns=3, stop_reason=AgentStopReason.END_TURN)
    assert event.kind == "loop_complete"
    assert event.total_turns == 3
    assert event.stop_reason == AgentStopReason.END_TURN


def test_usage_event_fields():
    event = UsageEvent(turn=1, input_tokens=10, output_tokens=5, total_input_tokens=10, total_output_tokens=5)
    assert event.kind == "usage"
    assert event.turn == 1
    assert event.input_tokens == 10
    assert event.total_input_tokens == 10

    zero = UsageEvent(turn=0)
    assert zero.input_tokens == 0
    assert zero.total_output_tokens == 0


def test_error_event():
    event = ErrorEvent("网络错误")
    assert event.kind == "error"
    assert event.message == "网络错误"


def test_stop_reason_values():
    values = [r.value for r in AgentStopReason]
    assert values == [
        "end_turn",
        "max_iterations",
        "user_cancelled",
        "timeout",
        "tool_error_limit",
        "internal_error",
    ]
    # 五种停止条件（INTERNAL_ERROR 为内部错误终止）
    assert len([v for v in values if v != "internal_error"]) == 5


def test_events_are_typed_union():
    from mewcode.engine.events import AgentEvent

    # 联合类型：所有具体事件都应可归入 AgentEvent
    sample: AgentEvent = TextChunkEvent("a")
    assert isinstance(sample, TextChunkEvent)

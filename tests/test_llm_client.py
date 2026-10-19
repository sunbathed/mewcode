import httpx
import pytest

from mewcode.config import ProviderConfig
from mewcode.llm_client import (
    OpenAICompatibleClient,
    StreamEnd,
    TextDelta,
    AuthenticationError,
    ToolUseDelta,
    ToolUseStart,
)


def _client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_stream_parses_sse():
    async def handler(request):
        body = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n' \
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n' \
            b'data: [DONE]\n\n'
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["Hello", " world"]
    assert isinstance(events[-1], StreamEnd)


@pytest.mark.asyncio
async def test_stream_parses_fragmented_tool_calls():
    """OpenAI 流式 tool_calls：id/name 先到，arguments 以多个碎片到达。"""
    body = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"ReadFile","arguments":""}}]}}]}\n\n' \
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":"}}]}}]}\n\n' \
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"a.py\\"}"}}]}}]}\n\n' \
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    client = OpenAICompatibleClient(
        ProviderConfig("openai", "demo", "http://test/v1", "secret"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
        ),
        tools=[{"type": "function", "function": {"name": "ReadFile", "description": "", "parameters": {}}}],
    )
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]

    starts = [e for e in events if isinstance(e, ToolUseStart)]
    deltas = [e for e in events if isinstance(e, ToolUseDelta)]
    assert len(starts) == 1
    assert starts[0].id == "call_1"
    assert starts[0].name == "ReadFile"
    assert starts[0].index == 0
    assert "".join(d.args_fragment for d in deltas) == '{"path":"a.py"}'
    assert events[-1] == StreamEnd("tool_calls")


@pytest.mark.asyncio
async def test_stream_multiple_tool_calls_and_text():
    raw = 'data: {"choices":[{"delta":{"content":"我来看看"}}]}\n\n' \
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c0","function":{"name":"ReadFile","arguments":"{\\"path\\":\\"a\\"}"}}]}}]}\n\n' \
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c1","function":{"name":"Grep","arguments":"{\\"pattern\\":\\"x\\"}"}}]}}]}\n\n' \
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    body = raw.encode("utf-8")
    client = _client(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    events = [event async for event in client.stream([])]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    starts = sorted([(e.index, e.id, e.name) for e in events if isinstance(e, ToolUseStart)])
    assert "".join(texts) == "我来看看"
    assert starts == [(0, "c0", "ReadFile"), (1, "c1", "Grep")]


@pytest.mark.asyncio
async def test_stream_tool_calls_single_fragment():
    body = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c0","function":{"name":"WriteFile","arguments":"{\\"path\\":\\"b\\",\\"content\\":\\"hi\\"}"}}]}}]}\n\n' \
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    client = _client(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    events = [event async for event in client.stream([])]
    starts = [e for e in events if isinstance(e, ToolUseStart)]
    assert starts[0].name == "WriteFile"
    assert events[-1] == StreamEnd("tool_calls")


@pytest.mark.asyncio
async def test_auth_error():
    async def handler(request):
        return httpx.Response(401)

    client = _client(handler)
    with pytest.raises(AuthenticationError):
        _ = [event async for event in client.stream([])]
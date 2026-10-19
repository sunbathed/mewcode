from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .config import ProviderConfig


class LLMError(RuntimeError):
    """Base class for model request failures."""


class AuthenticationError(LLMError):
    pass


class RateLimitError(LLMError):
    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    pass


class ServerError(LLMError):
    pass


class StreamInterruptedError(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseStart:
    """工具请求开始：请求标识与工具名先到。index 是流式碎片匹配键。"""

    index: int
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolUseDelta:
    """工具入参的 JSON 碎片，按 index 拼接。"""

    index: int
    args_fragment: str


@dataclass(frozen=True, slots=True)
class StreamEnd:
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None



class OpenAICompatibleClient:
    def __init__(
        self,
        config: ProviderConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
        tools: list[dict[str, Any]] | None = None,
        auto_tool_calls: bool = True,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self.timeout = timeout
        self.tools = tools
        self.auto_tool_calls = auto_tool_calls

    @property
    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[TextDelta | ToolUseStart | ToolUseDelta | StreamEnd]:
        """流式请求：区分文本增量与工具请求增量（tools 启用且 auto_tool_calls 时）。"""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.tools:
            payload["tools"] = self.tools
        if self.auto_tool_calls and self.tools:
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        client = self._http_client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._http_client is None
        response = None
        try:
            try:
                response = await client.send(client.build_request("POST", self.endpoint, headers=headers, json=payload), stream=True)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                raise NetworkError("网络请求失败，请检查网络连接或服务地址") from exc
            if response.status_code in (401, 403):
                await response.aread()
                raise AuthenticationError("模型服务认证失败，请检查 ZCode 配置")
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                await response.aread()
                raise RateLimitError("模型服务请求过于频繁", retry_after)
            if response.status_code >= 500:
                await response.aread()
                raise ServerError(f"模型服务暂时不可用（HTTP {response.status_code}）")
            if response.status_code >= 400:
                # 带上响应体：真实服务端报错（如 prompt_too_long）就藏在 body 里，
                # 第 8 章紧急压缩靠它识别上下文超限。
                body = (await response.aread()).decode("utf-8", errors="replace").strip()
                message = f"模型请求失败（HTTP {response.status_code}）"
                if body:
                    message += f"：{body[:2000]}"
                raise LLMError(message)

            try:
                last_usage: dict[str, Any] | None = None
                finish_reason: str | None = None
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        yield StreamEnd(finish_reason or "stop", usage=last_usage)
                        return
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        last_usage = event.get("usage")
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    for ev in self._delta_events(delta):
                        yield ev
                    finish = choices[0].get("finish_reason")
                    if finish:
                        finish_reason = finish
                yield StreamEnd(finish_reason or "stop", usage=last_usage)
            except (httpx.NetworkError, httpx.TimeoutException, UnicodeDecodeError) as exc:
                raise StreamInterruptedError("流式响应中断，已收到的内容仍会保存") from exc
        finally:
            if response is not None:
                await response.aclose()
            if owns_client:
                await client.aclose()

    def _delta_events(self, delta: dict[str, Any]) -> list[TextDelta | ToolUseStart | ToolUseDelta]:
        """把一条 delta 转成事件列表；完整提取同一 chunk 内的文本与全部工具增量。"""
        events: list[TextDelta | ToolUseStart | ToolUseDelta] = []
        text = delta.get("content")
        if isinstance(text, str) and text:
            events.append(TextDelta(text))
        tool_calls = delta.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                index = int(call.get("index", 0))
                mid = call.get("id")
                function = call.get("function") or {}
                fname = function.get("name")
                args = function.get("arguments")
                if mid is not None or fname is not None:
                    events.append(ToolUseStart(index=index, id=str(mid or ""), name=str(fname or "")))
                if args is not None:
                    events.append(ToolUseDelta(index=index, args_fragment=str(args)))
        return events

    def _delta_event(self, delta: dict[str, Any]) -> TextDelta | ToolUseStart | ToolUseDelta | None:
        """向后兼容单事件读取。"""
        evs = self._delta_events(delta)
        return evs[0] if evs else None
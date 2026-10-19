"""第 8 章：上下文管理器——编排第 1 层（大结果存盘）与第 2 层（全量摘要）。

每次 API 请求前按顺序执行两层：
  1. apply_budget / apply_agent_action —— 管单条工具结果的大小（预防性，几乎零成本）
  2. maybe_auto_compact —— 管对话历史的累积长度（治疗性，一次昂贵但强力的摘要调用）

所有压缩结果写回 Session 消息（外层对话历史），API 无状态、每轮重发完整历史，
所以「就地改写」是让压缩真正生效的唯一途径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import ProviderConfig
from ..llm_client import LLMError, OpenAICompatibleClient, TextDelta
from ..session import Message, Session, ToolUseBlock, utc_now
from .budget import (
    ContentReplacementState,
    ToolResultBudget,
    build_preview,
    persist_result,
)
from .compact import (
    AUTO_SAFETY_MARGIN,
    BOUNDARY_MESSAGE,
    COMPACT_MAX_RETRIES,
    MAX_COMPACT_FAILURES,
    SUMMARY_OUTPUT_RESERVE,
    SUMMARY_PROMPT,
    auto_compact_threshold,
    group_by_round,
    is_prompt_too_long_error,
    parse_summary,
)
from .tokens import estimate_messages_tokens, estimate_tokens, estimate_tools_tokens

DEFAULT_CONTEXT_WINDOW = 200_000


class CompactError(RuntimeError):
    """压缩失败（LLM 调用失败 / 摘要为空等）。"""


@dataclass(slots=True)
class ContextConfig:
    """上下文管理配置（理论篇的固定值设计：阈值由单轮操作特征决定，不由窗口总量决定）。"""

    context_window: int = DEFAULT_CONTEXT_WINDOW
    auto_compact_enabled: bool = True
    summary_output_reserve: int = SUMMARY_OUTPUT_RESERVE
    auto_safety_margin: int = AUTO_SAFETY_MARGIN
    result_max_chars: int = 50_000
    aggregate_budget_chars: int = 200_000
    preview_chars: int = 2_000
    keep_recent_messages: int = 5
    recover_recent_files: int = 5
    recover_recent_file_tokens: int = 5_000
    recover_total_tokens: int = 25_000

    def auto_threshold(self) -> int:
        return auto_compact_threshold(self.context_window, reserve=self.summary_output_reserve, margin=self.auto_safety_margin)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "ContextConfig":
        """支持用环境变量调低阈值（演示 / 测试自动压缩）与开关。"""
        env = environ if environ is not None else os.environ

        def _int(name: str, default: int) -> int:
            try:
                value = env.get(name, "").strip()
                return int(value) if value else default
            except ValueError:
                return default

        enabled = env.get("MEWCODE_AUTO_COMPACT", "1").strip().lower() not in {"0", "false", "off", "no"}
        return cls(
            context_window=_int("MEWCODE_CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW),
            auto_compact_enabled=enabled,
            summary_output_reserve=_int("MEWCODE_COMPACT_SUMMARY_RESERVE", SUMMARY_OUTPUT_RESERVE),
            auto_safety_margin=_int("MEWCODE_COMPACT_SAFETY_MARGIN", AUTO_SAFETY_MARGIN),
            result_max_chars=_int("MEWCODE_RESULT_MAX_CHARS", 50_000),
            aggregate_budget_chars=_int("MEWCODE_AGGREGATE_BUDGET_CHARS", 200_000),
            preview_chars=_int("MEWCODE_RESULT_PREVIEW_CHARS", 2_000),
        )


@dataclass(slots=True)
class CompactOutcome:
    """一次压缩尝试的结果，供 Agent / App 展示压缩前后的 Token 变化。"""

    performed: bool = False
    reason: str = ""  # auto / manual / force
    before_tokens: int = 0
    after_tokens: int = 0
    summary: str = ""


class ContextManager:
    """两层压缩的编排器；状态（决策冻结 / 熔断计数）挂在 Session 上随会话持久化。"""

    def __init__(
        self,
        provider_config: ProviderConfig,
        *,
        workdir: str,
        config: ContextConfig | None = None,
        state: ContentReplacementState | None = None,
        compact_client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.provider_config = provider_config
        self.workdir = workdir or os.getcwd()
        self.config = config or ContextConfig.from_env()
        self.state = state or ContentReplacementState()
        self._budget = ToolResultBudget(
            workdir=self.workdir,
            state=self.state,
            result_max_chars=self.config.result_max_chars,
            aggregate_budget_chars=self.config.aggregate_budget_chars,
            preview_chars=self.config.preview_chars,
        )
        # 熔断状态：连续失败计数与自动压缩开关
        self._auto_failures = 0
        self._config_auto_enabled = self.config.auto_compact_enabled
        self._auto_enabled = self._config_auto_enabled
        # 摘要专用客户端：不带任何工具定义（MewCode 走「不传 tools」的彻底路线）
        self._compact_client = compact_client
        self._client_factory = client_factory or self._default_client_factory

    def _default_client_factory(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(self.provider_config, tools=None)

    @property
    def auto_enabled(self) -> bool:
        return self._auto_enabled

    def reset(self) -> None:
        """切换会话时重置：决策冻结与熔断状态按会话隔离（tool_use_id 在同一会话内唯一）。"""
        self.state.seen_ids.clear()
        self.state.replacements.clear()
        self._auto_failures = 0
        self._auto_enabled = self._config_auto_enabled

    # ------------------------------------------------------------------
    # 第 1 层：大结果存盘 + 决策冻结
    # ------------------------------------------------------------------

    def apply_budget(self, messages: list[Message] | None = None) -> dict[str, Path]:
        """对（默认整条会话）消息执行预算检查：单结果阈值 + 聚合预算 + 决策冻结。

        幂等：已替换的重放缓存预览，已决定不替换的永不触碰。返回本轮真正
        新执行的替换 {tool_use_id: 存盘路径}。
        """
        return self._budget.apply(messages)

    def apply_agent_action(self, use_id: str, content: str) -> tuple[str, str]:
        """单个工具结果落地时的决策入口（理论篇「决策冻结」）。

        返回 (最终回填内容, 存盘路径或空字符串)。决定一旦做出就写进
        state，之后每一轮都原样重放，保证对话前缀逐字节稳定。
        """
        if not content:
            self.state.seen_ids.add(use_id)
            return content, ""
        if use_id in self.state.replacements:
            return self.state.replacements[use_id], ""
        if len(content) <= self.config.result_max_chars:
            self.state.seen_ids.add(use_id)
            return content, ""
        path = persist_result(self.workdir, use_id, content)
        preview = build_preview(use_id, content, path, self.workdir, self.config.preview_chars)
        self.state.replacements[use_id] = preview
        self.state.seen_ids.add(use_id)
        return preview, str(path)

    # ------------------------------------------------------------------
    # 第 2 层：全量摘要
    # ------------------------------------------------------------------

    def estimate_payload(self, system_prompt: str, session: Session, tools: list[dict[str, Any]] | None = None) -> int:
        """估算一次完整 API 请求的 token 数：system + 消息 + 工具定义。"""
        return (
            estimate_tokens(system_prompt)
            + estimate_messages_tokens(session.api_messages())
            + estimate_tools_tokens(tools or [])
        )

    async def maybe_auto_compact(
        self,
        session: Session,
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> CompactOutcome:
        """自动压缩入口：估算超过阈值才触发；连续失败熔断后停止自动触发。

        force=True 为紧急压缩（Agent 撞上 prompt_too_long 时的自救），
        不受熔断限制。
        """
        before = self.estimate_payload(system_prompt, session, tools)
        if not force and (not self._auto_enabled or before <= self.config.auto_threshold()):
            return CompactOutcome(performed=False, before_tokens=before, after_tokens=before)
        try:
            outcome = await self.compact(session, system_prompt=system_prompt, tools=tools)
            self._auto_failures = 0
            self._auto_enabled = self._config_auto_enabled
            outcome.reason = "force" if force else "auto"
            return outcome
        except Exception as exc:  # noqa: BLE001 - 压缩失败不应击穿 Agent Loop
            if not force:  # 紧急压缩不参与熔断计数
                self._auto_failures += 1
                if self._auto_failures >= MAX_COMPACT_FAILURES:
                    self._auto_enabled = False
            raise CompactError(f"自动压缩失败（第 {self._auto_failures} 次）{exc}") from exc

    async def compact(self, session: Session, *, system_prompt: str = "", tools: list[dict[str, Any]] | None = None) -> CompactOutcome:
        """执行全量摘要：调用 LLM 生成结构化摘要替换旧消息，保留最近消息并恢复关键上下文。"""
        before = self.estimate_payload(system_prompt, session, tools)
        outcome = CompactOutcome(performed=False, before_tokens=before, after_tokens=before)
        if not session.messages:
            return outcome

        messages = session.messages
        kept = self._keep_recent_messages(messages)
        cut = len(messages) - len(kept)
        if cut < 2:
            # 历史太短（连一次完整轮次都不够），压缩没有意义
            return outcome
        to_compact = messages[:cut]
        api_messages: list[dict[str, Any]] = []
        for message in to_compact:
            api_messages.extend(message.to_api())
        summary = await self._generate_summary_with_retry(api_messages)
        summary = parse_summary(summary)
        if not summary.strip():
            raise CompactError("摘要生成失败：模型返回了空内容")

        # 重组对话：摘要 + 恢复的最近文件 + 边界消息 + 最近消息原样保留
        new_messages: list[Message] = [
            Message("user", f"以下是你迄今为止所做工作的结构化摘要，请基于它继续工作：\n\n{summary}")
        ]
        recovery = self._build_file_recovery(session)
        if recovery:
            new_messages.append(Message("user", recovery))
        new_messages.append(Message("user", BOUNDARY_MESSAGE))
        new_messages.extend(kept)

        session.messages = new_messages
        session.updated_at = utc_now()
        after = self.estimate_payload(system_prompt, session, tools)
        outcome.performed = True
        outcome.after_tokens = after
        outcome.summary = summary
        return outcome

    async def _generate_summary_with_retry(self, api_messages: list[dict[str, Any]]) -> str:
        """调用 LLM 生成摘要；PTL 时按理论篇的重试逻辑：丢弃最旧的轮次组，最多重试 3 次，
        还不行就丢掉 20% 的消息组再试。"""
        if self._compact_client is None:
            self._compact_client = self._client_factory()
        client = self._compact_client
        messages = [{"role": "system", "content": SUMMARY_PROMPT}] + list(api_messages)

        for attempt in range(COMPACT_MAX_RETRIES + 1):
            try:
                return await self._collect_summary(client, messages)
            except LLMError as exc:
                if attempt >= COMPACT_MAX_RETRIES or not is_prompt_too_long_error(exc):
                    raise
                # 丢弃最旧的一组；只有一组时丢开头 20%
                body = messages[1:]
                groups = group_by_round(body)
                if len(groups) > 1:
                    body = [message for group in groups[1:] for message in group]
                else:
                    drop = max(1, len(body) // 5)
                    body = body[drop:]
                messages = [messages[0]] + body
        raise CompactError("摘要生成失败：多次重试仍未成功")

    @staticmethod
    async def _collect_summary(client: Any, messages: list[dict[str, Any]]) -> str:
        """流式收集摘要调用的文本输出（工具事件一律忽略——摘要请求不传 tools）。"""
        parts: list[str] = []
        async for event in client.stream(messages):
            if isinstance(event, TextDelta):
                parts.append(event.text)
        return "".join(parts)

    # ------------------------------------------------------------------
    # 压缩后恢复：最近文件 / 最近消息
    # ------------------------------------------------------------------

    def _keep_recent_messages(self, messages: list[Message]) -> list[Message]:
        """保留最近 N 条消息完整不动；窗口起点不能落在工具结果上（其 tool_use 必须一起保留）。"""
        start = max(0, len(messages) - self.config.keep_recent_messages)
        while start > 0 and messages[start].has_tool_result():
            start -= 1
        return messages[start:]

    def _recent_read_files(self, session: Session) -> list[str]:
        """从被压缩的消息里找出最近读取过的文件（去重，最多 recover_recent_files 个）。"""
        paths: list[str] = []
        for message in reversed(session.messages):
            for block in reversed(message.blocks or []):
                if isinstance(block, ToolUseBlock) and block.name == "ReadFile":
                    raw = str((block.input or {}).get("path", "")).strip()
                    if raw and raw not in paths:
                        paths.append(raw)
                        if len(paths) >= self.config.recover_recent_files:
                            return paths
        return paths

    def _build_file_recovery(self, session: Session) -> str:
        """恢复最近读取的文件内容，每个最多 recover_recent_file_tokens，合计不超预算。"""
        parts: list[str] = []
        total_tokens = 0
        for raw in self._recent_read_files(session):
            try:
                content = self._read_resolved_file(raw)
            except OSError:
                continue
            if not content:
                continue
            content = self._truncate_to_tokens(content, self.config.recover_recent_file_tokens)
            tokens = estimate_tokens(content)
            if parts and total_tokens + tokens > self.config.recover_total_tokens:
                break
            parts.append(f"[恢复文件] {raw} 的当前内容：\n{content}")
            total_tokens += tokens
        if not parts:
            return ""
        return "压缩前最近读取的文件内容已恢复如下（细节可能已过时，需要精确内容请用 ReadFile 重新读取）：\n\n" + "\n\n".join(parts)

    def _read_resolved_file(self, raw: str) -> str:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(self.workdir) / path
        content = path.read_text(encoding="utf-8")
        if len(content) > 40_000:
            content = content[:40_000]
        return content

    @staticmethod
    def _truncate_to_tokens(text: str, limit: int) -> str:
        if estimate_tokens(text) <= limit:
            return text
        ratio = limit / max(1, estimate_tokens(text))
        cut = max(1, int(len(text) * ratio))
        return text[:cut]


__all__ = [
    "ContextManager",
    "ContextConfig",
    "CompactOutcome",
    "CompactError",
    "DEFAULT_CONTEXT_WINDOW",
]
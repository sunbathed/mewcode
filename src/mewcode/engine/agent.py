from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from pathlib import Path

from ..config import ProviderConfig
from ..llm_client import LLMError, OpenAICompatibleClient, StreamEnd, StreamInterruptedError, TextDelta, ToolUseDelta, ToolUseStart
from ..session import Session, TextBlock, ToolUseBlock
from ..tools import ToolRegistry, ToolResult
from ..context import estimate_tokens
from ..context.compact import is_prompt_too_long_error
from ..context.manager import CompactError, CompactOutcome, ContextManager
from .builder import BuildOptions, build_system_prompt, detect_environment
from .collector import StreamCollector
from .events import (
    AgentEvent,
    AgentStopReason,
    ContextCompactedEvent,
    ErrorEvent,
    LoopCompleteEvent,
    PermissionRequestEvent,
    TextChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from .partition import execute_batches
from .plan_mode import (
    PLAN_MODE_INSTRUCTIONS,
    PlanModeState,
    build_plan_mode_reminder,
    build_plan_mode_tools,
    intercept_write,
)
from .prompt_pipeline import hook_notification_message, system_reminder_message
from ..permission import PermissionChecker, PermissionDecision, PermissionResponse
from ..hooks import HookContext, HookEngine, ToolRejectedError
from ..hooks.models import LifecycleEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Agent Loop 配置：最大轮数、整体超时、并发上限、连续工具失败容忍、系统提示词。"""

    max_iterations: int = 50
    timeout: float = 300.0
    max_concurrency: int | None = None
    max_tool_error_turns: int = 3
    system_prompt: str = ""
    # 第 9 章：跨会话记忆（MEWCODE.md 指令 + 自动记忆索引）注入 system prompt
    custom_instructions: str = ""
    memory_instructions: str = ""
    # 第 11 章：Skill 两阶段加载阶段一（轻量摘要列表，注入 system prompt）
    skill_catalog: str = ""


# 第 11 章：系统工具豁免 allowedTools 过滤（Skill 嵌套的关键）
SYSTEM_TOOL_NAMES = {"LoadSkill": True}


def _estimate_tokens(text: str) -> int:
    """粗略估算 token：中文按 1 字符 ≈ 1 token，ASCII 按 4 字符 ≈ 1 token。

    复用第八章上下文管理的共享估算实现。
    """
    return estimate_tokens(text)


def _default_system_prompt(workdir: str | None, os_name: str) -> str:
    """第四章理论篇定义的最小可用 System Prompt：角色设定 + 环境信息。"""
    lines = [
        "你是 MewCode，一个终端环境中的 AI 编程助手。",
        "你擅长阅读代码、编写代码和调试问题。",
        "你会先思考再行动，每一步都解释你的推理过程。",
        "",
        "# Environment",
        f"当前工作目录：{workdir or ''}",
        f"操作系统：{os_name}",
        f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return "\n".join(lines)


class Agent:
    """第四章核心：ReAct 范式的 Agent Loop 引擎。

    一轮 = 调 LLM → 收集流式响应 → 有工具调用则分批执行并回填 → 下一轮；
    没有工具调用（end_turn）则结束。对外只暴露事件流，UI 与引擎完全解耦。

    五种停止条件：
      1. END_TURN          模型主动收尾（无工具调用）
      2. MAX_ITERATIONS    达到迭代上限（安全网）
      3. USER_CANCELLED    外部 cancel 信号（每轮循环前检查）
      4. TIMEOUT           整体超时（每轮循环前检查）
      5. TOOL_ERROR_LIMIT  连续多轮工具失败，判定模型迷失
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        session: Session,
        registry: ToolRegistry,
        workdir: str | None = None,
        client: OpenAICompatibleClient | None = None,
        agent_config: AgentConfig | None = None,
        plan_state: PlanModeState | None = None,
        cancel_event: asyncio.Event | None = None,
        permission: PermissionChecker | None = None,
        context: ContextManager | None = None,
        hooks: HookEngine | None = None,
    ) -> None:
        self.provider_config = config
        self.session = session
        self.registry = registry
        self.workdir = workdir
        self.client = client or OpenAICompatibleClient(config, tools=registry.api_specs())
        self.agent_config = agent_config or AgentConfig()
        self.plan_state = plan_state or PlanModeState()
        self.cancel_event = cancel_event
        # 第六章：五层权限防御检查器（None 表示不启用权限系统）
        self.permission = permission
        # 第八章：上下文管理器（第 1 层存盘 + 第 2 层摘要；None 表示不启用）
        self.context = context
        # 第 12 章：Hook 引擎（生命周期事件自动化；None 表示不启用）
        self.hooks = hooks
        # 第 13 章：完整工具集引用（子 Agent 从它开始过滤，而非可能被
        # Skill allowedTools 缩减过的当前 registry）与实例级用量统计
        self._full_registry = registry
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens = 0
        self.total_tool_calls = 0
        # 第 13 章：父子链路追踪标识（AgentTool 设置子 Agent 的 _trace_id）
        self._trace_id = ""
        self._child_trace_ids: list[str] = []
        # 第 11 章：Skill 激活状态（env context 持久源）与工具过滤回调
        self.skill_state = None
        self.tool_filter = None  # callable(registry) -> ToolRegistry | None（allowedTools 过滤）
        # 第 15 章：每轮 Agent Loop 开头的注入回调（Team 邮箱消费）。
        # 签名 callable(turn) -> list[str]；返回的每段文本包装成
        # system reminder 注入请求首部，模型在开始本轮思考前读到
        self.pre_turn_inject: Callable[[int], list[str]] | None = None
        self._env = detect_environment(self.workdir, model=self.provider_config.model)
        self._cached_system_prompt: str | None = None

    # ------------------------------------------------------------------
    # System Prompt 与环境信息
    # ------------------------------------------------------------------

    def _os_name(self) -> str:
        return "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")

    def bind_skill_context(self, skill_state, tool_filter=None) -> None:
        """第 11 章：绑定 Skill 激活状态与 allowedTools 过滤回调（App/TUI 注入）。"""
        self.skill_state = skill_state
        self.tool_filter = tool_filter

    def _system_prompt(self) -> str:
        if self.agent_config.system_prompt:
            return self.agent_config.system_prompt
        if self._cached_system_prompt is None:
            # 第 9 章：指令文件（MEWCODE.md）与自动记忆索引作为条件注入模块进 system prompt
            # 第 11 章：Skill 两阶段加载阶段一——轻量摘要列表也在 system prompt 里
            self._cached_system_prompt = build_system_prompt(
                env=self._env,
                options=BuildOptions(
                    custom_instructions=self.agent_config.custom_instructions,
                    memory_instructions=self.agent_config.memory_instructions,
                    skill_instructions=self.agent_config.skill_catalog,
                ),
            )
        return self._cached_system_prompt

    def _tools_for_request(self) -> list[dict[str, Any]]:
        # 第 11 章：allowedTools 过滤（渐进式披露）+ 系统工具豁免 LoadSkill
        if self.tool_filter is not None:
            filtered_registry = self.tool_filter(self.registry)
            if filtered_registry is not None:
                registry = filtered_registry
            else:
                registry = self.registry
        else:
            registry = self.registry
        if self.plan_state.active:
            return build_plan_mode_tools(registry)
        return registry.api_specs()

    def _skill_env_reminders(self) -> list[dict[str, Any]]:
        """第 11 章：每轮重建 env context——已激活 Skill 的 SOP 钉到首部。

        普通用户消息会随对话往上推；env context 每轮重新构建，
        已激活的 SKILL.md 永远在最显眼的位置，不会被冲走。
        嵌套时所有激活的 Skill 同时在场（两份 SOP 同时生效）。
        """
        if self.skill_state is None:
            return []
        sop_texts = self.skill_state.sop_texts()
        if not sop_texts:
            return []
        joined = "\n\n".join(sop_texts)
        return [system_reminder_message(f"以下 Skill 已激活，请遵循其中的 SOP：\n\n{joined}")]

    def _messages_for_request(self, turn: int = 1) -> list[dict[str, Any]]:
        # 第 11 章：env context 每轮重建，已激活 Skill 的 SOP 钉在最前
        messages: list[dict[str, Any]] = self._skill_env_reminders()
        # 第 12 章：Hook 的 prompt 执行器注入——以 <hook-notification> 标签
        # 注入到请求首部，Agent 在开始本轮思考前读到，不污染对话历史
        if self.hooks is not None:
            for hook_message in self.hooks.get_prompt_messages():
                if hook_message:
                    messages.append(hook_notification_message(hook_message))
        messages.append({"role": "system", "content": self._system_prompt()})
        messages.extend(self.session.api_messages())
        if self.plan_state.active and self.plan_state.plan_file:
            plan_path = Path(self.plan_state.plan_file).expanduser()
            plan_exists = plan_path.exists()
            reminder = build_plan_mode_reminder(
                plan_file_path=self.plan_state.plan_file,
                plan_exists=plan_exists,
                iteration=turn,
            )
            messages.append(system_reminder_message(reminder))
        # 第 15 章：Team 邮箱消费——每轮循环开头把未读的队员消息注入成
        # system reminder，Agent 在开始本轮思考前读到（异步通信的读取点）
        if self.pre_turn_inject is not None:
            try:
                injected = self.pre_turn_inject(turn)
            except Exception:  # noqa: BLE001 - 注入失败不阻断 Agent Loop
                logger.exception("pre_turn_inject callback failed")
                injected = []
            for note in injected or []:
                if note:
                    messages.append(system_reminder_message(note))
        # 第七章：延迟工具提示。未发现的延迟工具（如 MCP 工具）不进完整 schema，
        # 只在 system-reminder 里列出名字，模型判断需要时调用 ToolSearch 拉取定义。
        deferred = self.registry.deferred_names()
        if deferred:
            hint = (
                "以下工具可通过 ToolSearch 加载它们的完整定义后再调用：\n"
                + "\n".join(f"- {name}" for name in deferred)
                + "\n使用格式：ToolSearch(query=\"select:工具名\") 或直接输入关键词搜索。"
            )
            messages.append(system_reminder_message(hint))
        return messages

    # ------------------------------------------------------------------
    # Agent Loop 主循环
    # ------------------------------------------------------------------

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        self.session.add_message("user", user_message)
        turn = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cached_tokens = 0
        tool_error_turns = 0
        # 第 8 章：本轮是否已做过紧急压缩（撞墙自救只允许一次，不无限重试）
        force_compacted = False
        start = asyncio.get_running_loop().time()

        while True:
            # 停止条件 3：用户取消
            if self.cancel_event is not None and self.cancel_event.is_set():
                yield LoopCompleteEvent(turn, AgentStopReason.USER_CANCELLED)
                return
            # 停止条件 4：整体超时
            if self.agent_config.timeout > 0 and asyncio.get_running_loop().time() - start > self.agent_config.timeout:
                yield LoopCompleteEvent(turn, AgentStopReason.TIMEOUT)
                return
            # 停止条件 2：迭代上限（安全网）
            if turn >= self.agent_config.max_iterations:
                yield LoopCompleteEvent(turn, AgentStopReason.MAX_ITERATIONS)
                return

            turn += 1
            collector = StreamCollector()
            turn_output_tokens = 0

            # 第 8 章：每轮请求前先执行两层压缩（能轻则轻，写回外层会话历史）。
            # 第 1 层：大结果存盘（幂等重放/决策冻结）；第 2 层：全量摘要（逼近窗口上限时）。
            if self.context is not None:
                self.context.apply_budget(self.session.messages)
                if not force_compacted:
                    outcome, compact_error = await self._try_auto_compact()
                    if compact_error:
                        # 第 12 章：error 事件——压缩失败（$ERROR 携带失败文本）
                        if self.hooks is not None:
                            await self.hooks.run_hooks(
                                LifecycleEvent.ERROR, HookContext(event_name=LifecycleEvent.ERROR, error=compact_error)
                            )
                        yield ErrorEvent(compact_error)
                    elif outcome is not None:
                        yield ContextCompactedEvent(
                            reason=outcome.reason,
                            before_tokens=outcome.before_tokens,
                            after_tokens=outcome.after_tokens,
                            summary_preview=outcome.summary[:300],
                        )

            # 一轮 LLM 调用：实时透传文本，同时积攒完整响应
            # Plan Mode 下每次请求的工具列表会变化，需要同步到 client（防御：非 OpenAI 兼容客户端无 tools 属性）
            if isinstance(self.client, OpenAICompatibleClient):
                self.client.tools = self._tools_for_request()
            # 第 12 章：pre_send 事件——消息发送给 LLM 之前（可注入 prompt 提示词）
            if self.hooks is not None:
                await self.hooks.run_hooks(
                    LifecycleEvent.PRE_SEND, HookContext(event_name=LifecycleEvent.PRE_SEND)
                )
            request_messages = self._messages_for_request(turn=turn)
            try:
                async for event in self.client.stream(request_messages):
                    collector.feed(event)
                    if isinstance(event, TextDelta):
                        turn_output_tokens += _estimate_tokens(event.text)
                        yield TextChunkEvent(event.text)
            except StreamInterruptedError as exc:
                # 第 12 章：error 事件——LLM 请求失败时触发（消息携带错误文本）
                if self.hooks is not None:
                    await self.hooks.run_hooks(
                        LifecycleEvent.ERROR, HookContext(event_name=LifecycleEvent.ERROR, error=str(exc))
                    )
                yield ErrorEvent(str(exc))
                yield LoopCompleteEvent(turn, AgentStopReason.INTERNAL_ERROR)
                return
            except LLMError as exc:
                # 第 8 章：紧急压缩（ForceCompact）——正常请求撞上 prompt_too_long
                # 说明自动压缩没拦住（估算偏差/异常大结果），压缩后重试原请求一次。
                if self.context is not None and not force_compacted and is_prompt_too_long_error(exc):
                    force_compacted = True
                    try:
                        outcome = await self.context.maybe_auto_compact(
                            self.session,
                            system_prompt=self._system_prompt(),
                            tools=self._tools_for_request(),
                            force=True,
                        )
                    except CompactError as compact_exc:
                        # 第 12 章：error 事件——紧急压缩失败（$ERROR 携带失败文本）
                        if self.hooks is not None:
                            await self.hooks.run_hooks(
                                LifecycleEvent.ERROR,
                                HookContext(event_name=LifecycleEvent.ERROR, error=str(compact_exc)),
                            )
                        yield ErrorEvent(str(compact_exc))
                        yield LoopCompleteEvent(turn, AgentStopReason.INTERNAL_ERROR)
                        return
                    if outcome is not None and outcome.performed:
                        yield ContextCompactedEvent(
                            reason="force",
                            before_tokens=outcome.before_tokens,
                            after_tokens=outcome.after_tokens,
                            summary_preview=outcome.summary[:300],
                        )
                    turn -= 1  # 本轮请求未成功，回退轮次后重试
                    continue
                # 第 12 章：error 事件——LLM 请求失败时触发（消息携带错误文本）
                if self.hooks is not None:
                    await self.hooks.run_hooks(
                        LifecycleEvent.ERROR, HookContext(event_name=LifecycleEvent.ERROR, error=str(exc))
                    )
                yield ErrorEvent(str(exc))
                yield LoopCompleteEvent(turn, AgentStopReason.INTERNAL_ERROR)
                return
            # 第 12 章：post_receive 事件——收到 LLM 响应之后
            if self.hooks is not None:
                await self.hooks.run_hooks(
                    LifecycleEvent.POST_RECEIVE, HookContext(event_name=LifecycleEvent.POST_RECEIVE)
                )

            collected = collector.result()
            turn_input_tokens = _estimate_tokens(repr(request_messages))
            cached_tokens = 0
            if collected.usage:
                turn_input_tokens = (
                    collected.usage.get("prompt_tokens")
                    or collected.usage.get("input_tokens")
                    or turn_input_tokens
                )
                if collected.usage.get("completion_tokens"):
                    turn_output_tokens = collected.usage["completion_tokens"]
                elif collected.usage.get("output_tokens"):
                    turn_output_tokens = collected.usage["output_tokens"]
                cached_tokens = (
                    collected.usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                    or collected.usage.get("cache_read_input_tokens", 0)
                )

            total_input_tokens += turn_input_tokens
            total_output_tokens += turn_output_tokens
            total_cached_tokens += cached_tokens
            # 第 13 章：同步实例级统计（子 Agent 用量经此上报给 TaskManager）
            self.total_input_tokens = total_input_tokens
            self.total_output_tokens = total_output_tokens
            self.total_cached_tokens = total_cached_tokens
            yield UsageEvent(
                turn,
                turn_input_tokens,
                turn_output_tokens,
                total_input_tokens,
                total_output_tokens,
                cached_tokens=cached_tokens,
                total_cached_tokens=total_cached_tokens,
            )
            yield TurnCompleteEvent(turn, collected.stop_reason)

            # 停止条件 1：模型主动结束（无工具调用）
            if not collected.tool_uses:
                self._persist_assistant_turn(collected.text, [])
                yield LoopCompleteEvent(turn, AgentStopReason.END_TURN)
                return

            # 有工具调用：persist 本轮 assistant 消息（text + tool_use 同一条，不可拆开）
            self._persist_assistant_turn(collected.text, list(collected.tool_uses))

            # 发出工具请求事件
            for use in collected.tool_uses:
                # 第 13 章：实例级工具调用计数（子 Agent 追踪用）
                self.total_tool_calls += 1
                yield ToolUseEvent(use.id, use.name, use.input)

            # 第 6 章：权限检查嵌入 Agent Loop。
            # 逐个工具先走「规划模式拦截位」，再走五层权限决策链：
            # ALLOW -> 进入可执行列表；DENY -> 拦截结果；ASK -> 触发第 5 层 HITL 阻塞等待。
            plan_reasons = [intercept_write(self.plan_state, use) for use in collected.tool_uses]
            plan_blocked = any(reason is not None for reason in plan_reasons)

            executable: list[ToolUseBlock] = []
            blocked: dict[str, ToolResult] = {}  # use.id -> 直接拦截的工具结果

            # 第 12 章：pre_tool_use Hook 先置安全拦截——逐工具在权限链之前检查。
            # 命中 reject 的调用直接生成拦截结果（reason 即 Hook 输出），
            # 与权限拦截走同一回填路径，不进执行列表。
            if self.hooks is not None:
                for use in collected.tool_uses:
                    hook_ctx = HookContext(
                        event_name=LifecycleEvent.PRE_TOOL_USE,
                        tool_name=use.name,
                        tool_args=use.input,
                        file_path=self._hook_file_path(use),
                    )
                    rejection = await self.hooks.run_pre_tool_hooks(hook_ctx)
                    if rejection is not None:
                        self._log_rejection(rejection)
                        blocked[use.id] = ToolResult(is_error=True, error=rejection.reason)

            for use, reason in zip(collected.tool_uses, plan_reasons):
                if use.id in blocked:
                    # Hook 拦截优先：跳过规划/权限链路
                    continue
                if reason is not None:
                    # 规划模式拦截：写类工具（除 plan 文件外）直接拒绝
                    blocked[use.id] = ToolResult(is_error=True, error=reason)
                    continue
                if self.plan_state.active:
                    # 规划模式拦截放行的调用 = 写入 plan 文件（唯一允许写入），权限系统始终放行
                    executable.append(use)
                    continue
                if self.permission is None:
                    executable.append(use)
                    continue
                decision = self.permission.check(use.name, use.input)
                if decision.decision == PermissionDecision.DENY:
                    # 第 1-3 层硬拦截：黑名单 / 沙箱 / deny 规则（不可覆盖）
                    blocked[use.id] = ToolResult(is_error=True, error=decision.reason)
                    continue
                if decision.decision == PermissionDecision.ASK:
                    # 第 5 层 HITL：发出权限请求事件并阻塞等待 UI 层回复
                    future = asyncio.get_running_loop().create_future()
                    yield PermissionRequestEvent(
                        request_id=use.id,
                        tool_name=use.name,
                        description=self._permission_description(use),
                        response_future=future,
                    )
                    response = await future
                    if response == PermissionResponse.ALLOW_ALWAYS:
                        # 「始终允许」：自动追加 allow 规则到本地配置，形成权限学习循环
                        self.permission.remember_always(use.name, use.input)
                    if response in (PermissionResponse.ALLOW, PermissionResponse.ALLOW_ALWAYS):
                        executable.append(use)
                    else:
                        blocked[use.id] = ToolResult(is_error=True, error="操作被用户拒绝：HITL 确认未通过。")
                    continue
                # 第 4 层模式判断放行（default/acceptEdits 的只读与编辑操作）
                executable.append(use)

            # 执行可执行批次（读类并发、写类串行，保持原有分批调度）
            if executable:
                results = await execute_batches(
                    self.registry, executable, max_concurrency=self.agent_config.max_concurrency
                )
            else:
                results = []
            result_map = {use.id: result for use, result in zip(executable, results)}
            result_map.update(blocked)
            results = [result_map[use.id] for use in collected.tool_uses]
            # 第 8 章：先落地再发事件——落地时经过决策冻结，超大结果在进入会话前
            # 已被替换为「预览 + 存盘路径」，UI 事件据此展示存盘提示。
            persisted = self._persist_tool_results(list(collected.tool_uses), results)

            for use, result in zip(collected.tool_uses, results):
                yield ToolResultEvent(
                    use.id,
                    result.output,
                    result.is_error,
                    result.error,
                    result.duration,
                    persisted=use.id in persisted,
                    persisted_path=persisted.get(use.id, ""),
                )
                # 第 12 章：post_tool_use 事件——工具执行完成之后
                # 对被 Hook 拦截的调用也回填其 Hook 输出（模型可据此调整策略）
                if self.hooks is not None:
                    await self.hooks.run_hooks(
                        LifecycleEvent.POST_TOOL_USE,
                        HookContext(
                            event_name=LifecycleEvent.POST_TOOL_USE,
                            tool_name=use.name,
                            tool_args=use.input,
                            file_path=self._hook_file_path(use),
                            error=result.error if result.is_error else "",
                        ),
                    )

            # 停止条件 5：连续多轮工具失败（模型可能已迷失）
            # 权限拒绝（DENY/HITL 拒绝）不算执行失败——模型看到
            # "Permission denied" 后可以调整策略换一种方式完成任务。
            if plan_blocked:
                tool_error_turns += 1
            else:
                executed_results = [result_map[use.id] for use in executable]
                executed_failed = any(r.is_error for r in executed_results)
                tool_error_turns = tool_error_turns + 1 if executed_failed else 0

            if tool_error_turns >= self.agent_config.max_tool_error_turns:
                yield ErrorEvent(f"连续 {tool_error_turns} 轮工具调用失败，Agent 已自动停止。")
                yield LoopCompleteEvent(turn, AgentStopReason.TOOL_ERROR_LIMIT)
                return

    # ------------------------------------------------------------------
    # 第 13 章：RunToCompletion——子 Agent 的非交互式执行循环
    # ------------------------------------------------------------------

    async def run_to_completion(self, task: str, *, fork_conversation: Session | None = None) -> str:
        """子 Agent 执行模式：任务直接注入，跑到底，返回最后一段文本。

        与 run() 的区别：
          - 不等待用户输入，任务从参数传入
          - LLM 不再调用工具（end_turn）即视为任务完成，返回最后文本
          - 工具调用权限由 AgentDef.permissionMode 决定（dontAsk 自动批准）
          - 消费事件流但不对外广播，文本结果就地累积

        fork_conversation 非空表示 Fork 路径：对话历史已由
        build_forked_messages 构建（含 Boilerplate 与任务指令），
        不再追加任务消息。
        """
        if fork_conversation is not None:
            # Fork 路径：直接使用已构建的会话（含任务指令），run 不再注入任务
            self.session = fork_conversation
            prompt = ""
        else:
            # 任务作为 user 消息由 run() 统一写入会话（这里不重复添加）
            prompt = task

        last_text = ""
        async for event in self.run(prompt):
            if isinstance(event, TextChunkEvent):
                last_text += event.text
            elif isinstance(event, ErrorEvent):
                last_text += f"\n（错误：{event.message}）"
            # ToolUseEvent / ToolResultEvent 等事件只驱动循环内部状态，
            # 子 Agent 的中间过程不需要对外可见
        return last_text.strip()

    # ------------------------------------------------------------------
    # 消息持久化
    # ------------------------------------------------------------------

    def _persist_assistant_turn(self, text: str, uses: list[ToolUseBlock]) -> None:
        """assistant 消息：text 与 tool_use 必须作为同一条消息整体追加；空响应不落库。"""
        blocks: list[Any] = []
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(uses)
        if not blocks:
            return
        self.session.add_message("assistant", text, blocks=blocks)

    def _persist_tool_results(self, uses: list[ToolUseBlock], results: list[ToolResult]) -> dict[str, str]:
        """工具结果以 user 角色回填，tool_use_id 与请求一一对应。

        第 8 章：落地前先过决策冻结——超大结果存盘并替换为预览字符串。
        返回 {tool_use_id: 存盘路径}，供 ToolResultEvent 提示 UI。
        """
        pairs: list[tuple[str, str]] = []
        persisted: dict[str, str] = {}
        for use, result in zip(uses, results):
            content = result.to_text()
            if self.context is not None and not result.is_error:
                content, path = self.context.apply_agent_action(use.id, content)
                if path:
                    persisted[use.id] = path
            pairs.append((use.id, content))
        if pairs:
            self.session.add_tool_results(pairs)
        return persisted

    async def _try_auto_compact(self) -> tuple[CompactOutcome | None, str | None]:
        """自动压缩的容错包装：失败只发错误事件，不中断 Agent 循环（熔断在管理器内计数）。"""
        if self.context is None:
            return None, None
        try:
            outcome = await self.context.maybe_auto_compact(
                self.session,
                system_prompt=self._system_prompt(),
                tools=self._tools_for_request(),
            )
            return outcome, None
        except CompactError as exc:
            return None, str(exc)

    @staticmethod
    def _permission_description(use: ToolUseBlock) -> str:
        """构建 HITL 确认对话框展示的工具调用描述。"""
        from ..permission import build_permission_description

        return build_permission_description(use.name, use.input)

    # ------------------------------------------------------------------
    # 第 12 章：Hook 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _hook_file_path(use: ToolUseBlock) -> str:
        """从工具参数里提取文件路径（WriteFile/EditFile/ReadFile 等的 path 参数）。"""
        file_path = use.input.get("file_path") or use.input.get("path")
        return str(file_path) if file_path else ""

    @staticmethod
    def _log_rejection(rejection: ToolRejectedError) -> None:
        """Hook 拦截按警告记录（辅助调试：哪条规则拦了哪个调用）。"""
        logger.warning(
            "Hook '%s' rejected tool '%s': %s", rejection.hook_id, rejection.tool, rejection.reason
        )


__all__ = ["Agent", "AgentConfig", "_default_system_prompt", "_estimate_tokens"]

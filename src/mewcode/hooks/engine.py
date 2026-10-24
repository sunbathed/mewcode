"""第 12 章：HookEngine——把 Hook 系统的所有部件串起来。

两条执行路径：
  - run_hooks(event, ctx)：普通事件（通知型），执行完即返回，不影响主流程
  - run_pre_tool_hooks(ctx)：pre_tool_use 专用，检查 reject 标记，
    一旦命中立即返回 ToolRejectedError 阻断工具执行

设计原则：
  - Hook 执行出错只记日志，绝不中断 Agent 主流程（辅助机制隔离）
  - prompt 执行器的结果攒在 _prompt_messages 里，由 Agent Loop
    每轮通过 get_prompt_messages 一次性取走（取走即清空）
  - async 标记的 Hook 用 asyncio.ensure_future 后台执行，
    任务句柄保存在引擎上避免被 GC 丢弃
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .executors import execute_action
from .models import Hook, HookContext, LifecycleEvent, ToolRejectedError

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .conditions import ConditionGroup

logger = logging.getLogger(__name__)


class HookEngine:
    """Hook 引擎：加载配置、匹配事件、执行动作、收集 prompt 消息。"""

    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = list(hooks or [])
        self._prompt_messages: list[str] = []
        self._background_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        """三层过滤：事件名 → once 标记 → 条件求值。"""
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched

    # ------------------------------------------------------------------
    # 普通事件路径
    # ------------------------------------------------------------------

    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        """触发一个非拦截事件：匹配到的 Hook 按声明顺序执行。

        async 标记的 Hook 后台执行不等结果；任一 Hook 出错只记日志。
        """
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                task = asyncio.ensure_future(self._run_single(hook, ctx))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                await self._run_single(hook, ctx)

    # ------------------------------------------------------------------
    # pre_tool_use 拦截路径
    # ------------------------------------------------------------------

    async def run_pre_tool_hooks(self, ctx: HookContext) -> ToolRejectedError | None:
        """触发 pre_tool_use：先执行动作，再检查 reject 标记。

        注意 reject 判断放在 action 执行之后——拒绝 Hook 的输出文本
        会作为拒绝原因返回给 LLM，Agent 据此调整策略。
        """
        matched = self.find_matching_hooks(LifecycleEvent.PRE_TOOL_USE, ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
            except Exception as exc:  # noqa: BLE001 - Hook 故障不中断主流程
                logger.warning("Hook '%s' execution error: %s", hook.id, exc)
                continue
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            if hook.reject:
                return ToolRejectedError(
                    tool=ctx.tool_name,
                    reason=result.output or f"工具调用被 Hook '{hook.id}' 拒绝",
                    hook_id=hook.id,
                )
        return None

    # ------------------------------------------------------------------
    # prompt 消息收集
    # ------------------------------------------------------------------

    def get_prompt_messages(self) -> list[str]:
        """取走攒下的 prompt 注入消息（一次性消费，取走即清空）。"""
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        """执行单个 Hook：结果不中断主流程，prompt 消息单独收集。"""
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
        except Exception as exc:  # noqa: BLE001 - 辅助机制故障只记日志
            logger.warning("Hook '%s' execution error: %s", hook.id, exc)

    def shutdown(self) -> None:
        """取消仍在后台运行的任务（退出时避免事件循环泄漏）。"""
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()


def _build_hook_context(
    event: str,
    *,
    tool_name: str = "",
    tool_args: dict[str, Any] | None = None,
    file_path: str = "",
    message: str = "",
    error: str = "",
) -> HookContext:
    """构造 HookContext 的便捷函数。"""
    return HookContext(
        event_name=event,
        tool_name=tool_name,
        tool_args=dict(tool_args or {}),
        file_path=file_path,
        message=message,
        error=error,
    )


__all__ = [
    "HookEngine",
    "HookContext",
    "_build_hook_context",
]
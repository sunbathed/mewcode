"""Skill 执行器：inline / fork 两种执行模式。

inline（默认）：Skill 的 prompt 注入当前对话，和正常用户消息一样走 Agent
Loop，能看到之前的上下文，执行结果留在对话历史里。

fork：Skill 在独立上下文中执行，不影响也不受当前对话影响。按 skill.context
决定带多少主对话上下文：full 带完整摘要、recent 带最近 5 条、none 完全隔离。
执行完把结果作为一条 assistant 消息塞回主对话。

allowedTools：非空则过滤工具集（渐进式披露 + 最小权限），空则不限制。
fail-fast：执行前先检查 allowedTools 里的工具是否存在，缺失立即报错。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..session import Session, TextBlock
from .types import (
    CONTEXT_FULL,
    CONTEXT_NONE,
    CONTEXT_RECENT,
    FORK_RECENT_MESSAGES,
    Skill,
)
from .loader import SkillLoader, SkillNotFoundError


@dataclass(slots=True)
class SkillResult:
    """一次 Skill 执行的返回（fork 模式的结果文本 / inline 的完成提示）。"""

    text: str
    mode: str = "inline"


class ExecutionCancelled(RuntimeError):
    pass


class SkillExecutor:
    """Skill 执行器：解析加载 → allowedTools 过滤 → inline/fork 分派。"""

    def __init__(
        self,
        loader: SkillLoader,
        *,
        agent_factory: Any = None,
        fork_runner: Any = None,
    ) -> None:
        """agent_factory：inline 模式构建 Agent 的回调（由 App/TUI 注入）。

        fork_runner：fork 模式下运行子 Agent 的回调，签名
        async (skill, prompt, context_messages) -> str（返回结果文本）。
        """
        self.loader = loader
        self.agent_factory = agent_factory
        self.fork_runner = fork_runner

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def execute(self, name: str, args: str = "") -> SkillResult:
        """按名字执行一个 Skill（热加载：每次重新读源文件）。"""
        try:
            skill = self.loader.get(name)
        except SkillNotFoundError as exc:
            return SkillResult(text=f"错误：{exc}", mode="error")

        # fail-fast 依赖检查：allowedTools 里的工具必须都存在
        missing = self._verify_allowed_tools(skill)
        if missing:
            return SkillResult(
                text=(
                    f"错误：Skill '{skill.name}' 声明了不存在的工具：{', '.join(missing)}。"
                    "请修复 Skill 定义后重试 /skill reload。"
                ),
                mode="error",
            )

        prompt = skill.render(args)
        if skill.mode == "fork":
            return await self._execute_fork(skill, prompt)
        return await self._execute_inline(skill, prompt, args)

    # ------------------------------------------------------------------
    # fail-fast
    # ------------------------------------------------------------------

    def _verify_allowed_tools(self, skill: Skill) -> list[str]:
        if self.agent_factory is None or not skill.allowed_tools:
            return []
        # 专属工具（tool.json 声明的）由 Skill 自行注册，不参与 fail-fast
        own_tool_names = {t.name for t in skill.tools}
        missing = []
        for tool_name in skill.allowed_tools:
            if tool_name in own_tool_names:
                continue
            if not self._has_tool(tool_name):
                missing.append(tool_name)
        return missing

    def _has_tool(self, name: str) -> bool:
        try:
            self.agent_factory.registry.get(name)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # inline：注入当前对话，共享上下文
    # ------------------------------------------------------------------

    async def _execute_inline(self, skill: Skill, prompt: str, args: str) -> SkillResult:
        if self.agent_factory is None:
            return SkillResult(text="inline 执行需要 agent_factory", mode="error")
        # 把渲染后的 SOP 作为用户消息注入当前会话，走正常 Agent Loop
        await self.agent_factory.run_skill_inline(skill, prompt, args)
        return SkillResult(text="", mode="inline")

    # ------------------------------------------------------------------
    # fork：独立上下文执行，结果回流主对话
    # ------------------------------------------------------------------

    async def _execute_fork(self, skill: Skill, prompt: str) -> SkillResult:
        if self.fork_runner is None:
            return SkillResult(text="fork 执行需要 fork_runner", mode="error")
        context_messages = self._build_fork_context(skill)
        result = await self.fork_runner(skill, prompt, context_messages)
        return SkillResult(text=result, mode="fork")

    def _build_fork_context(self, skill: Skill) -> list[Any]:
        """按 skill.context 决定带多少主对话上下文进 fork 会话。"""
        if self.agent_factory is None:
            return []
        session = self._get_session()
        if session is None:
            return []
        if skill.context == CONTEXT_NONE:
            return []
        if skill.context == CONTEXT_RECENT:
            return session.messages[-FORK_RECENT_MESSAGES:]
        # full：完整对话的摘要（简化：直接带全部消息文本，由 fork_runner 处理）
        return list(session.messages)

    def _get_session(self) -> Any:
        """从宿主（App/TUI）获取当前会话。"""
        factory = self.agent_factory
        session = getattr(factory, "session", None)
        if session is not None:
            return session
        app = getattr(factory, "_app", None)
        return getattr(app, "session", None) if app is not None else None


__all__ = ["SkillExecutor", "SkillResult", "ExecutionCancelled"]
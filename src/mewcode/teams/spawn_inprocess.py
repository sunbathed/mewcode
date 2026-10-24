"""第 15 章：In-process 后端——asyncio.Task 方案。

队员和 Lead 跑在同一个进程里，队员的执行被包装成一个 asyncio.Task。
更轻量：启动快、不依赖外部工具；限制是生命周期绑定在 Lead 身上
（Lead 退出，所有 in-process 队员一起退出）。

asyncio.Task 的取消语义很好：task.cancel() 会在下一个 await 点抛出
CancelledError，Agent 的主循环捕获后清理资源，取消信号自动沿着
await 链传播。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..engine.agent import Agent
    from ..session import Session


class InProcessTeammateHandle:
    """一个 in-process 队员的运行句柄：检查完成 / 取消执行。"""

    def __init__(self, agent: "Agent", task: "asyncio.Task[str]", name: str) -> None:
        self.agent = agent
        self.task = task
        self.name = name

    @property
    def done(self) -> bool:
        return self.task.done()

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()

    def result(self) -> str:
        """已完成后取结果（未完成会抛 InvalidStateError，调用方先看 done）。"""
        return self.task.result()


def spawn_inprocess_teammate(
    agent: "Agent",
    prompt: str,
    name: str,
    conversation: "Session | None" = None,
) -> InProcessTeammateHandle:
    """把队员的执行包装成 asyncio.Task 立即返回。

    conversation 非空表示续写路径：用已有的对话历史继续执行，
    否则用 prompt 从头开始。asyncio.create_task 的 name 参数方便
    调试时在 task 列表里找到它。
    """

    async def _run() -> str:
        if conversation is not None:
            return await agent.run_to_completion("", fork_conversation=conversation)
        return await agent.run_to_completion(prompt)

    task = asyncio.create_task(_run(), name=f"teammate-{name}")
    return InProcessTeammateHandle(agent=agent, task=task, name=name)


__all__ = ["InProcessTeammateHandle", "spawn_inprocess_teammate"]

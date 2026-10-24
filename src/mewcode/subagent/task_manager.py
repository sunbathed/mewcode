"""第 13 章：后台任务管理器。

管理所有后台 SubAgent 任务的生命周期：启动、终态、结果回传。
TaskManager 采用「推拉结合」的通知模式：
  - push：任务完成时把 task_id 放入 _notify_queue
  - pull：主 Agent Loop 每轮调用 poll_completed 拉取完成任务

launch 是核心入口：生成唯一 ID、创建 BackgroundTask、后台协程里
跑 RunToCompletion。异常保护：即使子 Agent 崩溃，状态变成 failed，
不会影响主程序。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..engine.agent import Agent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProgressInfo:
    """后台任务的进度信息：工具调用次数与 Token 消耗。"""

    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_active: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class BackgroundTask:
    """一个后台任务的状态容器。"""

    id: str
    name: str
    agent: Agent
    task: str
    status: str = "running"  # running / completed / failed / cancelled
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Callable[[], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)


class TaskManager:
    """后台任务生命周期管理：launch / poll / cancel / list。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._async_tasks: dict[str, asyncio.Task] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def launch(
        self,
        agent: Agent,
        task: str,
        *,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        """启动一个后台任务，返回 task_id。

        fork_conversation 非空表示 Fork 路径——对话历史已经构建好
        （含 Boilerplate 与任务指令），run_to_completion 不再追加任务。
        """
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(id=task_id, name=name or task_id, agent=agent, task=task)
        self._tasks[task_id] = bg
        async_task = asyncio.create_task(self._run_background(task_id, fork_conversation))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        return task_id

    def launch_fn(
        self,
        fn: Callable[[], Any],
        *,
        name: str = "",
        agent: Agent | None = None,
    ) -> str:
        """启动一个自定义后台协程（第 14 章：Worktree 隔离任务）。

        Worktree 隔离的完整生命周期（创建 → 运行 → 自动清理）必须在
        同一个协程里完成，run_to_completion 形式的 launch 覆盖不了；
        fn 是无参异步函数，结果文本与状态管理同普通后台任务一致。
        """
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(id=task_id, name=name or task_id, agent=agent, task="")
        self._tasks[task_id] = bg
        async_task = asyncio.create_task(self._run_fn_background(task_id, fn))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        return task_id

    # ------------------------------------------------------------------
    # 运行与通知
    # ------------------------------------------------------------------

    async def _run_background(self, task_id: str, fork_conversation: Any = None) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return
        try:
            if fork_conversation is not None:
                result = await bg.agent.run_to_completion(
                    "", fork_conversation=fork_conversation
                )
            else:
                result = await bg.agent.run_to_completion(bg.task)
            bg.result = result
            bg.status = "completed"
        except asyncio.CancelledError:
            bg.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - 后台任务崩溃不影响主程序
            bg.status = "failed"
            bg.result = f"Error: {exc}"
            logger.exception("后台任务 %s 执行失败", task_id)
        finally:
            bg.end_time = time.monotonic()
            bg.progress.input_tokens = getattr(bg.agent, "total_input_tokens", 0) or 0
            bg.progress.output_tokens = getattr(bg.agent, "total_output_tokens", 0) or 0
            # 任务完成后推送 task_id 到通知队列（Agent Loop 每轮 poll）
            await self._notify_queue.put(task_id)

    async def _run_fn_background(self, task_id: str, fn: Callable[[], Any]) -> None:
        """运行自定义后台协程：状态与通知机制同 _run_background。"""
        bg = self._tasks.get(task_id)
        if bg is None:
            return
        try:
            bg.result = await fn()
            bg.status = "completed"
        except asyncio.CancelledError:
            bg.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - 后台任务崩溃不影响主程序
            bg.status = "failed"
            bg.result = f"Error: {exc}"
            logger.exception("后台任务 %s 执行失败", task_id)
        finally:
            bg.end_time = time.monotonic()
            if bg.agent is not None:
                bg.progress.input_tokens = getattr(bg.agent, "total_input_tokens", 0) or 0
                bg.progress.output_tokens = getattr(bg.agent, "total_output_tokens", 0) or 0
            await self._notify_queue.put(task_id)

    def poll_completed(self) -> list[BackgroundTask]:
        """拉取所有已完成的任务（push/pull 结合的通知模式）。"""
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed

    # ------------------------------------------------------------------
    # 查询与取消
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[BackgroundTask]:
        """全部后台任务（运行中 + 已完成）。"""
        return list(self._tasks.values())

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """取消后台任务；返回是否找到。"""
        bg = self._tasks.get(task_id)
        if bg is None or bg.cancel is None:
            return False
        try:
            bg.cancel()
        except Exception:  # noqa: BLE001 - 取消失败不抛
            pass
        return True

    def shutdown(self) -> None:
        """退出时取消所有仍在运行的后台任务（避免事件循环泄漏）。"""
        for task_id in list(self._tasks):
            bg = self._tasks.get(task_id)
            if bg is not None and bg.status == "running":
                self.cancel(task_id)

    def clear_completed(self) -> None:
        """清理已结束任务的句柄（避免长会话内存增长）。"""
        for task_id in list(self._tasks):
            bg = self._tasks[task_id]
            if bg.status in ("completed", "failed", "cancelled"):
                self._tasks.pop(task_id, None)
                self._async_tasks.pop(task_id, None)


__all__ = [
    "BackgroundTask",
    "ProgressInfo",
    "TaskManager",
]
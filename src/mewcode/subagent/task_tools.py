"""第 13 章：后台任务查询工具（TaskList / TaskGet）。

后台任务没有给用户的 slash command 之外还有一套工具入口：主 Agent
想知道某个后台任务跑得怎么样，直接问它就行——它自己会用 TaskList /
TaskGet 去查，再用自然语言告诉用户。这让「查后台任务」变成自然的
对话交互，而不是让用户学一堆命令。
"""

from __future__ import annotations

from typing import Any

from ..tools.base import BaseTool
from .task_manager import TaskManager


def _format_task(task) -> str:
    """格式化一个后台任务的摘要行。"""
    elapsed = task.end_time - task.start_time if task.end_time is not None else 0.0
    return (
        f"[{task.id}] {task.name} — {task.status}"
        f"（{elapsed:.1f}s，{task.progress.input_tokens + task.progress.output_tokens} token）"
    )


class TaskListTool(BaseTool):
    """列出所有后台任务（运行中 + 已完成）。"""

    name = "TaskList"
    description = "列出当前所有后台子 Agent 任务及其状态（运行中 / 已完成 / 失败 / 已取消）。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }
    safety = "read"

    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    async def _run(self, args: dict[str, Any]) -> str:
        tasks = self._task_manager.list_tasks()
        if not tasks:
            return "暂无后台任务。"
        lines = [f"后台任务（共 {len(tasks)} 个）："]
        for task in tasks:
            lines.append(f"- {_format_task(task)}")
        return "\n".join(lines)


class TaskGetTool(BaseTool):
    """获取某个后台任务的状态或结果。"""

    name = "TaskGet"
    description = "获取指定后台子 Agent 任务的状态与结果（按 task_id 查询）。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "后台任务的 ID（Agent 工具启动后台任务时返回的 Task ID）。",
            }
        },
        "required": ["task_id"],
    }
    safety = "read"

    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    async def _run(self, args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        task = self._task_manager.get(task_id)
        if task is None:
            return f"Task {task_id} not found."
        elapsed = task.end_time - task.start_time if task.end_time is not None else "进行中"
        result = task.result if task.result else "（尚无结果）"
        return (
            f"Task: {task.id} ({task.name})\n"
            f"Status: {task.status}\n"
            f"Elapsed: {elapsed}\n"
            f"Tokens: {task.progress.input_tokens + task.progress.output_tokens}\n"
            f"Result:\n{result}"
        )


__all__ = [
    "TaskGetTool",
    "TaskListTool",
    "_format_task",
]
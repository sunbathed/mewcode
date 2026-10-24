"""第 15 章：共享任务工具（TaskCreate / TaskGet / TaskList / TaskUpdate）。

与第 13 章的后台任务查询工具（TaskList/TaskGet）不同名同实：这组工具
操作的是团队共享任务板（SharedTaskStore），是队员的「看板 + 工单」——
创建任务、查看详情、列看板、更新状态与依赖（addBlocks/addBlockedBy）。

工具实例不绑定具体团队：构造时注入 store provider（返回当前团队的
SharedTaskStore），队员侧绑定固定 store，Lead 侧按 active_team_name
动态解析。主 Agent 和普通 SubAgent 看不到这组工具，就像外包人员
没有公司内部的 Jira 权限一样。
"""

from __future__ import annotations

from typing import Any, Callable

from ..tools.base import BaseTool
from .shared_task import SharedTaskStore, VALID_STATUSES

# store provider：返回当前团队的共享任务板；拿不到时抛 TeamError
StoreProvider = Callable[[], SharedTaskStore]


def _store(provider: StoreProvider) -> SharedTaskStore:
    store = provider()
    if store is None:
        raise ValueError("当前没有活跃的团队任务板（先 TeamCreate 创建团队）。")
    return store


class TeamTaskCreateTool(BaseTool):
    """创建新的共享任务。"""

    name = "TaskCreate"
    description = (
        "在团队共享任务板上创建新任务。Lead 用它拆解目标，队员用它补充子任务；"
        "创建后队员通过 TaskList 看板看到它。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题（简短祈使句）。"},
            "description": {
                "type": "string",
                "description": "任务详情：做什么、验收标准；依赖原因也写在这里。",
            },
            "assignee": {"type": "string", "description": "指派给谁（可选，留空由队员自主认领）。"},
            "blocks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "该任务阻塞的其他任务 ID 列表（它们要等本任务完成）。",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "阻塞本任务的其他任务 ID 列表（它们完成前本任务不该开始）。",
            },
        },
        "required": ["title"],
    }
    safety = "read"

    def __init__(self, store_provider: StoreProvider) -> None:
        self._store_provider = store_provider

    async def _run(self, args: dict[str, Any]) -> str:
        title = str(args.get("title", "")).strip()
        if not title:
            return "Error: 'title' is required."
        store = _store(self._store_provider)
        task = store.create(
            title=title,
            description=str(args.get("description", "")),
            assignee=str(args.get("assignee", "")),
            blocks=[str(b) for b in args.get("blocks", []) or []],
            blocked_by=[str(b) for b in args.get("blocked_by", []) or []],
        )
        return f"Task {task.id} created: {task.title}\n{task.description}".strip()


class TeamTaskGetTool(BaseTool):
    """查看任务详情。"""

    name = "TaskGet"
    description = "查看团队共享任务板上某个任务的详情（状态、负责人、依赖关系）。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID。"},
        },
        "required": ["task_id"],
    }
    safety = "read"

    def __init__(self, store_provider: StoreProvider) -> None:
        self._store_provider = store_provider

    async def _run(self, args: dict[str, Any]) -> str:
        store = _store(self._store_provider)
        task = store.get(str(args.get("task_id", "")).strip())
        if task is None:
            return f"Task {args.get('task_id')} not found."
        lines = [
            f"Task {task.id}: {task.title}",
            f"Status: {task.status}",
            f"Assignee: {task.assignee or '（未指派）'}",
            f"Blocks: {', '.join(task.blocks) or '（无）'}",
            f"Blocked by: {', '.join(task.blocked_by) or '（无）'}",
            f"Description: {task.description or '（无）'}",
        ]
        return "\n".join(lines)


class TeamTaskListTool(BaseTool):
    """列出全部任务（看板视图，含状态与依赖）。"""

    name = "TaskList"
    description = "列出团队共享任务板上的所有任务及状态（pending / in_progress / completed）。"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    safety = "read"

    def __init__(self, store_provider: StoreProvider) -> None:
        self._store_provider = store_provider

    async def _run(self, args: dict[str, Any]) -> str:
        store = _store(self._store_provider)
        tasks = store.list_tasks()
        if not tasks:
            return "任务板为空。用 TaskCreate 创建任务。"
        lines = [f"共享任务板（共 {len(tasks)} 个任务）："]
        for task in tasks:
            deps = ""
            if task.blocked_by:
                deps = f" [blocked by {', '.join(task.blocked_by)}]"
            assignee = f" @{task.assignee}" if task.assignee else ""
            lines.append(f"- [{task.id}] {task.title} — {task.status}{assignee}{deps}")
        return "\n".join(lines)


class TeamTaskUpdateTool(BaseTool):
    """更新任务：状态、负责人、依赖（增量追加语义）。"""

    name = "TaskUpdate"
    description = (
        "更新共享任务：改状态（pending/in_progress/completed）、指派负责人，"
        "或用 addBlocks/addBlockedBy 建立任务依赖关系。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID。"},
            "status": {
                "type": "string",
                "enum": sorted(VALID_STATUSES),
                "description": "新状态（可选）。",
            },
            "assignee": {"type": "string", "description": "负责人（可选）。"},
            "description": {"type": "string", "description": "覆盖任务描述（可选）。"},
            "add_blocks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "追加：本任务阻塞的任务 ID（T1 完成前 T2 不该开始时，在 T2 上 addBlockedBy=T1）。",
            },
            "add_blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "追加：阻塞本任务的任务 ID。",
            },
        },
        "required": ["task_id"],
    }
    safety = "read"

    def __init__(self, store_provider: StoreProvider) -> None:
        self._store_provider = store_provider

    async def _run(self, args: dict[str, Any]) -> str:
        store = _store(self._store_provider)
        task_id = str(args.get("task_id", "")).strip()
        try:
            task = store.update(
                task_id,
                status=str(args["status"]) if "status" in args else None,
                assignee=str(args["assignee"]) if "assignee" in args else None,
                description=str(args["description"]) if "description" in args else None,
                add_blocks=[str(b) for b in args.get("add_blocks", []) or []] or None,
                add_blocked_by=[str(b) for b in args.get("add_blocked_by", []) or []] or None,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        if task is None:
            return f"Task {task_id} not found."
        return (
            f"Task {task.id} updated: {task.title} — {task.status}"
            f"（blocked by: {', '.join(task.blocked_by) or '无'}）"
        )


def build_team_task_tools(store_provider: StoreProvider) -> list[BaseTool]:
    """一组共享任务工具（绑定同一个 store provider）。"""
    return [
        TeamTaskCreateTool(store_provider),
        TeamTaskGetTool(store_provider),
        TeamTaskListTool(store_provider),
        TeamTaskUpdateTool(store_provider),
    ]


__all__ = [
    "TeamTaskCreateTool",
    "TeamTaskGetTool",
    "TeamTaskListTool",
    "TeamTaskUpdateTool",
    "build_team_task_tools",
]

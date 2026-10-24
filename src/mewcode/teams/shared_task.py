"""第 15 章：SharedTaskStore——带依赖关系的共享任务板。

团队成员之间的结构化协调方式：Lead 把用户目标拆成任务写进来，队员
通过 TaskList 看板认领、TaskUpdate 汇报进度。blocks / blocked_by 表达
任务间的依赖关系——「更新测试」等「数据层」完成，系统级约束比描述
文本里的口头约定更可靠。

存储是团队目录下的单个 tasks.json：任务量级小（每队员 2-4 个），
整文件读写足够，不需要数据库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 任务状态机：pending → in_progress → completed
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
VALID_STATUSES = {TASK_STATUS_PENDING, TASK_STATUS_IN_PROGRESS, TASK_STATUS_COMPLETED}


@dataclass(slots=True)
class SharedTask:
    """共享任务板上的一行。"""

    id: str
    title: str
    description: str = ""
    status: str = TASK_STATUS_PENDING
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)       # 我阻塞了哪些任务
    blocked_by: list[str] = field(default_factory=list)   # 我被哪些任务阻塞
    created_by: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "assignee": self.assignee,
            "blocks": list(self.blocks),
            "blocked_by": list(self.blocked_by),
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SharedTask":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", TASK_STATUS_PENDING)),
            assignee=str(data.get("assignee", "")),
            blocks=[str(b) for b in data.get("blocks", [])],
            blocked_by=[str(b) for b in data.get("blocked_by", [])],
            created_by=str(data.get("created_by", "")),
        )


class SharedTaskStore:
    """共享任务存储：CRUD + 依赖关系（blocks/blocked_by）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._tasks: dict[str, SharedTask] = {}
        self._next_id = 1
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        for item in tasks:
            if isinstance(item, dict):
                task = SharedTask.from_dict(item)
                self._tasks[task.id] = task
        self._next_id = self._max_numeric_id() + 1

    def _max_numeric_id(self) -> int:
        best = 0
        for task_id in self._tasks:
            if task_id.isdigit():
                best = max(best, int(task_id))
        return best

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"tasks": [t.to_dict() for t in self._tasks.values()]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def init_empty(self) -> None:
        """团队创建时初始化空任务板（文件存在即可见）。"""
        if not self._path.exists():
            self._save()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        created_by: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> SharedTask:
        task_id = str(self._next_id)
        self._next_id += 1
        task = SharedTask(
            id=task_id,
            title=title,
            description=description,
            assignee=assignee,
            blocks=[str(b) for b in (blocks or [])],
            blocked_by=[str(b) for b in (blocked_by or [])],
            created_by=created_by,
        )
        self._tasks[task_id] = task
        self._save()
        return task

    def get(self, task_id: str) -> SharedTask | None:
        return self._tasks.get(str(task_id))

    def list_tasks(self) -> list[SharedTask]:
        """全部任务，按数字 ID 排序（1、2、3…）。"""
        return sorted(self._tasks.values(), key=lambda t: (not t.id.isdigit(), int(t.id) if t.id.isdigit() else 0))

    def open_tasks(self) -> list[SharedTask]:
        return [t for t in self.list_tasks() if t.status != TASK_STATUS_COMPLETED]

    def delete(self, task_id: str) -> bool:
        if str(task_id) not in self._tasks:
            return False
        del self._tasks[str(task_id)]
        self._save()
        return True

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> SharedTask | None:
        """增量更新：字段为 None 就跳过；add_* 是追加语义，重复不重复加。"""
        task = self._tasks.get(str(task_id))
        if task is None:
            return None
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid task status: {status}")
            task.status = status
        if assignee is not None:
            task.assignee = assignee
        if description is not None:
            task.description = description
        if add_blocks:
            for bid in add_blocks:
                if bid not in task.blocks:
                    task.blocks.append(bid)
        if add_blocked_by:
            for bid in add_blocked_by:
                if bid not in task.blocked_by:
                    task.blocked_by.append(bid)
        self._save()
        return task


__all__ = [
    "SharedTask",
    "SharedTaskStore",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_IN_PROGRESS",
    "TASK_STATUS_PENDING",
    "VALID_STATUSES",
]

"""第 15 章：TeamManager——团队与队员生命周期的中枢。

所有团队操作都通过 TeamManager 发起：创建（检测后端 → 唯一命名 →
初始化目录三件套）、注册成员（花名册 + 全局名称注册表 + 反向映射）、
空闲通知（推模型：队友完成后主动写 mailbox 通知 Lead）、删除
（有活跃成员就拒绝；逐个清理注册表 / asyncio task / tmux pane /
worktree）。

内存态（in-process 句柄、pane id、agent_id → team 映射）与磁盘态
（config.json / tasks.json / mailbox/）在这里汇合；上层的团队工具
只跟 Manager 打交道。
"""

from __future__ import annotations

import logging
import shutil

from .mailbox import Mailbox, create_message
from .models import (
    AgentTeam,
    BackendType,
    TeamError,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from .registry import AgentNameRegistry
from .shared_task import SharedTaskStore
from .spawn_inprocess import InProcessTeammateHandle

logger = logging.getLogger(__name__)

# Lead 的收件箱标识（每个团队的 mailbox 目录内独立命名空间，可复用）
LEAD_AGENT_ID = "lead"


class TeamManager:
    """团队生命周期管理：创建 / 查询 / 注册 / 空闲通知 / 删除 / 关停。"""

    def __init__(self, worktree_manager=None) -> None:
        self.worktree_manager = worktree_manager
        # agent_id → 运行句柄（in-process）
        self._inprocess_handles: dict[str, InProcessTeammateHandle] = {}
        # agent_id → (pane_id / session_id, backend)
        self._pane_ids: dict[str, tuple[str, str]] = {}
        # agent_id → team_name 反向映射（SendMessage 按 ID 找团队）
        self._teammate_team_map: dict[str, str] = {}
        # Lead 侧共享任务工具的「当前团队」上下文（TeamCreate 时设置）
        self.active_team_name: str = ""

    # ------------------------------------------------------------------
    # 创建与查询
    # ------------------------------------------------------------------

    def detect_backend(self, teammate_mode: str = "", is_interactive: bool = True) -> BackendType:
        """后端检测委托（保持 Manager 单一入口的编排语义）。"""
        from .backend_detect import detect_backend

        return detect_backend(teammate_mode, is_interactive)

    def create_team(
        self,
        name: str,
        lead_agent_id: str = LEAD_AGENT_ID,
        description: str = "",
        teammate_mode: str = "",
    ) -> AgentTeam:
        """创建团队：唯一命名 → 初始化 config / tasks / mailbox 三件套。

        不在这里锁定后端——backendType 是 member 级别的，实际 spawn 时
        每个队员按环境独立选择（提示性检测由调用方在创建前完成）。
        """
        slug = unique_team_name(name)
        team_dir = resolve_team_dir(slug)
        team_dir.mkdir(parents=True, exist_ok=True)

        team = AgentTeam(
            name=slug,
            lead_agent_id=lead_agent_id,
            config_path=str(team_dir / "config.json"),
            description=description,
        )
        team.save()
        task_store = SharedTaskStore(team_dir / "tasks.json")
        task_store.init_empty()
        (team_dir / "mailbox").mkdir(parents=True, exist_ok=True)
        self.active_team_name = slug
        return team

    def get_team(self, team_name: str) -> AgentTeam | None:
        """从磁盘加载团队配置；不存在返回 None（每次读盘保证最新）。"""
        path = resolve_team_dir(team_name) / "config.json"
        if not path.exists():
            return None
        try:
            return AgentTeam.load(str(path))
        except (OSError, ValueError) as exc:
            logger.warning("Failed to load team config %s: %s", path, exc)
            return None

    def list_teams(self) -> list[AgentTeam]:
        """扫描团队根目录下的全部团队（按名称排序）。"""
        from .models import teams_base_dir

        base = teams_base_dir()
        if not base.is_dir():
            return []
        teams: list[AgentTeam] = []
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            team = self.get_team(entry.name)
            if team is not None:
                teams.append(team)
        return teams

    def get_mailbox(self, team_name: str) -> Mailbox | None:
        team_dir = resolve_team_dir(team_name)
        if not team_dir.exists():
            return None
        return Mailbox(team_dir / "mailbox")

    def get_task_store(self, team_name: str) -> SharedTaskStore | None:
        team_dir = resolve_team_dir(team_name)
        if not team_dir.exists():
            return None
        return SharedTaskStore(team_dir / "tasks.json")

    def find_team_for_agent(self, agent_id: str) -> AgentTeam | None:
        """按 agent_id 反查所属团队（SendMessage 路由用）。"""
        team_name = self._teammate_team_map.get(agent_id)
        if team_name is None:
            return None
        return self.get_team(team_name)

    # ------------------------------------------------------------------
    # 成员注册与状态
    # ------------------------------------------------------------------

    def register_member(self, team_name: str, member: TeammateInfo) -> None:
        """成员注册三件事：花名册持久化、全局名称注册、反向映射。"""
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")
        team.add_member(member)
        team.save()
        AgentNameRegistry.instance().register(member.name, member.agent_id)
        self._teammate_team_map[member.agent_id] = team_name

    def set_member_idle(self, team_name: str, member_name: str, last_output: str = "") -> None:
        """队员完成任务：标记不活跃 + 往 Lead 邮箱写空闲通知（推模型）。"""
        team = self.get_team(team_name)
        if team is None:
            return
        team.set_member_active(member_name, False)
        team.save()
        mailbox = self.get_mailbox(team_name)
        if mailbox:
            content = f"Teammate '{member_name}' is now idle. Work completed and results are ready."
            if last_output:
                content += f"\n最后输出摘要：\n{last_output[:800]}"
            msg = create_message(
                from_agent=member_name,
                to_agent=team.lead_agent_id,
                content=content,
                summary=f"{member_name} idle",
            )
            mailbox.write(team.lead_agent_id, msg)

    def set_member_active(self, team_name: str, member_name: str) -> bool:
        """队员被唤醒（续写）：标记回活跃状态。"""
        team = self.get_team(team_name)
        if team is None:
            return False
        found = team.set_member_active(member_name, True)
        if found:
            team.save()
        return found

    # ------------------------------------------------------------------
    # 运行句柄与窗格
    # ------------------------------------------------------------------

    def register_inprocess_handle(self, agent_id: str, handle: InProcessTeammateHandle) -> None:
        self._inprocess_handles[agent_id] = handle

    def get_inprocess_handle(self, agent_id: str) -> InProcessTeammateHandle | None:
        return self._inprocess_handles.get(agent_id)

    def register_pane(self, agent_id: str, pane_id: str, backend: str) -> None:
        self._pane_ids[agent_id] = (pane_id, backend)

    def get_pane(self, agent_id: str) -> tuple[str, str] | None:
        return self._pane_ids.get(agent_id)

    def running_agent_ids(self) -> list[str]:
        """仍在运行的 in-process 队员 ID（删除团队前检查用）。"""
        return [aid for aid, handle in self._inprocess_handles.items() if not handle.done]

    # ------------------------------------------------------------------
    # 删除与关停
    # ------------------------------------------------------------------

    def delete_team(self, team_name: str) -> dict:
        """删除团队：有活跃成员就拒绝；逐个清理每个队员的运行资源。

        返回清理摘要（取消的句柄数、关闭的窗格数、清理的 worktree 数）。
        """
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")
        active = [m.name for m in team.active_members()]
        if active:
            raise TeamError(f"Cannot delete team: active members: {', '.join(active)}")

        summary = {"cancelled": 0, "panes": 0, "worktrees": 0}
        for member in list(team.members):  # 拷贝：循环体里会改 team.members
            AgentNameRegistry.instance().unregister(member.name)
            handle = self._inprocess_handles.pop(member.agent_id, None)
            if handle and not handle.done:
                handle.cancel()
                summary["cancelled"] += 1
            pane = self._pane_ids.pop(member.agent_id, None)
            if pane:
                self._kill_pane(pane[0], pane[1])
                summary["panes"] += 1
            if member.worktree_path and self._cleanup_worktree(member.worktree_path):
                summary["worktrees"] += 1
            self._teammate_team_map.pop(member.agent_id, None)

        team_dir = resolve_team_dir(team_name)
        shutil.rmtree(team_dir, ignore_errors=True)
        if self.active_team_name == team_name:
            self.active_team_name = ""
        return summary

    def _kill_pane(self, pane_id: str, backend: str) -> None:
        if backend == BackendType.TMUX.value:
            from .spawn_tmux import kill_tmux_pane

            kill_tmux_pane(pane_id)
        else:
            from .spawn_iterm2 import kill_iterm2_pane

            kill_iterm2_pane(pane_id)

    def _cleanup_worktree(self, worktree_path: str) -> bool:
        """同步 best-effort 删除队员的 worktree（已由 Lead 合并过，强制清理）。

        事件循环里跑不了同步 git 子进程等待时退化为后台任务；拿不到
        循环（纯同步测试场景）就跳过并返回 False。
        """
        if self.worktree_manager is None:
            return False
        try:
            target = next(
                (wt for wt in self.worktree_manager.list_worktrees() if wt.path == worktree_path),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - 清理失败只记日志
            logger.warning("Worktree listing failed for %s: %s", worktree_path, exc)
            return False
        if target is None:
            return False
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(self.worktree_manager.remove(target.name, discard_changes=True))
        return True

    def shutdown(self) -> None:
        """退出时取消所有仍在运行的 in-process 队员（避免事件循环泄漏）。"""
        for handle in list(self._inprocess_handles.values()):
            if not handle.done:
                handle.cancel()
        self._inprocess_handles.clear()


__all__ = ["LEAD_AGENT_ID", "TeamManager"]

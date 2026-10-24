"""第 15 章：团队顶层工具（TeamCreate / TeamDelete / SendMessage）。

TeamCreate / TeamDelete 是独立的顶层工具，不是 Agent 工具的参数分支：
团队的创建是一个有明确边界的操作，它会改变 Lead 的运行模式，不应该
作为 Agent 工具的副作用偷偷发生。Agent 工具的 team_name 参数负责往
已有团队里 spawn 队员，两者配合职责清晰。

SendMessage 是队员之间的直接通信能力。to 支持三种寻址：队友名称或
agent_id（经 AgentNameRegistry 解析，两者路由到同一个地方）、"lead"（发
给负责人）、"*"（广播全团队）。目标已停止时，Lead 侧会自动从磁盘
transcript 恢复它（续写机制）——队员的生命周期因此是暂停/恢复式的。
"""

from __future__ import annotations

from typing import Any

from ..tools.base import BaseTool
from .backend_detect import detect_backend_or_fallback
from .mailbox import (
    KNOWN_MESSAGE_TYPES,
    Mailbox,
    MailboxMessage,
    MESSAGE_PLAN_APPROVAL_RESPONSE,
    MESSAGE_SHUTDOWN_RESPONSE,
    MESSAGE_TEXT,
    create_message,
)
from .manager import LEAD_AGENT_ID, TeamManager
from .models import AgentTeam, TeamError, TeammateInfo
from .registry import AgentNameRegistry


class TeamCreateTool(BaseTool):
    """创建团队：初始化配置 / 共享任务板 / 邮箱三件套。"""

    name = "TeamCreate"
    description = (
        "创建一个 Agent 团队（ Lead 是你自己）：初始化团队配置、共享任务板和"
        "消息邮箱。创建后用 Agent 工具带 team_name 参数派生队员。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "团队名称（同名自动加序号）。"},
            "description": {"type": "string", "description": "团队目标描述（可选）。"},
            "teammate_mode": {
                "type": "string",
                "enum": ["", "in-process", "tmux", "iterm2"],
                "description": "队员运行后端（可选，留空自动检测；检测不到终端后端时回退 in-process）。",
            },
        },
        "required": ["team_name"],
    }
    safety = "read"

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def _run(self, args: dict[str, Any]) -> str:
        team_name = str(args.get("team_name", "")).strip()
        if not team_name:
            return "Error: 'team_name' is required."
        teammate_mode = str(args.get("teammate_mode", "") or "")
        backend, fallback_notice = detect_backend_or_fallback(teammate_mode)
        try:
            team = self._manager.create_team(
                team_name,
                description=str(args.get("description", "")),
                teammate_mode=teammate_mode,
            )
        except TeamError as exc:
            return f"Error: {exc}"
        return (
            f"Team '{team.name}' created.{fallback_notice}\n"
            f"- 后端（默认检测）：{backend.value}\n"
            f"- 团队目录：{team.config_path}\n"
            f"- 共享任务板与消息邮箱已初始化\n"
            f"下一步：用 Agent 工具派生队员，参数传 team_name='{team.name}'、"
            f"name='队员名'（如 alice），队员会自动获得独立 worktree 与协作工具"
            f"（TaskCreate/TaskList/TaskUpdate/SendMessage）。"
        )


class TeamDeleteTool(BaseTool):
    """删除团队：先确认全员空闲，再逐个清理并删除团队目录。"""

    name = "TeamDelete"
    description = (
        "删除一个 Agent 团队：终止队员、关闭窗格、清理 worktree、删除团队目录。"
        "有活跃队员时拒绝——请先等队员全部空闲（并确认已合并它们的成果）。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "团队名称。"},
        },
        "required": ["team_name"],
    }
    safety = "read"

    def __init__(self, manager: TeamManager) -> None:
        self._manager = manager

    async def _run(self, args: dict[str, Any]) -> str:
        team_name = str(args.get("team_name", "")).strip()
        if not team_name:
            return "Error: 'team_name' is required."
        try:
            summary = self._manager.delete_team(team_name)
        except TeamError as exc:
            return f"Error: {exc}"
        return (
            f"Team '{team_name}' deleted."
            f"（取消运行中队友 {summary['cancelled']} 个，关闭窗格 {summary['panes']} 个，"
            f"清理 worktree {summary['worktrees']} 个）"
        )


class SendMessageTool(BaseTool):
    """点对点消息：队员之间、Lead 与队员之间的直接通信。"""

    name = "SendMessage"
    description = (
        "给团队里的其他成员发消息。to 支持队友名称（如 alice）、agent_id、"
        "'lead'（发给负责人）或 '*'（广播全团队）。纯文本消息必须带 5-10 词的"
        " summary 摘要。目标队员已停止时（Lead 侧）会自动从磁盘恢复它的上下文。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "收件人：队友名称 / agent_id / 'lead' / '*'（广播）。",
            },
            "message": {"type": "string", "description": "消息内容。"},
            "summary": {
                "type": "string",
                "description": "5-10 词的摘要，作为消息列表的预览（纯文本消息必填）。",
            },
            "message_type": {
                "type": "string",
                "enum": sorted(KNOWN_MESSAGE_TYPES),
                "description": "结构化消息类型（可选，默认 text；shutdown_request / shutdown_response / plan_approval_response）。",
            },
        },
        "required": ["to", "message"],
    }
    safety = "read"

    def __init__(
        self,
        manager: TeamManager,
        *,
        host: Any = None,
        mailbox: Mailbox | None = None,
        team_name: str = "",
        self_name: str = LEAD_AGENT_ID,
        self_agent_id: str = "",
        allow_resume: bool = False,
    ) -> None:
        """host 侧：动态解析团队、可续写；teammate 侧：绑定固定团队上下文。"""
        self._manager = manager
        self._host = host
        self._mailbox = mailbox
        self._team_name = team_name
        self._self_name = self_name
        self._self_agent_id = self_agent_id
        self._allow_resume = allow_resume

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def _run(self, args: dict[str, Any]) -> str:
        to = str(args.get("to", "")).strip()
        content = str(args.get("message", ""))
        summary = str(args.get("summary", "")).strip()
        message_type = str(args.get("message_type", MESSAGE_TEXT) or MESSAGE_TEXT)
        if not to:
            return "Error: 'to' is required."
        if not content.strip():
            return "Error: 'message' is required."
        if message_type not in KNOWN_MESSAGE_TYPES:
            return f"Error: unknown message_type '{message_type}'."
        if message_type == MESSAGE_TEXT and not summary:
            return "Error: 纯文本消息必须提供 summary（5-10 词摘要）。"
        if message_type == MESSAGE_PLAN_APPROVAL_RESPONSE and self._self_name != LEAD_AGENT_ID:
            return "Error: plan_approval_response 只能由 Lead 发送。"

        team = self._resolve_team(to)
        if team is None:
            return f"Error: 无法定位团队上下文（to='{to}'）。先 TeamCreate 或确认团队存在。"
        mailbox = self._mailbox or self._manager.get_mailbox(team.name)
        if mailbox is None:
            return f"Error: 团队 '{team.name}' 的邮箱不存在。"

        if to == "*":
            return self._broadcast(team, mailbox, content, summary, message_type)
        return await self._send_to(team, mailbox, to, content, summary, message_type)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def _resolve_team(self, to: str) -> AgentTeam | None:
        """收件人 → 所属团队。固定绑定（队员侧）优先，其次按 agent_id 反查。"""
        if self._team_name:
            return self._manager.get_team(self._team_name)
        if to in ("lead", LEAD_AGENT_ID):
            # 队员侧发 lead：用绑定的团队
            return self._manager.get_team(self._team_name) if self._team_name else None
        agent_id = AgentNameRegistry.instance().resolve(to)
        if agent_id is not None:
            team = self._manager.find_team_for_agent(agent_id)
            if team is not None:
                return team
        # Lead 侧广播/未知名：落到当前活跃团队
        if self._manager.active_team_name:
            return self._manager.get_team(self._manager.active_team_name)
        return None

    def _broadcast(
        self,
        team: AgentTeam,
        mailbox: Mailbox,
        content: str,
        summary: str,
        message_type: str,
    ) -> str:
        targets = [m.agent_id for m in team.members if m.agent_id != self._self_agent_id]
        if team.lead_agent_id != self._self_name:
            targets.append(team.lead_agent_id)
        count = mailbox.broadcast(targets, create_message(
            from_agent=self._self_name,
            to_agent="*",
            content=content,
            summary=summary,
            message_type=message_type,
        ))
        self._wake_panes(team)
        return f"Message broadcast to {count} teammates."

    async def _send_to(
        self,
        team: AgentTeam,
        mailbox: Mailbox,
        to: str,
        content: str,
        summary: str,
        message_type: str,
    ) -> str:
        if message_type == MESSAGE_SHUTDOWN_RESPONSE and to not in ("lead", LEAD_AGENT_ID, team.lead_agent_id):
            return "Error: shutdown_response 只能发给 lead。"
        if to in ("lead", LEAD_AGENT_ID, team.lead_agent_id):
            if self._self_name == LEAD_AGENT_ID and to in ("lead", LEAD_AGENT_ID):
                return "Error: 你就是 lead，不能给自己发消息。"
            mailbox.write(
                team.lead_agent_id,
                create_message(
                    from_agent=self._self_name,
                    to_agent=team.lead_agent_id,
                    content=content,
                    summary=summary,
                    message_type=message_type,
                ),
            )
            return f"Message delivered to lead."
        agent_id = AgentNameRegistry.instance().resolve(to)
        if agent_id is None:
            return (
                f"Error: 未找到收件人 '{to}'。用队友名称（如 alice）或 agent_id，"
                f"或 '*' 广播全团队。"
            )
        member = self._member_by_id(team, agent_id)
        if member is None:
            return f"Error: '{to}' 不在团队 '{team.name}' 中。"
        if member.name == self._self_name:
            return "Error: 不能给自己发消息。"
        mailbox.write(
            agent_id,
            create_message(
                from_agent=self._self_name,
                to_agent=member.name,
                content=content,
                summary=summary,
                message_type=message_type,
            ),
        )
        self._wake_pane(member)
        # 续写：目标已停止 + 调用方有恢复能力 → 从磁盘 transcript 恢复
        if self._allow_resume and member.backend_type == "in-process":
            return await self._maybe_resume(team, member, content)
        return f"Message delivered to {member.name}（{member.backend_type}，将于其下一轮 Agent Loop 开头被读取）。"

    @staticmethod
    def _member_by_id(team: AgentTeam, agent_id: str) -> TeammateInfo | None:
        for member in team.members:
            if member.agent_id == agent_id:
                return member
        return None

    def _wake_pane(self, member: TeammateInfo) -> None:
        """tmux 后端写完 mailbox 额外 send-keys 唤醒目标 pane。"""
        if member.backend_type != "tmux":
            return
        pane = self._manager.get_pane(member.agent_id)
        if pane is None:
            return
        try:
            from .spawn_tmux import wake_tmux_pane

            wake_tmux_pane(pane[0])
        except Exception:  # noqa: BLE001 - 唤醒失败不影响投递
            pass

    def _wake_panes(self, team: AgentTeam) -> None:
        for member in team.members:
            self._wake_pane(member)

    async def _maybe_resume(self, team: AgentTeam, member: TeammateInfo, content: str) -> str:
        handle = self._manager.get_inprocess_handle(member.agent_id)
        if handle is not None and not handle.done:
            return f"Message delivered to {member.name}（运行中，将于其下一轮 Agent Loop 开头被读取）。"
        spawner = getattr(self._host, "team_spawner", None) if self._host is not None else None
        if spawner is None:
            return f"Message delivered to {member.name}（已停止，但当前环境无法自动恢复它）。"
        return await spawner.resume(team, member, content)


__all__ = ["SendMessageTool", "TeamCreateTool", "TeamDeleteTool"]

"""第 15 章：队员的构造、运行与续写（Team 模式与 SubAgent 的接合部）。

让一个子 Agent 变成团队成员需要三件事：
  1. 往工具集里注入协调工具（build_teammate_tools）：共享任务板四件套
     + SendMessage，叠加在基础工作工具之上；Agent 工具不在其中——
     队员不能 spawn 队员，只有 Lead 能扩张团队
  2. 系统提示追加团队通信附录（TEAMMATE_APPENDIX）：只解决一个问题——
     纯文本回复对队友不可见，想通信必须用 SendMessage
  3. 每轮 Agent Loop 开头消费邮箱（pre_turn_inject）：未读消息注入成
     system reminder，下一次调用 LLM 时就能看到

TeammateSpawner 负责完整生命周期：创建 worktree → 注册成员 → 构造
Agent → 按后端分流 spawn → 完成后存 transcript、标记空闲、通知 Lead。
Lead 通过 SendMessage 给已停止的队员发消息时，resume() 从磁盘
transcript 恢复它的对话历史继续工作——队员因此是持续性的工作者。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from ..engine.agent import Agent, AgentConfig
from ..permission import PermissionChecker, PermissionMode
from ..session import Session
from ..tools import ToolRegistry
from .mailbox import Mailbox, MailboxMessage
from .manager import TeamManager
from .models import BackendType, TeamError, TeammateInfo
from .shared_task import SharedTaskStore
from .spawn_inprocess import InProcessTeammateHandle
from .team_task_tools import build_team_task_tools
from .tools import SendMessageTool
from .transcript import load_transcript, save_transcript

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..subagent.agent_tool import AgentTool
    from ..subagent.types import AgentToolParams

logger = logging.getLogger(__name__)

# 队员系统提示附录：只解决「纯文本回复队友看不见」这一个问题。
# 工具列表本身是自描述的，具体工作流程由 Lead 通过 prompt 传达。
TEAMMATE_APPENDIX = """
IMPORTANT: You are running as an agent in a team.
Just writing a response in text is not visible to others on your team - you MUST use the SendMessage tool.
The user interacts primarily with the team lead.
Your work is coordinated through the task system and teammate messaging.
"""

# 队员的基础工作工具（不含 Agent——队员不能 spawn 队员）
TEAMMATE_BASE_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "ReadFile",
        "WriteFile",
        "EditFile",
        "Bash",
        "Glob",
        "Grep",
        "ToolSearch",
        "LoadSkill",
        "Skill",
    }
)

# 协调者上下文引用的队员完整工具池（工作工具 + 协调工具）
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: frozenset[str] = TEAMMATE_BASE_ALLOWED_TOOLS | {
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
}

# 随目录根变化、需要以 worktree 为根重建的核心文件工具（与 worktree 集成一致）
_CORE_TOOL_NAMES = frozenset({"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep"})


# ---------------------------------------------------------------------------
# 工具注入
# ---------------------------------------------------------------------------


def build_teammate_tools(
    base_registry: ToolRegistry | None,
    *,
    worktree_path: str,
    task_store: SharedTaskStore,
    mailbox: Mailbox,
    manager: TeamManager,
    team_name: str,
    teammate_name: str,
    self_agent_id: str,
) -> ToolRegistry:
    """构造队员的工具注册中心：基础工作工具 + 协调工具。

    核心文件工具以 worktree 为根重建（显式 cwd 机制）；其余按队员白名单
    挑选；MCP 工具直通。协调工具（共享任务板 + SendMessage）绑定本队员
    的团队上下文——主 Agent 和普通 SubAgent 看不到它们。
    """
    fresh = ToolRegistry(workdir=worktree_path)
    if base_registry is not None:
        for name, tool in base_registry._tools.items():  # noqa: SLF001 - 同包内复用内部表
            if name in _CORE_TOOL_NAMES or name == "ToolSearch":
                continue  # 核心工具已按 worktree 重建
            if name.startswith("mcp_"):
                fresh.register(tool)  # MCP 工具直通（独立的权限控制）
                continue
            if name in TEAMMATE_BASE_ALLOWED_TOOLS:
                fresh.register(tool)
    for task_tool in build_team_task_tools(lambda: task_store):
        fresh.register(task_tool)
    fresh.register(
        SendMessageTool(
            manager,
            mailbox=mailbox,
            team_name=team_name,
            self_name=teammate_name,
            self_agent_id=self_agent_id,
            allow_resume=False,  # 续写是 Lead 的能力，队员只投递消息
        )
    )
    return fresh


def format_mailbox_digest(messages: list[MailboxMessage]) -> str:
    """把未读消息格式化成注入 Agent Loop 开头的 system reminder 文本。"""
    if not messages:
        return ""
    parts = [
        f'<teammate-message from="{m.from_agent}" to="{m.to_agent}" type="{m.message_type}" summary="{m.summary}">\n'
        f"{m.content}\n"
        f"</teammate-message>"
        for m in messages
    ]
    header = f"收到 {len(messages)} 条团队消息（请在继续当前工作前先处理）："
    return header + "\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# 队员 spawn / 续写
# ---------------------------------------------------------------------------


class TeammateSpawner:
    """队员完整生命周期的编排者（宿主 + Agent 工具的接合部）。

    Agent 工具的 team_name 路径与 SendMessage 的续写路径都委托到这里，
    共享同一套「worktree → 注册 → 构造 → spawn」流程。
    """

    def __init__(self, host: Any, agent_tool: "AgentTool | None" = None) -> None:
        self._host = host
        self._agent_tool = agent_tool

    # ------------------------------------------------------------------
    # 宿主资源
    # ------------------------------------------------------------------

    @property
    def manager(self) -> TeamManager | None:
        return getattr(self._host, "team_manager", None)

    def _get_agent_tool(self) -> "AgentTool | None":
        if self._agent_tool is not None:
            return self._agent_tool
        return getattr(self._host, "agent_tool", None)

    def _workdir(self) -> str:
        return getattr(self._host, "workdir", "") or ""

    # ------------------------------------------------------------------
    # Spawn：Lead 的 Agent 工具带 team_name 时走这里
    # ------------------------------------------------------------------

    async def spawn(self, params: "AgentToolParams") -> str:
        manager = self.manager
        if manager is None:
            return "Error: team manager is not initialized."
        team = manager.get_team(params.team_name)
        if team is None:
            return (
                f"Error: Team '{params.team_name}' not found. "
                "先用 TeamCreate 创建团队，再往里面 spawn 队员。"
            )
        name = (params.name or "").strip() or self._next_name(team)
        if team.get_member(name) is not None:
            return (
                f"Error: 队员 '{name}' 已存在于团队 '{team.name}'。"
                f"换一个 name，或直接用 SendMessage 给它发新指令。"
            )

        agent_tool = self._get_agent_tool()
        if agent_tool is None:
            return "Error: agent tool is not available."
        loader = agent_tool._loader
        if params.subagent_type:
            definition = loader.get(params.subagent_type)
            if definition is None:
                return (
                    f"Unknown agent type: '{params.subagent_type}'. "
                    f"Available types: {loader.available_types_text()}"
                )
        else:
            definition = loader.get("general-purpose")
            if definition is None:
                return "Error: general-purpose agent definition is missing."

        from .backend_detect import detect_backend_or_fallback

        backend, fallback_notice = detect_backend_or_fallback()

        # 队员自动获得独立 Worktree（创建失败则共享主目录并说明）
        worktree_path = self._workdir()
        worktree_note = ""
        wt_manager = getattr(self._host, "worktree_manager", None)
        if wt_manager is not None:
            try:
                wt = await wt_manager.create(f"team-{team.name}/{name}", "HEAD")
                worktree_path = wt.path
            except Exception as exc:  # noqa: BLE001 - worktree 失败不阻断 spawn
                worktree_note = f"（worktree 创建失败，共享主目录：{exc}）"

        agent_id = uuid.uuid4().hex[:12]
        member = TeammateInfo(
            name=name,
            agent_id=agent_id,
            agent_type=definition.agent_type,
            model=params.model or definition.model,
            worktree_path=worktree_path,
            backend_type=backend.value,
            is_active=True,
        )
        manager.register_member(team.name, member)

        if backend is BackendType.IN_PROCESS:
            handle = await self._spawn_inprocess(team, member, definition, params, agent_tool)
            running_note = "" if not handle.done else "（注意：任务已结束）"
        else:
            try:
                self._spawn_pane(team, member, params, backend)
            except Exception as exc:  # noqa: BLE001 - pane 启动失败返回错误文本
                return f"Error: failed to spawn teammate '{name}' on {backend.value}: {exc}"
            running_note = ""
        return (
            f"Teammate '{name}' spawned in team '{team.name}'.\n"
            f"- agent_id: {agent_id}\n"
            f"- backend: {member.backend_type}{fallback_notice}\n"
            f"- worktree: {worktree_path} {worktree_note}\n"
            f"{running_note}"
            f"队员在独立上下文中自主工作；完成（空闲）时会写消息通知你。"
            f"期间可用 SendMessage(to='{name}') 与它沟通，"
            f"停止后 SendMessage 会自动从磁盘恢复它继续工作。"
        )

    def _next_name(self, team) -> str:
        existing = {m.name for m in team.members}
        index = 1
        while f"teammate-{index}" in existing:
            index += 1
        return f"teammate-{index}"

    # ------------------------------------------------------------------
    # In-process 路径
    # ------------------------------------------------------------------

    async def _spawn_inprocess(
        self,
        team,
        member: TeammateInfo,
        definition,
        params: "AgentToolParams",
        agent_tool: "AgentTool",
    ) -> InProcessTeammateHandle:
        manager = self.manager
        assert manager is not None
        agent = self._build_teammate_agent(team, member, definition, params, agent_tool, session=None)
        task = asyncio.create_task(
            self._run_member_lifecycle(team, member, agent, params.prompt),
            name=f"team-{team.name}-{member.name}",
        )
        handle = InProcessTeammateHandle(agent=agent, task=task, name=member.name)
        manager.register_inprocess_handle(member.agent_id, handle)
        # 让 spawn 调用立即返回（队员异步跑），同时让出控制权启动协程
        await asyncio.sleep(0)
        return handle

    def _build_teammate_agent(
        self,
        team,
        member: TeammateInfo,
        definition,
        params: "AgentToolParams",
        agent_tool: "AgentTool",
        session: Session | None,
    ) -> Agent:
        """构造队员 Agent 实例：worktree 根 + 协调工具 + 团队附录 + 邮箱消费。"""
        manager = self.manager
        assert manager is not None
        config = getattr(self._host, "config", None)
        if config is None:
            raise TeamError("agent host is not initialized.")
        worktree_path = member.worktree_path or self._workdir()

        base_registry = agent_tool._host_registry()
        mailbox = manager.get_mailbox(team.name)
        task_store = manager.get_task_store(team.name)
        if mailbox is None or task_store is None:
            raise TeamError(f"Team '{team.name}' storage is missing (mailbox/tasks).")
        registry = build_teammate_tools(
            base_registry,
            worktree_path=worktree_path,
            task_store=task_store,
            mailbox=mailbox,
            manager=manager,
            team_name=team.name,
            teammate_name=member.name,
            self_agent_id=member.agent_id,
        )
        client = agent_tool._select_llm(config, params, definition)
        model_name = getattr(client, "config", None)
        session_model = model_name.model if model_name is not None else getattr(config, "model", "")
        system_prompt = (definition.system_prompt or "") + TEAMMATE_APPENDIX
        agent = Agent(
            config,
            session=session or Session(model=session_model),
            registry=registry,
            workdir=worktree_path,
            client=client,
            agent_config=AgentConfig(
                max_iterations=definition.max_turns,
                system_prompt=system_prompt,
                custom_instructions="",
                memory_instructions="",
                skill_catalog="",
            ),
            cancel_event=None,
            # 队员是自主工作者：工具过滤已锁定能力边界，不再触发 HITL
            permission=PermissionChecker(workdir=worktree_path, mode=PermissionMode.BYPASS_PERMISSIONS),
            context=None,
            hooks=agent_tool._host_hooks(),
        )
        agent._trace_id = f"team-{team.name}/{member.name}"
        # 每轮 Agent Loop 开头消费邮箱：未读消息注入成 system reminder
        agent.pre_turn_inject = self._make_mailbox_drain(manager, team.name, member.agent_id)
        return agent

    @staticmethod
    def _make_mailbox_drain(manager: TeamManager, team_name: str, agent_id: str):
        def _drain(turn: int) -> list[str]:
            mailbox = manager.get_mailbox(team_name)
            if mailbox is None:
                return []
            messages = mailbox.consume(agent_id)
            digest = format_mailbox_digest(messages)
            return [digest] if digest else []

        return _drain

    async def _run_member_lifecycle(self, team, member: TeammateInfo, agent: Agent, prompt: str) -> str:
        """队员执行生命周期：跑完 → 存 transcript → 标记空闲 → 通知 Lead。"""
        manager = self.manager
        assert manager is not None
        try:
            result = await agent.run_to_completion(prompt)
        except asyncio.CancelledError:
            # 被 TeamDelete/退出取消：留下现场记录，但不算「空闲通知」
            save_transcript(team.name, member.agent_id, agent.session)
            raise
        except Exception as exc:  # noqa: BLE001 - 队员崩溃也要走空闲收尾
            logger.exception("Teammate %s failed", member.name)
            result = f"（队员执行失败：{exc}）"
        try:
            save_transcript(team.name, member.agent_id, agent.session)
            manager.set_member_idle(team.name, member.name, last_output=result)
        except Exception:  # noqa: BLE001 - 收尾失败不影响主流程
            logger.exception("Teammate %s idle finalization failed", member.name)
        return result

    # ------------------------------------------------------------------
    # Pane 后端路径（tmux / iTerm2）
    # ------------------------------------------------------------------

    def _spawn_pane(self, team, member: TeammateInfo, params: "AgentToolParams", backend: BackendType) -> None:
        manager = self.manager
        assert manager is not None
        if backend is BackendType.TMUX:
            from .spawn_tmux import spawn_tmux_teammate

            info = spawn_tmux_teammate(
                team_name=team.name,
                teammate_name=member.name,
                worktree_path=member.worktree_path,
                prompt=params.prompt,
                agent_type=member.agent_type,
                model="" if member.model == "inherit" else member.model,
            )
            manager.register_pane(member.agent_id, info.pane_id, backend.value)
            return
        from .spawn_iterm2 import spawn_iterm2_teammate

        info = spawn_iterm2_teammate(
            team_name=team.name,
            teammate_name=member.name,
            worktree_path=member.worktree_path,
            prompt=params.prompt,
            agent_type=member.agent_type,
            model="" if member.model == "inherit" else member.model,
        )
        manager.register_pane(member.agent_id, info.session_id, backend.value)

    # ------------------------------------------------------------------
    # 续写：SendMessage 目标已停止时从磁盘 transcript 恢复
    # ------------------------------------------------------------------

    async def resume(self, team, member: TeammateInfo, message: str) -> str:
        """从磁盘恢复已停止的队员，带着完整历史上下文继续工作。"""
        manager = self.manager
        if manager is None:
            return "Error: team manager is not initialized."
        agent_tool = self._get_agent_tool()
        if agent_tool is None:
            return "Error: agent tool is not available."
        loader = agent_tool._loader
        definition = loader.get(member.agent_type) or loader.get("general-purpose")
        if definition is None:
            return f"Error: agent definition '{member.agent_type}' is missing."

        # worktree 目录可能已被清理：退化为共享主目录
        worktree_path = member.worktree_path
        if not worktree_path or not os.path.isdir(worktree_path):
            worktree_path = self._workdir()
            member.worktree_path = worktree_path

        session = load_transcript(team.name, member.agent_id)
        if session is None:
            session = Session(model="")

        params = self._params_for_member(member)
        agent = self._build_teammate_agent(team, member, definition, params, agent_tool, session=session)
        member.is_active = True
        team.save()
        manager.set_member_active(team.name, member.name)

        # 复用 spawn 的生命周期：run_to_completion(message) 把续写指令
        # 作为新的 user 消息注入（引擎修复后只写一次），收尾同样存
        # transcript、标记空闲、通知 Lead
        task = asyncio.create_task(
            self._run_member_lifecycle(team, member, agent, message),
            name=f"team-{team.name}-{member.name}",
        )
        handle = InProcessTeammateHandle(agent=agent, task=task, name=member.name)
        manager.register_inprocess_handle(member.agent_id, handle)
        await asyncio.sleep(0)
        return (
            f"Message delivered to {member.name}（已停止 → 已从磁盘 transcript 恢复，"
            f"带着完整上下文继续工作；agent_id={member.agent_id}）。"
        )

    @staticmethod
    def _params_for_member(member: TeammateInfo):
        from ..subagent.types import AgentToolParams

        return AgentToolParams(
            prompt="",
            description=f"resume teammate {member.name}",
            subagent_type=member.agent_type,
            model=member.model if member.model not in ("", "inherit") else "",
        )


__all__ = [
    "IN_PROCESS_TEAMMATE_ALLOWED_TOOLS",
    "TEAMMATE_APPENDIX",
    "TEAMMATE_BASE_ALLOWED_TOOLS",
    "TeammateSpawner",
    "build_teammate_tools",
    "format_mailbox_digest",
]

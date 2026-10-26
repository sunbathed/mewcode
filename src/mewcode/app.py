from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import sys
from pathlib import Path
from typing import Any

from .commands import (
    Command,
    CommandContext,
    CommandRegistry,
    CommandType,
    ParsedCommand,
    build_default_registry,
    command_usage,
    parse_command,
)
from .config import ConfigurationError, ProviderConfig, load_provider_config
from .context import CompactError, ContextConfig, ContextManager
from .engine import (
    Agent,
    AgentConfig,
    AgentStopReason,
    ContextCompactedEvent,
    ErrorEvent,
    LoopCompleteEvent,
    PermissionRequestEvent,
    PlanModeState,
    TextChunkEvent,
    ToolResultEvent,
    ToolUseEvent,
    build_plan_mode_tools,
)
from .llm_client import OpenAICompatibleClient
from .memory import (
    MemoryManager,
    MemoryManagerError,
    build_time_gap_message,
    load_instructions,
)
from .permission import (
    MODE_ALIASES,
    PermissionChecker,
    PermissionMode,
    PermissionResponse,
    parse_mode,
)
from .session import Session
from .storage import SessionStorage, SessionStorageError
from .terminal import SPINNER_FRAMES, Terminal
from .tools import ToolRegistry, ToolResult
from .mcp import MCPManager
from .hooks import HookEngine, HookContext, load_hooks
from .hooks.models import LifecycleEvent, WRITE_TOOL_NAMES
from .subagent import AgentLoader, AgentTool, TaskManager, TraceRegistry
from .subagent.task_tools import TaskGetTool, TaskListTool


class App:
    """终端版 MewCode：接入第四章的 Agent Loop 引擎（事件流驱动 UI）。"""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        storage: SessionStorage | None = None,
        client=None,
        terminal: Terminal | None = None,
        registry: ToolRegistry | None = None,
        terminal_ready: asyncio.Event | None = None,
        workdir: str | None = None,
        max_iterations: int = 10,
        permission_mode: PermissionMode | str = PermissionMode.DEFAULT,
        permission: PermissionChecker | None = None,
        mcp_manager: MCPManager | None = None,
        context: ContextManager | None = None,
        memory: MemoryManager | None = None,
        memory_client=None,
        auto_memory: bool = True,
    ) -> None:
        self.config = config
        self.storage = storage or SessionStorage()
        self.client = client or OpenAICompatibleClient(config)
        self.terminal = terminal or Terminal()
        self.registry = registry or ToolRegistry()
        self.workdir = workdir or str(Path.cwd())
        self.max_iterations = max_iterations
        self.session = self.storage.latest() or Session(model=config.model)
        if not self.session.model:
            self.session.model = config.model
        # 第四章：Agent Loop 相关状态（规划模式开关、用户取消信号）
        self.plan_state = PlanModeState()
        self.cancel_event = asyncio.Event()
        # 第六章：五层权限防御（默认 default 模式）；`/permissions` 命令可切换模式
        self.permission = permission or PermissionChecker(workdir=self.workdir, mode=permission_mode)
        # 第七章：MCP 开放工具生态（连接缓存、工具注册）
        self.mcp = mcp_manager or MCPManager(workdir=self.workdir)
        # 第八章：上下文管理（两层压缩；状态挂在 Session 上跨轮保持）
        self.context = context or ContextManager(config, workdir=self.workdir)
        # 第 9 章：跨会话记忆（指令文件加载 + 自动记忆管理）
        self.memory = memory or MemoryManager(self.workdir)
        self.memory_client = memory_client
        self.auto_memory = auto_memory
        self._memory_tasks: list[asyncio.Task] = []
        # 第 10 章：命令框架注册中心（10 核心命令 + App 扩展命令）
        self.commands = self._build_command_registry()
        # 第 12 章：Hook 系统（生命周期事件自动化；配置加载失败只告警不阻断启动）
        try:
            self.hooks = HookEngine(load_hooks(workdir=self.workdir))
        except Exception as exc:  # noqa: BLE001 - 配置错误不阻止终端可用
            self.terminal.error(f"Hook 配置加载失败，已跳过：{exc}")
            self.hooks = HookEngine()
        # 第 11 章：Skill 系统（加载器 / 激活状态 / 执行器 / LoadSkill 工具）
        from .skills import SkillHost

        self.skill_host = SkillHost(self.workdir)
        self.skill_host.bind(self, self.registry)
        self.skill_host.register_command(self.commands)
        # 第 13 章：SubAgent 系统（定义加载 / 后台任务 / 父子追踪 / Agent 工具）
        self.agent_loader = AgentLoader(project_root=self.workdir)
        self.task_manager = TaskManager()
        self.trace = TraceRegistry()
        self.agent_tool = AgentTool(
            self, self.agent_loader, self.task_manager, self.trace
        )
        if "Agent" not in self.registry.names():
            self.registry.register(self.agent_tool)
            self.registry.register(TaskListTool(self.task_manager))
            self.registry.register(TaskGetTool(self.task_manager))
        # 第 14 章：Worktree 文件系统隔离（生命周期管理 + 会话持久化）。
        # 目录不是 git 仓库时 manager 仍可用，git 操作在调用时报错
        from .worktree import WorktreeManager, load_worktree_settings

        wt_settings = load_worktree_settings(self.workdir)
        self.worktree_settings = wt_settings
        self.worktree_manager = WorktreeManager(
            repo_root=self.workdir,
            symlink_directories=wt_settings.symlink_directories,
            worktree_dir=wt_settings.worktree_dir or None,
        )
        restored_session = self.worktree_manager.restore_session()
        if restored_session is not None:
            # 上次进程退出时仍在 worktree 中：把工具根目录切回去继续工作
            self._swap_registry_root(restored_session.worktree_path)
            self.terminal.write(
                f"已恢复 Worktree 会话：{restored_session.worktree_name}（{restored_session.worktree_path}）"
            )
        # 第 15 章：Agent Teams——团队生命周期管理 + 顶层团队工具。
        # TeamCreate/TeamDelete/SendMessage 常驻主 Agent 工具集（Lead 的工具）；
        # 共享任务工具只注入队员工具池与 Coordinator 模式的收窄工具集
        from .teams import SendMessageTool, TeamCreateTool, TeamDeleteTool, TeamManager, TeammateSpawner

        self.team_manager = TeamManager(worktree_manager=self.worktree_manager)
        self.team_create_tool = TeamCreateTool(self.team_manager)
        self.team_delete_tool = TeamDeleteTool(self.team_manager)
        self.team_send_tool = SendMessageTool(self.team_manager, host=self, allow_resume=True)
        for team_tool in (self.team_create_tool, self.team_delete_tool, self.team_send_tool):
            if team_tool.name not in self.registry.names():
                self.registry.register(team_tool)
        self.team_spawner = TeammateSpawner(self, self.agent_tool)
        # 第 14 章：后台过期清理任务（run_async 启动，退出时取消）
        self._worktree_cleanup_task: asyncio.Task | None = None
        # 客户端携带注册中心启用的工具描述
        if isinstance(self.client, OpenAICompatibleClient) and self.client.tools is None:
            self.client.tools = self.registry.api_specs()
        # 供测试等待输出就绪（首行启动文案写出后置位）
        self.terminal_ready = terminal_ready if terminal_ready is not None else asyncio.Event()
        # 第 13 章：当前运行的主 Agent 引用（AgentTool 从其继承资源；_ask 时设置）
        self.current_agent = None

    def request_cancel(self) -> None:
        """用户取消：把信号传给 Agent Loop，循环在下一轮开始前干净退出。"""
        self.cancel_event.set()

    async def setup_mcp(self) -> None:
        """启动时后台连接所有配置的 MCP Server 并注册工具。

        部分 Server 连接失败不阻止启动，只打警告——用户配置了多个 Server，
        其中一个 Token 过期不应影响其他 Server 的正常使用。
        """
        if not self.mcp.configs:
            return
        failed = await self.mcp.register_all_tools(self.registry)
        # 工具注册后同步客户端工具描述
        self._refresh_client_tools()
        connected = self.mcp.connected_servers()
        if connected:
            self.terminal.write(
                f"MCP 已连接 {len(connected)} 个 Server，注册 {self.mcp.registered_count()} 个工具：{', '.join(connected)}"
            )
        if failed:
            self.terminal.error(f"MCP 连接失败：{', '.join(failed)}（不影响其他功能）")

    def _save(self) -> None:
        self.storage.save(self.session)

    def _display_path(self, path: str) -> str:
        """UI 展示路径：workdir 内用相对路径，否则用绝对路径。"""
        if not path:
            return path
        try:
            rel = os.path.relpath(path, self.workdir)
            return rel if not rel.startswith("..") else path
        except ValueError:
            return path

    # ------------------------------------------------------------------
    # 第 9 章：跨会话记忆
    # ------------------------------------------------------------------

    def _instructions(self) -> str:
        """项目指令文件（MEWCODE.md 三层优先级 + @include），每次读取保证最新。"""
        return load_instructions(self.workdir)

    def _remember_prompt(self, text: str) -> bool:
        """显式记忆：用户输入以「记住」开头时，直接把内容写入记忆文件。

        返回 True 表示已处理（不再进入 Agent 对话）。
        """
        if not text.startswith(("记住", "请记住", "帮我记住")):
            return False
        try:
            written = self.memory.remember(text)
        except MemoryManagerError as exc:
            self.terminal.error(f"记忆写入失败：{exc}")
            return True
        if written:
            self.terminal.write(f"✓ 已记住：{text}")
        return True

    def _schedule_memory_extraction(self) -> None:
        """每轮 Agent Loop 结束后后台异步回顾本轮对话（不阻塞用户下一轮输入）。

        达到提取频率阈值才真正调用 LLM；提取使用独立客户端（不带工具定义），
        避免与对话流互相干扰。任务句柄保存在 App 上，退出时统一清理。
        """
        if not self.auto_memory:
            return
        if not self.memory.should_extract(len(self.session.messages)):
            return
        messages = list(self.session.messages)
        client = self.memory_client or OpenAICompatibleClient(self.config, tools=None)
        self.terminal.write("（正在后台回顾对话，提取值得长期记忆的信息…）")

        async def _extract() -> None:
            try:
                result = await self.memory.extract(client, messages)
                if result and "失败" not in result:
                    self.terminal.write(f"✓ 记忆已更新：{result}")
            except Exception as exc:  # noqa: BLE001 - 后台提取失败不影响主流程
                self.terminal.write(f"（记忆提取跳过：{exc}）")

        self._memory_tasks.append(asyncio.create_task(_extract()))

    def _cancel_memory_tasks(self) -> None:
        """退出时取消尚未完成的记忆提取任务，避免事件循环关闭时泄漏。"""
        for task in self._memory_tasks:
            task.cancel()
        self._memory_tasks.clear()

    # ------------------------------------------------------------------
    # 会话恢复
    # ------------------------------------------------------------------

    def _apply_resume_checks(self, session_id: str) -> None:
        """恢复会话时做两件事：token 超限提示 + 时间跨度提示。

        时间跨度提示：距上次活跃超过 24 小时时插入一条提醒消息，
        避免 Agent 拿着过期的文件内容做决策。
        """
        meta = self.storage.load_meta(session_id)
        if meta is None:
            return
        gap_message = build_time_gap_message(meta.last_active)
        if gap_message is not None:
            self.session.messages.insert(0, gap_message)
            self._save()
            self.terminal.write(f"⏱ 距离上次会话已过去 {_gap_text(meta.last_active)}，已插入时间跨度提醒。")

    # ------------------------------------------------------------------
    # 第 9 章命令
    # ------------------------------------------------------------------

    def _handle_session_command(self, command) -> None:
        """/session：会话管理（list / delete / path）。"""
        argument = command.argument.strip()
        subcommand = argument.split(maxsplit=1)
        action = subcommand[0].lower() if subcommand else "list"
        if action in ("list", ""):
            metas = self.storage.list_metas()
            if not metas:
                self.terminal.write("暂无历史会话。")
                return
            self.terminal.write(f"会话目录：{self.storage.directory}")
            for meta in metas:
                marker = " *" if meta.id == self.session.id else ""
                title = f"（{meta.title[:24]}）" if meta.title else ""
                self.terminal.write(
                    f"{meta.id}  {meta.last_active.isoformat()}  {meta.model}{title}{marker}"
                )
        elif action == "delete":
            if len(subcommand) < 2:
                self.terminal.error(command_usage(command))
                return
            session_id = subcommand[1]
            if session_id == self.session.id:
                self.terminal.error("不能删除当前会话。")
                return
            removed = self.storage.delete(session_id)
            self.terminal.write("已删除会话。") if removed else self.terminal.error("会话不存在。")
        elif action == "path":
            self.terminal.write(str(self.storage.directory))
        else:
            self.terminal.error(command_usage(command))

    def _handle_memory_command(self, command) -> None:
        """/memory：查看/清空/定位自动记忆。"""
        argument = command.argument.strip()
        action = argument.split(maxsplit=1)[0].lower() if argument else "show"
        if action == "show":
            view = self.memory.view()
            self.terminal.write(view if view else "暂无记忆。")
        elif action == "clear":
            removed = self.memory.clear()
            self.terminal.write(f"已清空 {removed} 条记忆。")
        elif action == "path":
            self.terminal.write(f"用户级：{self.memory.user_dir}")
            self.terminal.write(f"项目级：{self.memory.project_dir}")
        else:
            self.terminal.error(command_usage(command))

    async def _handle_resume_command(self, command) -> None:
        """/resume：展示会话列表，让用户选择恢复（输入编号）。"""
        metas = self.storage.list_metas()
        if not metas:
            self.terminal.write("暂无历史会话。")
            return
        self.terminal.write("历史会话（输入编号恢复，回车取消）：")
        for index, meta in enumerate(metas, start=1):
            marker = " *" if meta.id == self.session.id else ""
            title = f"（{meta.title[:24]}）" if meta.title else ""
            self.terminal.write(f"{index}. {meta.id}  {meta.last_active.isoformat()}{title}{marker}")
        try:
            answer = self.terminal.input_fn("选择会话编号：").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not answer:
            return
        try:
            index = int(answer) - 1
            meta = metas[index]
        except (ValueError, IndexError):
            self.terminal.error("无效的编号。")
            return
        try:
            self.session = self.storage.load(meta.id)
        except SessionStorageError as exc:
            self.terminal.error(str(exc))
            return
        if not self.session.model:
            self.session.model = self.config.model
        self.context.reset()  # 第八章：切换会话后冻结状态随之重置
        self._apply_resume_checks(meta.id)
        self.terminal.write(f"已恢复到会话：{meta.id}（共 {len(self.session.messages)} 条消息）")

    def _refresh_client_tools(self) -> None:
        """Play Mode 切换后，同步客户端携带的工具描述。"""
        if isinstance(self.client, OpenAICompatibleClient):
            self.client.tools = (
                build_plan_mode_tools(self.registry) if self.plan_state.active else self.registry.api_specs()
            )

    def _report_loop_stop(self, event: LoopCompleteEvent) -> None:
        reason = event.stop_reason
        if reason == AgentStopReason.MAX_ITERATIONS:
            self.terminal.error(f"已达到工具调用轮数上限（{self.max_iterations} 轮），已自动停止。")
        elif reason == AgentStopReason.USER_CANCELLED:
            self.terminal.write("已取消本次操作。")
        elif reason == AgentStopReason.TIMEOUT:
            self.terminal.error("Agent 循环超时，已自动停止。")
        elif reason == AgentStopReason.TOOL_ERROR_LIMIT:
            self.terminal.error("模型连续工具调用失败，已提前终止。")
        elif reason == AgentStopReason.INTERNAL_ERROR:
            self.terminal.write("模型请求失败，本轮对话已终止。")

    # ------------------------------------------------------------------
    # 第 10 章：命令框架
    # ------------------------------------------------------------------

    def _build_command_registry(self) -> CommandRegistry:
        """构建 App 版命令注册中心。

        十个核心命令来自框架（build_default_registry），App 扩展命令
        （new/sessions/use/init/mcp/exit/resume）在此注册。handler 以
        CommandContext 注入 App 依赖，实际执行仍走各自的同步分发方法
        （终端版输入循环是同步的）；PROMPT 类型命令在 run_async 里 await。
        """
        registry = build_default_registry()

        def _ctx(args: str) -> CommandContext:
            return CommandContext(
                args=args,
                session=self.session,
                session_manager=self.storage,
                memory_manager=self.memory,
                ui=TerminalUIController(self),
                config={
                    "app": self,
                    "registry": registry,
                    "workdir": self.workdir,
                    "version": f"v0.1.0",
                    "mode": self.permission.mode.value if self.permission else "default",
                    "permission": self.permission,
                    "plan_state": self.plan_state,
                    "mcp": self.mcp,
                    "context": self.context,
                },
            )

        # App 扩展命令（同步 handler：直接执行 App 的同步分发逻辑）
        def _noop_new(ctx: CommandContext) -> None:
            self._save()
            self.session = Session(model=self.config.model)
            self.context.reset()
            self.terminal.write(f"已创建新会话：{self.session.id}")

        def _noop_sessions(ctx: CommandContext) -> None:
            self._handle_command(ParsedCommand("sessions"))

        def _noop_use(ctx: CommandContext) -> None:
            self._handle_command(ParsedCommand("use", ctx.args))

        def _noop_init(ctx: CommandContext) -> None:
            self._handle_command(ParsedCommand("init"))

        def _noop_mcp(ctx: CommandContext) -> None:
            self._handle_mcp_command()

        def _noop_exit(ctx: CommandContext) -> None:
            self._save()

        def _noop_resume(ctx: CommandContext) -> None:
            self.terminal.write("请使用 /resume 交互选择会话（当前在终端版）。")

        registry.register_sync(
            Command(name="new", description="新建会话", type=CommandType.LOCAL_UI,
                    handler=_noop_new, usage="/new")
        )
        registry.register_sync(
            Command(name="sessions", description="列出历史会话", type=CommandType.LOCAL,
                    handler=_noop_sessions, usage="/sessions")
        )
        registry.register_sync(
            Command(name="use", description="切换到指定会话", type=CommandType.LOCAL,
                    handler=_noop_use, usage="/use <会话ID>", arg_prompt="用法：/use <会话ID>")
        )
        registry.register_sync(
            Command(name="init", description="初始化 MEWCODE.md", type=CommandType.LOCAL,
                    handler=_noop_init, usage="/init")
        )
        registry.register_sync(
            Command(name="mcp", description="查看 MCP Server 状态", type=CommandType.LOCAL,
                    handler=_noop_mcp, usage="/mcp")
        )
        registry.register_sync(
            Command(name="exit", description="保存并退出", type=CommandType.LOCAL_UI,
                    handler=_noop_exit, usage="/exit", hidden=True)
        )
        registry.register_sync(
            Command(name="resume", description="恢复历史会话", type=CommandType.LOCAL,
                    handler=_noop_resume, usage="/resume")
        )
        registry.register_sync(
            Command(name="skill", description="Skill 管理（list/info/reload）", type=CommandType.LOCAL,
                    handler=lambda ctx: self._handle_skill_command(ParsedCommand("skill", ctx.args)),
                    usage="/skill [list|info <名称>|reload]")
        )
        registry.register_sync(
            Command(name="tasks", description="列出后台子 Agent 任务", type=CommandType.LOCAL,
                    handler=lambda ctx: self._handle_tasks_command(ParsedCommand("tasks", ctx.args)),
                    usage="/tasks")
        )
        registry.register_sync(
            Command(name="task", description="后台任务详情（info/cancel）", type=CommandType.LOCAL,
                    handler=lambda ctx: self._handle_task_command(ParsedCommand("task", ctx.args)),
                    usage="/task info <ID> | /task cancel <ID>")
        )
        registry.register_sync(
            Command(name="worktree", description="Git Worktree 管理（list/create/enter/exit/remove）",
                    type=CommandType.LOCAL_UI,
                    handler=lambda ctx: self._handle_worktree_command(ParsedCommand("worktree", ctx.args)),
                    usage=("/worktree [list|create <名称>|enter <名称>"
                           "|exit [remove] [--discard]|remove <名称> [--discard]]"))
        )
        registry.register_sync(
            Command(name="teams", description="列出 Agent 团队与队员状态", type=CommandType.LOCAL,
                    handler=lambda ctx: self._handle_teams_command(), usage="/teams")
        )
        return registry

    def _handle_status_command(self) -> None:
        """第 10 章：/status 显示综合状态信息（模式/Token/工具/记忆/目录/版本）。"""
        lines = ["MewCode 状态", "─────────────"]
        lines.append(f"模式：{self.permission.mode.value if self.permission else 'default'}")
        try:
            estimate = self.context.estimate_payload("", self.session)
            window = self.context.config.context_window
            lines.append(f"Token：{estimate:,} / {window:,}（{estimate / window * 100:.0f}%）")
        except Exception:
            lines.append("Token：-")
        lines.append(f"工具：{len(self.registry.api_specs())} 个已启用")
        try:
            user_n = len(self.memory.list_memory_files(self.memory.user_dir))
            project_n = len(self.memory.list_memory_files(self.memory.project_dir))
            lines.append(f"记忆：user {user_n} 条，project {project_n} 条")
        except Exception:
            lines.append("记忆：-")
        lines.append(f"工作目录：{self.workdir}")
        lines.append(f"版本：v0.1.0")
        self.terminal.write("\n".join(lines))

    def _handle_command(self, command) -> bool:
        # 第 10 章：以注册中心为命令合法性的唯一来源（未知命令统一引导到 /help）
        if self.commands.find(command.name) is None:
            self.terminal.error(command_usage(command))
            return True
        if command.name == "exit":
            self._save()
            return False
        if command.name == "new":
            self._save()
            self.session = Session(model=self.config.model)
            self.context.reset()  # 第八章：决策冻结按会话隔离
            self.skill_host.state.clear()  # 第 11 章：激活的 Skill 跟着对话走
            self.terminal.write(f"已创建新会话：{self.session.id}")
            return True
        if command.name == "clear":
            self.session.clear()
            self._save()
            self.skill_host.state.clear()  # 第 11 章：清理激活的 Skill（避免 SOP 残留）
            self.terminal.write("当前会话已清空。")
            return True
        if command.name == "sessions":
            sessions = self.storage.list_sessions()
            if not sessions:
                self.terminal.write("暂无历史会话。")
            else:
                for item in sessions:
                    marker = " *" if item.id == self.session.id else ""
                    self.terminal.write(f"{item.id}  {item.updated_at}  {item.model}{marker}")
            return True
        if command.name == "use":
            if not command.argument:
                self.terminal.error(command_usage(command))
                return True
            try:
                self.session = self.storage.load(command.argument)
            except SessionStorageError as exc:
                self.terminal.error(str(exc))
            else:
                self.context.reset()  # 第八章：切换会话后冻结状态随之重置
                self._apply_resume_checks(command.argument)
                self.terminal.write(f"已切换到会话：{self.session.id}")
            return True
        if command.name == "plan":
            # 第四章 Plan Mode：只读探索，唯一可写的是 plan 文件
            if self.plan_state.active:
                self.terminal.write(f"已在规划模式。计划文件：{self.plan_state.plan_file}")
            else:
                path = self.plan_state.enter(self.workdir)
                self._refresh_client_tools()
                self.terminal.write(f"已进入规划模式（只读探索）。计划文件：{path}")
            return True
        if command.name == "do":
            if not self.plan_state.active:
                self.terminal.write("当前不在规划模式。")
            else:
                self.plan_state.exit()
                self._refresh_client_tools()
                self.terminal.write("已退出规划模式，恢复正常工具。")
            return True
        if command.name in ("permissions", "permission", "perm"):
            self._handle_permissions_command(command)
            return True
        if command.name in ("status", "s"):
            self._handle_status_command()
            return True
        if command.name == "mcp":
            self._handle_mcp_command()
            return True
        if command.name == "session":
            self._handle_session_command(command)
            return True
        if command.name == "memory":
            self._handle_memory_command(command)
            return True
        if command.name == "status":
            self._handle_status_command()
            return True
        if command.name == "resume":
            # /resume 需要异步读取输入并切换会话，走 run_async 分支
            self.terminal.write("请使用 /resume 交互选择会话。")
            return True
        if command.name == "review":
            # PROMPT 类型命令：触发 Agent 执行预设 prompt（异步，走 run_async 分支）
            self.terminal.write("正在发送代码审查请求…")
            return True
        if command.name in ("compact", "c"):
            # /compact 需异步调用 LLM 生成摘要，run_async 里 await；
            # 这里先做无需 LLM 的前置检查（空会话直接提示）
            if not self.session.messages:
                self.terminal.write("当前会话还没有内容，无需压缩。")
                return True
            self.terminal.write("正在压缩上下文…")
            return True
        if command.name == "skill":
            # /skill 需访问加载器与执行器，直接走同步方法
            self._handle_skill_command(command)
            return True
        if command.name == "tasks":
            # 第 13 章：列出后台子 Agent 任务
            self._handle_tasks_command(command)
            return True
        if command.name == "task":
            # 第 13 章：后台任务详情 / 取消
            self._handle_task_command(command)
            return True
        if command.name == "teams":
            # 第 15 章：列出团队与队员状态
            self._handle_teams_command()
            return True
        if command.name == "help":
            self._handle_help_command()
            return True
        if command.name == "init":
            path = "MEWCODE.md"
            if os.path.exists(path):
                self.terminal.write(f"{path} 已存在。")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# MEWCODE.md\n\n项目说明与指令文件。\n")
                self.terminal.write(f"已创建 {path} 文件。")
            return True
        return True

    def _handle_help_command(self) -> None:
        """/help：从注册中心列出所有可用命令（名称、别名、说明）。"""
        lines = ["可用命令："]
        # 按注册顺序展示，别名来自框架命令定义
        rows = []
        for cmd in self.commands.list_commands():
            if cmd.name == "exit":
                continue  # 隐藏命令不展示
            names = [f"/{cmd.name}"] + [f"/{a}" for a in cmd.aliases]
            rows.append((", ".join(names), cmd.description))
        for name, desc in rows:
            lines.append(f"  {name:<30} {desc}")
        lines.append("")
        lines.append("输入 /help <命令名> 查看详细用法。")
        self.terminal.write("\n".join(lines))

    def _handle_permissions_command_option(self, mode: str) -> None:
        """供框架 permission 命令 mode 子命令使用的切换入口。"""
        self._handle_permissions_command(ParsedCommand("permissions", mode))

    def _handle_permissions_command(self, command) -> None:
        """/permissions 命令：查看或切换权限模式（第 4 层权限模式）。

        无参数时展示当前模式与规则统计；带参数时切换模式。
        支持两种写法：/permissions acceptEdits（旧）或 /permission mode acceptEdits（框架）。
        """
        argument = command.argument.strip()
        if not argument:
            rules = self.permission.rules
            self.terminal.write(
                f"当前权限模式：{self.permission.mode.value}（本地规则 {len(rules.rules.local)} 条 / "
                f"项目规则 {len(rules.rules.project)} 条 / 用户规则 {len(rules.rules.user)} 条）"
            )
            return
        # 兼容子命令前缀：permission mode <模式>
        if argument.startswith("mode "):
            argument = argument[5:].strip()
        mode = parse_mode(argument)
        if mode == self.permission.mode:
            self.terminal.write(f"当前权限模式已是 {mode.value}。")
            return
        self.permission.set_mode(mode)
        self.terminal.write(f"权限模式已切换为：{mode.value}")
        self.terminal.write(
            f"可用模式：{', '.join(m.value for m in PermissionMode)}（别名 yolo = bypassPermissions）"
        )

    def _prompt_permission(self, event: PermissionRequestEvent) -> PermissionResponse:
        """第 5 层 HITL：渲染确认对话框并读取用户按键。

        支持三种回答：
          y / 1  是（本次允许）
          n / 3  否（拒绝）
          a / 2  始终允许（自动生成 allow 规则）
        """
        self.terminal.write()
        self.terminal.write(f"[{event.tool_name}] {event.description}")
        while True:
            answer = self.terminal.input_fn("允许执行？(y)是 / (n)否 / (a)始终允许：").strip().lower()
            if answer in ("y", "1", "yes"):
                return PermissionResponse.ALLOW
            if answer in ("a", "2", "always"):
                return PermissionResponse.ALLOW_ALWAYS
            if answer in ("n", "3", "no"):
                return PermissionResponse.DENY
            self.terminal.error("无效输入，请输入 y / n / a（或 1 / 2 / 3）。")

    def _handle_mcp_command(self) -> None:
        """/mcp 命令：查看 MCP Server 连接状态与注册的工具。"""
        if not self.mcp.configs:
            self.terminal.write("未配置任何 MCP Server（项目级 .mewcode/config.yaml 或用户级 ~/.mewcode/config.yaml）。")
            return
        connected = self.mcp.connected_servers()
        self.terminal.write(
            f"已配置 {len(self.mcp.configs)} 个 MCP Server：{', '.join(self.mcp.configs)}"
        )
        if connected:
            for name in connected:
                client = self.mcp.get_client(name)
                tool_names = client.tool_names() if client else []
                self.terminal.write(f"  ✓ {name}：{len(tool_names)} 个工具（{', '.join(tool_names)}）")
        if self.mcp.failures:
            for name, reason in self.mcp.failures.items():
                self.terminal.write(f"  ✗ {name}：{reason}")
        if not connected and not self.mcp.failures:
            self.terminal.write("MCP Server 尚未连接。")

    # ------------------------------------------------------------------
    # 第 14 章：Worktree 文件系统隔离
    # ------------------------------------------------------------------

    def _swap_registry_root(self, new_workdir: str) -> None:
        """把工具注册中心的根目录切到 new_workdir（进入/退出 worktree 时）。

        本实现的「显式 cwd」机制：核心文件工具在构造时固定根目录，
        重建注册中心即完成切换；Agent/Task/LoadSkill/MCP 等与目录无关
        的工具实例直接复用。切换后同步客户端工具描述。
        """
        from .worktree import reroot_registry

        self.registry = reroot_registry(self.registry, new_workdir)
        self._refresh_client_tools()

    async def _handle_worktree_command(self, command) -> None:
        """/worktree：Git Worktree 手动管理（list/create/enter/exit/remove）。"""
        from .worktree import WorktreeError

        parts = command.argument.split() if command.argument else []
        sub = parts[0].lower() if parts else "list"
        manager = self.worktree_manager

        try:
            if sub == "list":
                self._worktree_list(manager)
                return
            if sub == "create":
                if len(parts) < 2:
                    self.terminal.error("用法：/worktree create <名称>")
                    return
                wt = await manager.create(parts[1], "HEAD")
                self.terminal.write(
                    f"已创建 worktree：{wt.name}\n  目录：{wt.path}\n  分支：{wt.branch}"
                )
                return
            if sub == "enter":
                if len(parts) < 2:
                    self.terminal.error("用法：/worktree enter <名称>")
                    return
                session = await manager.enter(parts[1], session_id=self.session.id)
                self._swap_registry_root(session.worktree_path)
                self.terminal.write(
                    f"已进入 worktree：{session.worktree_name}（{session.worktree_path}）\n"
                    f"后续文件操作都发生在 worktree 中；/worktree exit 返回主目录。"
                )
                return
            if sub == "exit":
                if manager.current_session is None:
                    self.terminal.write("当前不在任何 worktree 中。")
                    return
                action = "remove" if len(parts) > 1 and parts[1].lower() == "remove" else "keep"
                discard = "--discard" in parts[1:]
                name = manager.current_session.worktree_name
                wt_path = manager.current_session.worktree_path
                await manager.exit(name, action=action, discard_changes=discard)
                self._swap_registry_root(self.workdir)
                if action == "remove":
                    self.terminal.write(f"已退出并删除 worktree：{name}（{wt_path}）")
                else:
                    self.terminal.write(
                        f"已退出 worktree：{name}（目录保留在 {wt_path}，可再次 enter 或手动 remove）"
                    )
                return
            if sub == "remove":
                if len(parts) < 2:
                    self.terminal.error("用法：/worktree remove <名称> [--discard]")
                    return
                discard = "--discard" in parts[2:]
                await manager.remove(parts[1], discard_changes=discard)
                if manager.current_session is None:
                    self._swap_registry_root(self.workdir)
                self.terminal.write(f"已删除 worktree：{parts[1]}")
                return
            self.terminal.error(
                "用法：/worktree [list|create <名称>|enter <名称>|exit [remove] [--discard]"
                "|remove <名称> [--discard]]"
            )
        except WorktreeError as exc:
            self.terminal.error(f"Worktree 操作失败：{exc}")

    def _worktree_list(self, manager) -> None:
        """/worktree list：列出所有 worktree 与当前所在位置。"""
        session = manager.current_session
        worktrees = manager.list_worktrees()
        if not worktrees:
            self.terminal.write("当前没有 worktree。用 /worktree create <名称> 创建。")
            return
        lines = ["Worktrees", "─────────────"]
        for wt in worktrees:
            marker = ""
            if session is not None and session.worktree_path == wt.path:
                marker = "（当前所在）"
            elif session is not None and session.worktree_name == wt.name:
                marker = "（当前所在）"
            lines.append(f"  {wt.name}  分支 {wt.branch}{marker}")
            lines.append(f"    {wt.path}")
        self.terminal.write("\n".join(lines))


    async def _handle_compact_command(self) -> None:
        """第 8 章：/compact 手动压缩——显示压缩前后的 Token 变化。

        用户的预防性压缩与话题切换场景：在对话累积到自动压缩阈值前，
        主动清理掉旧细节，为接下来的大任务腾出空间。
        """
        if not self.session.messages:
            self.terminal.write("当前会话还没有内容，无需压缩。")
            return
        try:
            outcome = await self.context.compact(self.session)
        except CompactError as exc:
            self.terminal.error(f"压缩失败：{exc}")
            return
        if not outcome.performed:
            self.terminal.write("当前会话内容不多，无需压缩。")
            return
        self.terminal.write(
            f"已压缩：{outcome.before_tokens:,} → {outcome.after_tokens:,} token"
            f"（释放 {max(0, outcome.before_tokens - outcome.after_tokens):,}）"
        )
        # 第 12 章：compact 事件——手动 /compact 压缩成功同样触发
        if self.hooks is not None:
            await self.hooks.run_hooks(
                LifecycleEvent.COMPACT,
                HookContext(
                    event_name=LifecycleEvent.COMPACT,
                    message=(
                        f"手动压缩：{outcome.before_tokens:,} → {outcome.after_tokens:,} token"
                        f"（释放 {max(0, outcome.before_tokens - outcome.after_tokens):,}）"
                    ),
                ),
            )
        self._save()

    async def _ask(self, text: str) -> None:
        """第四章：以 Agent Loop 引擎驱动一轮用户消息，事件流驱动 UI 渲染。"""
        instructions = load_instructions(self.workdir)
        memory_index = self.memory.load_index() if self.auto_memory else ""
        # 第 11 章：Skill 两阶段加载阶段一——轻量摘要列表注入 system prompt
        skill_catalog = self.skill_host.loader.catalog_text() if getattr(self.skill_host, "loader", None) else ""
        # 第 15 章：Coordinator Mode 双锁——feature flag（config.yaml）+ 环境变量。
        # 激活后收窄本轮 Agent 的工具集（无 WriteFile/EditFile）并注入四阶段工作流提示词
        registry_for_run = self.registry
        if self._coordinator_mode_active():
            registry_for_run = self._build_coordinator_registry()
            instructions = (
                instructions
                + "\n\n"
                + get_coordinator_system_prompt()
                + "\n"
                + get_coordinator_user_context()["workerToolsContext"]
            )
        # 第 12 章：turn_start 事件——用户发送新消息时触发
        if self.hooks is not None:
            await self.hooks.run_hooks(
                LifecycleEvent.TURN_START, HookContext(event_name=LifecycleEvent.TURN_START, message=text)
            )
        agent = Agent(
            self.config,
            session=self.session,
            registry=registry_for_run,
            workdir=self.workdir,
            client=self.client,
            agent_config=AgentConfig(
                max_iterations=self.max_iterations,
                custom_instructions=instructions,
                memory_instructions=memory_index,
                skill_catalog=skill_catalog,
            ),
            plan_state=self.plan_state,
            cancel_event=self.cancel_event,
            permission=self.permission,
            context=self.context,
            hooks=self.hooks,
        )
        # 第 11 章：绑定 Skill 激活状态 + allowedTools 过滤（env context 每轮重建）
        agent.bind_skill_context(self.skill_host.state, tool_filter=self._skill_tool_filter)
        # 第 15 章：每轮循环开头消费 Lead 邮箱（队员空闲通知 / 消息注入 system reminder）
        agent.pre_turn_inject = self._drain_lead_mailbox
        # 第 13 章：登记当前主 Agent（AgentTool 从它继承资源），退出时清除
        self.current_agent = agent
        # 工具卡片标签：tool_use_id -> 渲染行
        pending_labels: dict[str, str] = {}
        spinner_task: asyncio.Task | None = None

        async def _spin() -> None:
            frames = itertools.cycle(SPINNER_FRAMES)
            while True:
                if pending_labels and self.terminal.interactive:
                    line = "  ".join(f"{label} {next(frames)}" for label in pending_labels.values())
                    self.terminal.raw(f"\r{line}")
                await asyncio.sleep(0.1)

        try:
            async for event in agent.run(text):
                if isinstance(event, TextChunkEvent):
                    self.terminal.stream_text(event.text)
                elif isinstance(event, ToolUseEvent):
                    pending_labels[event.id] = self.terminal.tool_call(event.name, event.input)
                    if spinner_task is None and self.terminal.interactive:
                        spinner_task = asyncio.create_task(_spin())
                elif isinstance(event, ToolResultEvent):
                    label = pending_labels.pop(event.tool_use_id, "")
                    result = ToolResult(
                        is_error=event.is_error, output=event.output, error=event.error, duration=event.duration
                    )
                    if label:
                        self.terminal.tool_finish(label, result)
                    elif event.is_error:
                        self.terminal.error(event.error)
                    if event.persisted:
                        # 第 8 章：超大结果已存盘，卡片下方给出文件位置供用户查看
                        self.terminal.write(f"  ⤷ 输出过大，完整内容已保存：{self._display_path(event.persisted_path)}")
                elif isinstance(event, PermissionRequestEvent):
                    # 第 5 层 HITL：渲染确认对话框，阻塞等待用户回复后唤醒 Agent Loop
                    response = self._prompt_permission(event)
                    event.response_future.set_result(response)
                elif isinstance(event, ContextCompactedEvent):
                    # 第 8 章：自动/紧急压缩后展示 Token 变化（手动压缩走 /compact 命令）
                    label = {"auto": "自动压缩", "manual": "手动压缩", "force": "紧急压缩"}.get(
                        event.reason, event.reason
                    )
                    self.terminal.write(f"（{label}：{event.before_tokens:,} → {event.after_tokens:,} token）")
                elif isinstance(event, ErrorEvent):
                    self.terminal.error(event.message)
                elif isinstance(event, LoopCompleteEvent):
                    self._report_loop_stop(event)
        finally:
            if spinner_task is not None:
                spinner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await spinner_task
            self.current_agent = None
            self._save()
            self._schedule_memory_extraction()
            # 第 13 章：轮次结束后拉取已完成的子 Agent 任务，把结果
            # 以 task-notification 形式注入主对话（不打断当前对话）。
            # 结果回流后，模型下轮可见子 Agent 的产出，用户也可以要求
            # 展示或基于它继续工作。
            self._collect_task_notifications()
            # 第 12 章：turn_end 事件——Agent 完成回复时触发
            if self.hooks is not None:
                await self.hooks.run_hooks(
                    LifecycleEvent.TURN_END, HookContext(event_name=LifecycleEvent.TURN_END, message=text)
                )

    def _collect_task_notifications(self) -> None:
        """把已完成的后台子 Agent 结果注入主对话并提示（异步回传）。"""
        completed = self.task_manager.poll_completed()
        if not completed:
            return
        from .session import TextBlock

        for task in completed:
            if not task.result:
                continue
            notice = (
                f"<task-notification>\n"
                f"后台任务 {task.name}（{task.id}）已完成，状态：{task.status}。\n"
                f"结果：\n{task.result}\n"
                f"</task-notification>"
            )
            self.session.add_message("user", notice, blocks=[TextBlock(text=notice)])
            self.terminal.write(f"⏱ 后台任务 {task.name}（{task.id}）已完成：{task.status}")

    # ------------------------------------------------------------------
    # 第 15 章：Agent Teams——Coordinator Mode / Lead 邮箱 / 团队命令
    # ------------------------------------------------------------------

    def _coordinator_mode_active(self) -> bool:
        """双锁判定：feature flag（config.yaml）+ 环境变量（用户显式 opt-in）。"""
        from .teams import coordinator_env_enabled, coordinator_feature_enabled

        return coordinator_feature_enabled(self.workdir) and coordinator_env_enabled()

    def _lead_task_store_provider(self):
        """Lead 侧共享任务工具的 store provider：按当前活跃团队动态解析。"""
        from .teams import TeamError

        team_name = self.team_manager.active_team_name
        store = self.team_manager.get_task_store(team_name) if team_name else None
        if store is None:
            raise TeamError("当前没有活跃团队：先用 TeamCreate 创建团队。")
        return store

    def _build_coordinator_registry(self):
        """Coordinator 模式的收窄注册中心（不改动宿主完整工具集）。"""
        from .teams import build_coordinator_registry, build_team_task_tools

        team_tools: dict[str, Any] = {
            self.team_create_tool.name: self.team_create_tool,
            self.team_delete_tool.name: self.team_delete_tool,
            self.team_send_tool.name: self.team_send_tool,
        }
        for task_tool in build_team_task_tools(self._lead_task_store_provider):
            team_tools[task_tool.name] = task_tool
        return build_coordinator_registry(self.registry, team_tools)

    def _drain_lead_mailbox(self, turn: int) -> list[str]:
        """Agent Loop 每轮开头消费 Lead 邮箱（空闲通知 / 队员消息 → system reminder）。"""
        from .teams import format_mailbox_digest

        digest_parts: list[str] = []
        try:
            teams = self.team_manager.list_teams()
        except Exception:  # noqa: BLE001 - 邮箱消费失败不阻断对话
            return []
        for team in teams:
            mailbox = self.team_manager.get_mailbox(team.name)
            if mailbox is None:
                continue
            digest = format_mailbox_digest(mailbox.consume(team.lead_agent_id))
            if digest:
                digest_parts.append(f"[团队 {team.name}]\n{digest}")
        joined = "\n\n".join(digest_parts)
        return [joined] if joined else []

    def _handle_teams_command(self) -> None:
        """/teams：列出全部团队与队员状态。"""
        from .teams.models import TeammateInfo

        teams = self.team_manager.list_teams()
        if not teams:
            self.terminal.write("当前没有团队。对话里让 Agent 用 TeamCreate 创建。")
            return
        state_text = {True: "工作中", False: "空闲", None: "未开始"}
        for team in teams:
            self.terminal.write(
                f"团队 {team.name}（lead={team.lead_agent_id}）— {team.description or '（无描述）'}"
            )
            if not team.members:
                self.terminal.write("  （无队员）")
            for member in team.members:
                self.terminal.write(
                    f"  - {member.name} [{member.agent_id}] {member.agent_type} "
                    f"@{member.backend_type} {state_text[member.is_active]}"
                    f" worktree={member.worktree_path}"
                )

    async def run_async(self) -> None:
        # 第 12 章：startup + session_start 事件——MewCode 启动、新会话开始时触发
        if self.hooks is not None:
            await self.hooks.run_hooks(LifecycleEvent.STARTUP, HookContext(event_name=LifecycleEvent.STARTUP))
            await self.hooks.run_hooks(
                LifecycleEvent.SESSION_START, HookContext(event_name=LifecycleEvent.SESSION_START)
            )
        # 第 14 章：后台过期 worktree 清理任务（先 sleep 再清理，退出时取消）
        from .worktree import start_stale_cleanup_task

        self._worktree_cleanup_task = asyncio.create_task(
            start_stale_cleanup_task(
                self.worktree_manager,
                self.worktree_settings.cleanup_interval_seconds,
                self.worktree_settings.stale_cutoff_hours,
            )
        )
        self.terminal.write(f"MewCode 已启动，会话：{self.session.id}。输入 /exit 退出。")
        self.terminal_ready.set()
        # 第七章：启动时后台连接 MCP Server（失败不阻塞，只告警）
        await self.setup_mcp()
        try:
            while True:
                try:
                    text = self.terminal.prompt().strip()
                except (EOFError, KeyboardInterrupt):
                    self.terminal.write()
                    self._save()
                    self._cancel_memory_tasks()
                    return
                if not text:
                    continue
                # 第 9 章：显式记忆——「记住 X」直接写入记忆文件，不走 Agent 对话
                if self._remember_prompt(text):
                    continue
                command = parse_command(text)
                if command is not None:
                    # 第 12 章：command_execute 事件——Slash Command 执行时触发
                    if self.hooks is not None:
                        await self.hooks.run_hooks(
                            LifecycleEvent.COMMAND_EXECUTE,
                            HookContext(
                                event_name=LifecycleEvent.COMMAND_EXECUTE,
                                message=text,
                            ),
                        )
                    # 第 8 章：/compact 需异步调用 LLM 生成摘要，单独走异步分支
                    if command.name == "compact":
                        await self._handle_compact_command()
                    elif command.name == "resume":
                        await self._handle_resume_command(command)
                    elif command.name == "worktree":
                        # 第 14 章：Worktree 管理是异步操作（git 子进程），单独走异步分支
                        await self._handle_worktree_command(command)
                    elif command.name == "review":
                        # 第 11 章：/review 升级为 Skill（fork 模式，隔离上下文客观审查）
                        result = await self.skill_host.execute_skill("review", command.argument)
                        if result.text:
                            self.terminal.write(result.text)
                    elif self.skill_host.executor is not None and self._is_skill_command(command.name):
                        # 第 11 章：Skill 自动注册的 PROMPT 命令（/commit、/test …）
                        result = await self.skill_host.execute_skill(command.name, command.argument)
                        if result.text:
                            self.terminal.write(result.text)
                    elif not self._handle_command(command):
                        self._cancel_memory_tasks()
                        return
                    continue
                await self._ask(text)
        finally:
            # 第 12 章：shutdown + session_end 事件——退出时兜底触发（含 /exit 路径）
            if self.hooks is not None:
                await self.hooks.run_hooks(LifecycleEvent.SESSION_END, HookContext(event_name=LifecycleEvent.SESSION_END))
                await self.hooks.run_hooks(LifecycleEvent.SHUTDOWN, HookContext(event_name=LifecycleEvent.SHUTDOWN))
                self.hooks.shutdown()
            # 第 14 章：停掉后台过期清理任务
            if self._worktree_cleanup_task is not None:
                self._worktree_cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worktree_cleanup_task
                self._worktree_cleanup_task = None
            # 第 13 章：退出时取消所有仍在运行的后台子 Agent 任务
            self.task_manager.shutdown()
            self._collect_task_notifications()
            # 第 15 章：停掉所有仍在运行的 in-process 队员
            self.team_manager.shutdown()

    def _is_skill_command(self, name: str) -> bool:
        """判断某个命令名是否为 Skill 自动注册的命令（非内置命令）。"""
        cmd = self.commands.find(name)
        if cmd is None:
            return False
        # Skill 注册的命令描述带 [skill] 标记
        return "[skill]" in (cmd.description or "")


    def _handle_skill_command(self, command) -> None:
        """/skill：管理 Skill（list / info <名称> / reload）。"""
        parts = command.argument.split(maxsplit=1) if command.argument else []
        sub = parts[0].lower() if parts else "list"
        host = self.skill_host
        if sub == "list":
            skills = host.loader.load_catalog()
            if not skills:
                self.terminal.write("暂无 Skill。可放入 ~/.mewcode/skills/ 或 项目根/.mewcode/skills/。")
                return
            lines = ["已加载 Skill："]
            for skill in sorted(skills, key=lambda s: s.source_rank):
                marker = "项目" if skill.source_rank == 0 else "用户" if skill.source_rank == 1 else "内置"
                lines.append(f"  {skill.name:<16} [{marker}] {skill.description}")
            lines.append(f"（共 {len(skills)} 个）")
            self.terminal.write("\n".join(lines))
        elif sub == "info":
            if len(parts) < 2:
                self.terminal.write(command_usage(command))
                return
            name = parts[1].strip().lower()
            try:
                skill = host.loader.get(name)
            except Exception as exc:
                self.terminal.error(str(exc))
                return
            lines = [
                f"名称：{skill.name}",
                f"描述：{skill.description}",
                f"模式：{skill.mode}（context={skill.context}）",
                f"模型：{skill.model or '默认'}",
                f"来源：{skill.source}",
                f"允许工具：{', '.join(skill.allowed_tools) if skill.allowed_tools else '（全部）'}",
                "",
                "--- SOP ---",
                skill.prompt_body,
            ]
            self.terminal.write("\n".join(lines))
        elif sub == "reload":
            names = host.reload()
            self.terminal.write(f"已重新扫描 Skill：{', '.join(names) if names else '（无）'}")
        else:
            self.terminal.error(command_usage(command))

    def _handle_tasks_command(self, command) -> None:
        """第 13 章：/tasks——列出后台子 Agent 任务与调用链。"""
        tasks = self.task_manager.list_tasks()
        lines = [f"后台任务（共 {len(tasks)} 个）："]
        if not tasks:
            lines.append("  （无）")
        for task in tasks:
            elapsed = task.end_time - task.start_time if task.end_time is not None else None
            duration = f"{elapsed:.1f}s" if elapsed is not None else "进行中"
            tokens = task.progress.input_tokens + task.progress.output_tokens
            lines.append(f"  [{task.id}] {task.name} — {task.status}（{duration}，{tokens} token）")
        lines.append("")
        lines.append(self.trace.tree_text())
        self.terminal.write("\n".join(lines))

    def _handle_task_command(self, command) -> None:
        """/task：后台任务详情（info <ID>）/ 取消（cancel <ID>）。"""
        parts = command.argument.split(maxsplit=2) if command.argument else []
        if not parts or len(parts) < 2:
            self.terminal.error(command_usage(command))
            return
        action, task_id = parts[0].lower(), parts[1].strip()
        task = self.task_manager.get(task_id)
        if task is None:
            self.terminal.error(f"任务不存在：{task_id}")
            return
        if action in ("info", "show"):
            elapsed = task.end_time - task.start_time if task.end_time is not None else None
            duration = f"{elapsed:.1f}s" if elapsed is not None else "进行中"
            lines = [
                f"任务：{task.id}（{task.name}）",
                f"状态：{task.status}",
                f"耗时：{duration}",
                f"Token：{task.progress.input_tokens + task.progress.output_tokens}",
            ]
            if task.result:
                lines.append("--- 结果 ---")
                lines.append(task.result)
            self.terminal.write("\n".join(lines))
        elif action == "cancel":
            if task.status == "running":
                self.task_manager.cancel(task_id)
                self.terminal.write(f"已请求取消任务：{task_id}")
            else:
                self.terminal.write(f"任务 {task_id} 已结束（{task.status}），无需取消。")
        else:
            self.terminal.error(command_usage(command))

    def _skill_tool_filter(self, registry) -> Any:
        """allowedTools 过滤回调（Agent 每轮构建工具列表时调用）。

        返回过滤后的注册中心；空白名单返回 None（不限制，用完整工具集）。
        """
        host = self.skill_host
        # 收集所有激活 Skill 并集的 allowedTools（激活状态里记录的是已渲染 SOP）
        # 简化：active 状态不直接存白名单，这里由 SkillHost 依据激活记录重建
        return host.filter_registry_by_active()

    async def _run_sub_agent(self, skill, prompt: str, context_messages: list, host) -> str:
        """fork 模式：开一个独立子 Agent 执行，收集文本结果返回。"""
        from .session import Session, TextBlock

        sub_session = Session(model=skill.model or self.config.model)
        # 按 skill.context 决定带多少主对话上下文
        if skill.context == "recent":
            for msg in context_messages[-5:]:
                sub_session.messages.append(msg)
        elif skill.context == "full":
            for msg in context_messages:
                sub_session.messages.append(msg)
        # context: none 不带任何历史

        # allowedTools 过滤工具集（LoadSkill 始终可见）
        filtered_registry = self.skill_host.filter_registry(skill.allowed_tools) or self.registry
        agent = Agent(
            self.config,
            session=sub_session,
            registry=filtered_registry,
            workdir=self.workdir,
            client=self.client,  # 复用宿主 client（真实场景共享连接，测试场景用 fake）
            agent_config=AgentConfig(max_iterations=self.max_iterations),
            plan_state=self.plan_state,
            cancel_event=self.cancel_event,
            permission=self.permission,
        )
        agent.bind_skill_context(self.skill_host.state, tool_filter=self._skill_tool_filter)

        collected: list[str] = []
        async for event in agent.run(prompt):
            if isinstance(event, TextChunkEvent):
                collected.append(event.text)
            elif isinstance(event, ErrorEvent):
                collected.append(f"（错误：{event.message}）")
        # fork 结果回流主对话：作为一条 assistant 消息塞回去
        result_text = "".join(collected)
        if result_text:
            self.session.add_message("assistant", result_text, blocks=[TextBlock(text=result_text)])
            self._save()
        return result_text

class TerminalUIController:
    """第 10 章：终端版 UIController——命令 handler 与 UI 的接口。

    命令不需要知道 UI 底层细节，只需要知道能显示系统消息、发送用户消息、
    切换 Plan Mode、查询 Token、刷新状态栏。
    """

    def __init__(self, app: App) -> None:
        self.app = app

    def add_system_message(self, text: str) -> None:
        self.app.terminal.write(text)

    def send_user_message(self, text: str) -> None:
        self.app.terminal.write(f"（发送给 Agent：{text[:80]}…）" if len(text) > 80 else f"（发送给 Agent：{text}）")
        asyncio.create_task(self.app._ask(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if enabled:
            if not self.app.plan_state.active:
                path = self.app.plan_state.enter(self.app.workdir)
                self.app._refresh_client_tools()
                self.app.terminal.write(f"已进入规划模式（只读探索）。计划文件：{path}")
        else:
            if self.app.plan_state.active:
                self.app.plan_state.exit()
                self.app._refresh_client_tools()

    def get_token_count(self) -> tuple[int, int]:
        estimate = self.app.context.estimate_payload("", self.app.session)
        return estimate, self.app.context.config.context_window

    def refresh_status(self) -> None:
        pass  # 终端版无状态栏


def _gap_text(last_active) -> str:
    """把上次活跃时间到现在的跨度转成「X 天 / X 小时」的中文描述。"""
    from datetime import datetime

    try:
        last = datetime.fromisoformat(str(last_active))
    except ValueError:
        return "较长时间"
    from .session import utc_now

    delta = datetime.fromisoformat(utc_now()) - last
    total_seconds = max(0, delta.total_seconds())
    hours = int(total_seconds // 3600)
    if hours >= 48:
        return f"{hours // 24} 天"
    if hours >= 1:
        return f"{hours} 小时"
    return "不到 1 小时"


def main() -> None:
    try:
        config = load_provider_config(dotenv_path=".env")
    except ConfigurationError as exc:
        Terminal().error(str(exc))
        return
    try:
        asyncio.run(App(config).run_async())
    except KeyboardInterrupt:
        return
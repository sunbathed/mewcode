from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import sys
from pathlib import Path
from typing import Any

from .commands import (
    KNOWN_COMMANDS,
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
        # 客户端携带注册中心启用的工具描述
        if isinstance(self.client, OpenAICompatibleClient) and self.client.tools is None:
            self.client.tools = self.registry.api_specs()
        # 供测试等待输出就绪（首行启动文案写出后置位）
        self.terminal_ready = terminal_ready if terminal_ready is not None else asyncio.Event()

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
            self.terminal.write(f"已创建新会话：{self.session.id}")
            return True
        if command.name == "clear":
            self.session.clear()
            self._save()
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
        if command.name == "status":
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
        from .commands.handlers import ALL_COMMANDS

        # 按「核心 + 扩展」顺序展示，手写别名展示保证可读性
        rows = [
            ("/help, /h, /?", "显示帮助信息"),
            ("/status, /s", "显示状态信息"),
            ("/compact, /c", "压缩上下文"),
            ("/clear", "清除对话，创建新会话"),
            ("/plan, /p", "切换到 Plan 模式"),
            ("/do", "切换到执行模式"),
            ("/session", "会话管理"),
            ("/memory", "记忆管理"),
            ("/permission, /perm, /permissions", "权限管理"),
            ("/review", "审查代码变更"),
            ("/new", "新建会话"),
            ("/sessions", "列出历史会话"),
            ("/use <id>", "切换会话"),
            ("/resume", "恢复会话"),
            ("/init", "初始化 MEWCODE.md"),
            ("/mcp", "查看 MCP 状态"),
            ("/exit", "保存并退出"),
        ]
        for name, desc in rows:
            lines.append(f"  {name:<26} {desc}")
        lines.append("")
        self.terminal.write("\n".join(lines))

    def _handle_permissions_command_option(self, mode: str) -> None:
        """供框架 permission 命令 mode 子命令使用的切换入口。"""
        self._handle_permissions_command(ParsedCommand("permissions", mode))

    def _handle_permissions_command(self, command) -> None:
        """/permissions 命令：查看或切换权限模式（第 4 层权限模式）。

        无参数时展示当前模式与规则统计；带参数时切换模式。
        """
        argument = command.argument.strip()
        if not argument:
            rules = self.permission.rules
            self.terminal.write(
                f"当前权限模式：{self.permission.mode.value}（本地规则 {len(rules.rules.local)} 条 / "
                f"项目规则 {len(rules.rules.project)} 条 / 用户规则 {len(rules.rules.user)} 条）"
            )
            return
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
        self._save()

    async def _ask(self, text: str) -> None:
        """第四章：以 Agent Loop 引擎驱动一轮用户消息，事件流驱动 UI 渲染。"""
        instructions = load_instructions(self.workdir)
        memory_index = self.memory.load_index() if self.auto_memory else ""
        agent = Agent(
            self.config,
            session=self.session,
            registry=self.registry,
            workdir=self.workdir,
            client=self.client,
            agent_config=AgentConfig(
                max_iterations=self.max_iterations,
                custom_instructions=instructions,
                memory_instructions=memory_index,
            ),
            plan_state=self.plan_state,
            cancel_event=self.cancel_event,
            permission=self.permission,
            context=self.context,
        )
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
            self._save()
            self._schedule_memory_extraction()

    async def run_async(self) -> None:
        self.terminal.write(f"MewCode 已启动，会话：{self.session.id}。输入 /exit 退出。")
        self.terminal_ready.set()
        # 第七章：启动时后台连接 MCP Server（失败不阻塞，只告警）
        await self.setup_mcp()
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
                # 第 8 章：/compact 需异步调用 LLM 生成摘要，单独走异步分支
                if command.name == "compact":
                    await self._handle_compact_command()
                elif command.name == "resume":
                    await self._handle_resume_command(command)
                elif command.name == "review":
                    # 第 10 章：PROMPT 类型命令——把审查 prompt 发给 Agent
                    from .commands.handlers.review import REVIEW_PROMPT

                    prompt = REVIEW_PROMPT
                    if command.argument:
                        prompt += f"\n\n额外关注：{command.argument}"
                    await self._ask(prompt)
                elif not self._handle_command(command):
                    self._cancel_memory_tasks()
                    return
                continue
            await self._ask(text)


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
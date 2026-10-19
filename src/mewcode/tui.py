from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from rich.text import Text
from textual.app import App as TextualApp, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from . import __version__
from .commands import (
    Command,
    CommandContext,
    CommandRegistry,
    CommandType,
    ParsedCommand,
    command_usage,
    parse_command,
)
from .commands.completion import CompletionPopup
from .config import ProviderConfig
from .engine.builder import BuildOptions, build_system_prompt, detect_environment
from .llm_client import (
    LLMError,
    StreamEnd,
    StreamInterruptedError,
    TextDelta,
    ToolUseDelta,
    ToolUseStart,
)
from .memory import MemoryManager, MemoryManagerError, load_instructions
from .permission import (
    MODE_ALIASES,
    PermissionChecker,
    PermissionDecision,
    PermissionMode,
    PermissionResponse,
    parse_mode,
)
from .session import Session, TextBlock, ToolResultBlock, ToolUseBlock
from .storage import SessionStorage, SessionStorageError
from .tools import ToolRegistry, ToolResult

# 主题色
COLOR_USER = "#8a8a8a"
COLOR_ORANGE = "#e8862d"
COLOR_FOREGROUND = "#e8e8e8"
COLOR_MUTED = "#8a8a8a"
COLOR_SUCCESS = "#7cc97c"
COLOR_FAIL = "#e07070"

# Claude Code 风格 8-bit 像素吉祥物
MASCOT_ART = """  ▄     ▄
 █▀█████▀█
 █ █   █ █
 █▀▀▀▀▀▀▀█
 ▀ ▀   ▀ ▀"""


class PermissionModal(ModalScreen[PermissionResponse]):
    """第 5 层 HITL 确认对话框：展示工具调用信息，供用户放行/始终允许/拒绝。"""

    CSS = """
    PermissionModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    #permission-box {
        width: 72;
        border: round #e8862d;
        background: #0a0a0a;
        padding: 1 2;
    }
    #permission-title {
        color: #e8862d;
        text-style: bold;
        margin-bottom: 1;
    }
    #permission-tool {
        color: #ffffff;
        text-style: bold;
    }
    #permission-desc {
        color: #cccccc;
        height: auto;
        margin: 1 0;
    }
    #permission-actions {
        height: 3;
        align: center middle;
    }
    """

    BINDINGS = [
        ("y,1", "allow_once", "放行"),
        ("a,2", "allow_always", "始终允许"),
        ("n,3,escape", "deny", "拒绝"),
    ]

    def __init__(self, tool_name: str, description: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-box"):
            yield Static("MewCode 想要执行以下操作：", id="permission-title")
            yield Static(f"[{self.tool_name}] {self.description}", id="permission-tool")
            yield Static("允许执行？(y)是 / (n)否 / (a)始终允许", id="permission-desc")
            with Horizontal(id="permission-actions"):
                yield Button("1 放行", id="perm-allow", variant="success")
                yield Button("2 始终允许", id="perm-always", variant="primary")
                yield Button("3 拒绝", id="perm-deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "perm-allow":
            self.dismiss(PermissionResponse.ALLOW)
        elif event.button.id == "perm-always":
            self.dismiss(PermissionResponse.ALLOW_ALWAYS)
        elif event.button.id == "perm-deny":
            self.dismiss(PermissionResponse.DENY)

    def action_allow_once(self) -> None:
        self.dismiss(PermissionResponse.ALLOW)

    def action_allow_always(self) -> None:
        self.dismiss(PermissionResponse.ALLOW_ALWAYS)

    def action_deny(self) -> None:
        self.dismiss(PermissionResponse.DENY)


class MewCodeTUI(TextualApp[None]):
    """Claude Code 风格全屏终端工作台：双栏卡片 + 单层滚动对话流 + 橙色强调。"""

    TITLE = "MewCode"
    CSS = """
    Screen { background: #0a0a0a; color: #e8e8e8; }

    /* Claude Code 顶部欢迎大卡片 */
    #welcome {
        height: auto;
        border: round #e8862d;
        margin: 1 2 0 2;
        padding: 1 2;
        background: #0a0a0a;
    }
    #welcome-left {
        width: 46%;
        height: auto;
        align: center middle;
        border-right: solid #333333;
        padding-right: 2;
    }
    #welcome-right {
        width: 54%;
        height: auto;
        padding-left: 2;
    }
    #welcome-brand {
        color: #e8862d;
        text-style: bold;
        content-align: center middle;
    }
    #welcome-heading {
        color: #ffffff;
        text-style: bold;
        content-align: center middle;
        margin-top: 1;
    }
    #welcome-mascot {
        color: #e8862d;
        content-align: center middle;
        margin: 1 0;
    }
    #welcome-meta-model {
        color: #8a8a8a;
        content-align: center middle;
    }
    #welcome-meta-cwd {
        color: #666666;
        content-align: center middle;
    }
    #tips-title {
        color: #e8862d;
        text-style: bold;
        margin-bottom: 0;
    }
    .welcome-tip {
        color: #cccccc;
        margin-bottom: 0;
    }
    .welcome-tip-dim {
        color: #777777;
        margin-bottom: 1;
    }
    #recent-title {
        color: #c97223;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    .recent-item {
        color: #8a8a8a;
    }
    .recent-muted {
        color: #555555;
    }

    #conversation { height: 1fr; scrollbar-size: 1 1; padding: 1 2; }
    .user-row { color: #8a8a8a; }
    .assistant-row { color: #e8e8e8; }
    .tool-row { color: #e8862d; }
    .system-row { color: #8a8a8a; }

    /* Claude Code 极简输入框：上下横线分隔 */
    #composer {
        height: 3;
        border-top: solid #333333;
        border-bottom: solid #333333;
        background: #0a0a0a;
        padding: 0 1;
    }
    #prompt-mark {
        width: 2;
        color: #e8862d;
        text-style: bold;
        content-align: center middle;
    }
    #prompt {
        height: 1;
        background: #0a0a0a;
        color: #e8e8e8;
        border: none;
        padding: 0;
    }
    #prompt:focus {
        border: none;
    }

    /* 底部状态栏 */
    #footer {
        height: 1;
        background: #0a0a0a;
        padding: 0 2;
    }
    #shortcuts-hint {
        width: 1fr;
        color: #555555;
    }
    #status {
        width: auto;
        color: #8a8a8a;
    }
    """
    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("f1", "show_help", "帮助"),
        ("tab", "command_complete", "命令补全"),
        ("escape", "dismiss_completion", "关闭补全"),
    ]

    def __init__(
        self,
        config: ProviderConfig,
        *,
        storage: SessionStorage,
        client: Any,
        registry: ToolRegistry | None = None,
        workdir: str | None = None,
        permission_mode: PermissionMode | str = PermissionMode.DEFAULT,
        permission: PermissionChecker | None = None,
        mcp_manager=None,
        memory: MemoryManager | None = None,
        memory_client=None,
        auto_memory: bool = True,
    ) -> None:
        super().__init__()
        self.config = config
        self.storage = storage
        self.client = client
        self.registry = registry or ToolRegistry()
        self.workdir = workdir or os.getcwd()
        # 第六章：五层权限防御
        self.permission = permission or PermissionChecker(workdir=self.workdir, mode=permission_mode)
        # 第七章：MCP 开放工具生态
        from .mcp import MCPManager

        self.mcp = mcp_manager or MCPManager(workdir=self.workdir)
        # 第 9 章：跨会话记忆
        self.memory = memory or MemoryManager(self.workdir)
        self.memory_client = memory_client
        self.auto_memory = auto_memory
        self._pending_resume = False  # /resume 交互：等待用户输入会话编号
        # 第 10 章：命令框架注册中心 + Tab 补全弹窗
        self.commands = self._build_command_registry()
        self.completion_popup = CompletionPopup(self.commands)
        # /compact 需要 ContextManager（第八章），TUI 也接入一份专供手工压缩
        from .context import ContextManager

        self.context = ContextManager(config, workdir=self.workdir)
        self.session = storage.latest() or Session(model=config.model)
        if not self.session.model:
            self.session.model = config.model
        self._busy = False
        self._card_tasks: dict[int, asyncio.Task[None]] = {}

    def _build_command_registry(self) -> CommandRegistry:
        """TUI 版命令注册中心：十个核心命令 + TUI 扩展命令。

        核心命令复用框架的元数据（名称/别名/描述/类型/用法），handler
        覆写为 TUI 适配版——走 TUI 现有的异步渲染方法，与 Textual
        事件循环天然契合。compact/review 也注册为命令（/help 可见、
        Tab 补全可达），handler 调 TUI 自己的异步实现。
        """
        registry = CommandRegistry()

        from .commands.handlers.clear import CLEAR_COMMAND
        from .commands.handlers.compact import COMPACT_COMMAND
        from .commands.handlers.do import DO_COMMAND
        from .commands.handlers.help import HELP_COMMAND
        from .commands.handlers.memory import MEMORY_COMMAND
        from .commands.handlers.permission import PERMISSION_COMMAND
        from .commands.handlers.plan import PLAN_COMMAND
        from .commands.handlers.review import REVIEW_COMMAND
        from .commands.handlers.session import SESSION_COMMAND
        from .commands.handlers.status import STATUS_COMMAND

        async def _clear(ctx) -> None:
            self.session.clear()
            self._save()
            await self._render_session()
            await self._system("当前会话已清空")

        async def _plan(ctx) -> None:
            if self.plan_state.active:
                await self._system(f"已在规划模式。计划文件：{self.plan_state.plan_file}")
            else:
                path = self.plan_state.enter(self.workdir)
                self._refresh_client_tools()
                await self._system(f"已进入规划模式（只读探索）。计划文件：{path}")
                if ctx.args:
                    await self._ask(ctx.args)

        async def _do(ctx) -> None:
            if not self.plan_state.active:
                await self._system("当前不在规划模式。")
            else:
                self.plan_state.exit()
                self._refresh_client_tools()
                await self._system("已退出规划模式，恢复正常工具。")

        async def _help(ctx) -> None:
            await self._system(self._help_text())

        async def _status(ctx) -> None:
            await self._system(self._status_text())

        async def _session_cmd(ctx) -> None:
            await self._handle_session_command(ParsedCommand("session", ctx.args))

        async def _memory_cmd(ctx) -> None:
            await self._handle_memory_command(ParsedCommand("memory", ctx.args))

        async def _permission_cmd(ctx) -> None:
            await self._handle_permissions_command(ParsedCommand("permissions", ctx.args))

        async def _compact(ctx) -> None:
            await self._handle_compact_command(ctx.args)

        async def _review(ctx) -> None:
            from .commands.handlers.review import REVIEW_PROMPT

            prompt = REVIEW_PROMPT
            if ctx.args:
                prompt += f"\n\n额外关注：{ctx.args}"
            await self._ask(prompt)

        tui_handlers = [
            (HELP_COMMAND, _help),
            (STATUS_COMMAND, _status),
            (CLEAR_COMMAND, _clear),
            (PLAN_COMMAND, _plan),
            (DO_COMMAND, _do),
            (SESSION_COMMAND, _session_cmd),
            (MEMORY_COMMAND, _memory_cmd),
            (PERMISSION_COMMAND, _permission_cmd),
            (COMPACT_COMMAND, _compact),
            (REVIEW_COMMAND, _review),
        ]
        for base, handler in tui_handlers:
            registry.register_sync(
                Command(
                    name=base.name,
                    description=base.description,
                    type=base.type,
                    handler=handler,
                    aliases=list(base.aliases),
                    usage=base.usage,
                    arg_prompt=base.arg_prompt,
                    hidden=base.hidden,
                )
            )

        registry.register_sync(
            Command(
                name="new",
                description="新建会话",
                type=CommandType.LOCAL_UI,
                handler=lambda ctx: self._start_new_session(),
                usage="/new",
            )
        )
        registry.register_sync(
            Command(
                name="sessions",
                description="列出历史会话",
                type=CommandType.LOCAL,
                handler=lambda ctx: self._list_sessions(),
                usage="/sessions",
            )
        )
        registry.register_sync(
            Command(
                name="use",
                description="切换到指定会话",
                type=CommandType.LOCAL,
                handler=lambda ctx: self._switch_session(ctx.args),
                usage="/use <会话ID>",
                arg_prompt="用法：/use <会话ID>",
            )
        )
        registry.register_sync(
            Command(
                name="init",
                description="初始化 MEWCODE.md",
                type=CommandType.LOCAL,
                handler=lambda ctx: self._create_init_file(),
                usage="/init",
            )
        )
        registry.register_sync(
            Command(
                name="mcp",
                description="查看 MCP Server 状态",
                type=CommandType.LOCAL,
                handler=lambda ctx: self._run_mcp_command(),
                usage="/mcp",
            )
        )
        registry.register_sync(
            Command(
                name="exit",
                description="保存并退出",
                type=CommandType.LOCAL_UI,
                handler=lambda ctx: self._quit_app(),
                usage="/exit",
                hidden=True,
            )
        )
        registry.register_sync(
            Command(
                name="resume",
                description="恢复历史会话",
                type=CommandType.LOCAL,
                handler=lambda ctx: self._start_resume(),
                usage="/resume",
            )
        )
        return registry

    # ---------- TUI 扩展命令辅助方法 ----------

    async def _start_new_session(self) -> None:
        self._save()
        self.session = Session(model=self.config.model)
        await self._render_session()
        self._show_welcome()
        await self._system(f"已创建新会话：{self.session.id}")
        self._refresh_status("已连接")

    async def _list_sessions(self) -> None:
        sessions = self.storage.list_sessions()
        if not sessions:
            await self._system("暂无历史会话")
            return
        for item in sessions:
            marker = " *" if item.id == self.session.id else ""
            await self._system(f"{item.id}  {item.updated_at}  {item.model}{marker}")

    async def _switch_session(self, session_id: str) -> None:
        if not session_id:
            await self._system("用法：/use <会话ID>")
            return
        try:
            self.session = self.storage.load(session_id)
        except SessionStorageError as exc:
            await self._system(str(exc))
            return
        await self._render_session()
        self._show_work()
        await self._system(f"已切换到会话：{self.session.id}")
        self._refresh_status("已连接")

    async def _create_init_file(self) -> None:
        path = "MEWCODE.md"
        if os.path.exists(path):
            await self._system(f"{path} 已存在。")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# MEWCODE.md\n\n项目说明与指令文件。\n")
            await self._system(f"已创建 {path} 文件。")
        except Exception as exc:
            await self._system(f"创建文件失败：{exc}")

    def _quit_app(self) -> None:
        self._save()
        self.exit()

    async def _run_mcp_command(self) -> None:
        await self._handle_mcp_command()

    def compose(self) -> ComposeResult:
        with Horizontal(id="welcome"):
            with Vertical(id="welcome-left"):
                yield Static(f"MewCode v{__version__}", id="welcome-brand")
                yield Static("Welcome back!", id="welcome-heading")
                yield Static(MASCOT_ART, id="welcome-mascot")
                yield Static(f"{self.config.model} · API Usage Billing", id="welcome-meta-model")
                yield Static(f"{os.getcwd()}", id="welcome-meta-cwd")
            with Vertical(id="welcome-right"):
                yield Static("Tips for getting started", id="tips-title")
                yield Static("Run /init to create a MEWCODE.md file with instructions for MewCode", classes="welcome-tip")
                yield Static(
                    f"Note: You have launched mewcode in {os.path.basename(os.getcwd()) or os.getcwd()}.",
                    classes="welcome-tip-dim",
                )
                yield Static("Recent activity", id="recent-title")
                with Vertical(id="recent-list"):
                    sessions = self.storage.list_sessions()
                    if not sessions:
                        yield Static("No recent activity", classes="recent-muted")
                    else:
                        for item in sessions[:3]:
                            time_str = item.updated_at[:16].replace("T", " ") if item.updated_at else ""
                            yield Static(f"· {item.id[:8]}  {time_str}", classes="recent-item")
        yield ScrollableContainer(id="conversation")
        with Horizontal(id="composer"):
            yield Static(">", id="prompt-mark")
            yield Input(
                placeholder="",
                id="prompt",
            )
        with Horizontal(id="footer"):
            yield Static("? for shortcuts", id="shortcuts-hint")
            yield Static("", id="status")
        yield self.completion_popup

    # ---------- 布局切换 ----------

    async def on_mount(self) -> None:
        self._refresh_status("已连接")
        if self.session.messages:
            self._show_work()
            await self._render_session()
        else:
            self._show_welcome()
        self.query_one("#prompt", Input).focus()
        # 第七章：启动时后台连接 MCP Server（失败不阻塞，只告警）
        if self.mcp.configs:
            self.run_worker(self._setup_mcp(), exclusive=False)

    # ---------- 第 10 章：命令补全 ----------

    async def action_command_complete(self) -> None:
        """Tab 补全：输入框以 / 开头时显示候选命令列表。"""
        prompt = self.query_one("#prompt", Input)
        value = prompt.value
        if not value.startswith("/"):
            # 非命令输入不触发补全
            return
        self.completion_popup.show(value)

    async def action_dismiss_completion(self) -> None:
        self.completion_popup.hide()
        self.query_one("#prompt", Input).focus()

    def on_completion_popup_selected(self, event) -> None:
        """补全弹窗选中后把命令填入输入框。"""
        prompt = self.query_one("#prompt", Input)
        prompt.value = event.value + " "
        prompt.focus()

    async def _setup_mcp(self) -> None:
        """连接 MCP Server 并把工具注册进注册中心。"""
        try:
            failed = await self.mcp.register_all_tools(self.registry)
            connected = self.mcp.connected_servers()
            if connected:
                await self._system(
                    f"MCP 已连接 {len(connected)} 个 Server，注册 {self.mcp.registered_count()} 个工具：{', '.join(connected)}"
                )
            if failed:
                await self._system(f"MCP 连接失败：{', '.join(failed)}（不影响其他功能）")
            # 同步客户端工具描述（延迟工具只列名字，完整 schema 经 ToolSearch 加载）
            self._refresh_client_tools()
        except Exception as exc:  # noqa: BLE001 - MCP 连接失败不阻塞 UI
            await self._system(f"MCP 初始化失败：{exc}")

    def _refresh_recent_activity(self) -> None:
        try:
            container = self.query_one("#recent-list", Vertical)
            for child in list(container.children):
                child.remove()
            sessions = self.storage.list_sessions()
            if not sessions:
                container.mount(Static("No recent activity", classes="recent-muted"))
            else:
                for item in sessions[:3]:
                    time_str = item.updated_at[:16].replace("T", " ") if item.updated_at else ""
                    container.mount(Static(f"· {item.id[:8]}  {time_str}", classes="recent-item"))
        except Exception:
            pass

    def _show_welcome(self) -> None:
        self._refresh_recent_activity()
        self.query_one("#welcome").display = True
        self.query_one("#conversation").display = False

    def _show_work(self) -> None:
        self.query_one("#welcome").display = False
        self.query_one("#conversation").display = True

    def _conversation(self) -> ScrollableContainer:
        return self.query_one("#conversation", ScrollableContainer)

    def _refresh_status(self, state: str) -> None:
        dot_color = "#50fa7b" if state == "已连接" else "#f1fa8c" if state == "生成中" else "#ff5555"
        self.query_one("#status", Static).update(
            Text.assemble(
                ("● ", dot_color),
                (f"{state} · ", "#8a8a8a"),
                (f"{self.session.model}", "#aaaaaa"),
            )
        )

    async def _append_row(self, text: Any, classes: str | None = None) -> Static:
        """在对话流末尾追加一行，滚动到底部。"""
        row = Static(text, classes=classes)
        await self._conversation().mount(row)
        self._conversation().scroll_end(animate=False)
        return row

    @staticmethod
    def _summarize(text: str, limit: int) -> str:
        text = text.replace("\n", " ")
        if len(text) > limit:
            return text[:limit] + "…"
        return text

    # ---------- 历史渲染（单流） ----------

    async def _render_message(self, message) -> None:
        if message.blocks:
            text_parts = [b.text for b in message.blocks if isinstance(b, TextBlock)]
            tool_uses = [b for b in message.blocks if isinstance(b, ToolUseBlock)]
            tool_results = [b for b in message.blocks if isinstance(b, ToolResultBlock)]
            if message.role == "user" and tool_results and not text_parts:
                for block in tool_results:
                    summary = self._summarize(block.content, 120)
                    await self._append_row(Text(f"✓ 工具结果 › {summary}", style=COLOR_SUCCESS))
                return
            if message.role == "user":
                await self._append_row(f"你  › {message.content}", classes="user-row")
            if message.role == "assistant":
                if text_parts:
                    await self._append_row("".join(text_parts), classes="assistant-row")
                for use in tool_uses:
                    summary = self._summarize(json.dumps(use.input, ensure_ascii=False), 60)
                    await self._append_row(f"⎿ ⎿ {use.name} {summary}".rstrip(), classes="tool-row")
        elif message.role == "user":
            await self._append_row(f"你  › {message.content}", classes="user-row")
        elif message.role == "assistant":
            await self._append_row(message.content, classes="assistant-row")

    async def _render_session(self) -> None:
        container = self._conversation()
        for child in list(container.children):
            child.remove()
        for message in self.session.messages:
            await self._render_message(message)

    async def _system(self, text: str) -> None:
        await self._append_row(Text(f"系统 › {text}", style=COLOR_MUTED))

    def _save(self) -> None:
        self.storage.save(self.session)

    # ---------- 输入与命令 ----------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text or self._busy:
            return
        if self._pending_resume:
            asyncio.create_task(self._finish_resume(text))
            return
        if text == "?":
            asyncio.create_task(self._system("快捷命令提示：/init（初始化规则）、/new（新建会话）、/sessions（历史会话）、/use <id>（切换）、/session（管理会话）、/resume（恢复会话）、/memory（记忆）、/clear（清空）、/help（帮助）、/exit（退出）"))
            return
        # 第 9 章：显式记忆——「记住 X」直接写入记忆文件，不走 Agent 对话
        if text.startswith(("记住", "请记住", "帮我记住")):
            asyncio.create_task(self._remember(text))
            return
        command = parse_command(text)
        if command is not None:
            asyncio.create_task(self._handle_command(command))
        else:
            self._show_work()
            self.run_worker(self._ask(text), exclusive=True)

    async def _remember(self, text: str) -> None:
        try:
            written = self.memory.remember(text)
        except MemoryManagerError as exc:
            await self._system(f"记忆写入失败：{exc}")
            return
        if written:
            await self._system(f"已记住：{text}")

    def action_show_help(self) -> None:
        """F1 快捷帮助：走 /help 命令的框架文本。"""
        asyncio.create_task(self._system(self._help_text()))

    async def _handle_command(self, command) -> None:
        """第 10 章：命令统一走注册中心分发。

        - 未知命令：统一引导到 /help
        - 参数缺失且有 arg_prompt：显示参数提示
        - 找到命令：调用 handler（TUI 的 handler 都是 async，直接 await）
        """
        # 补全弹窗还开着时收到命令输入，先关掉
        if self.completion_popup.display:
            self.completion_popup.hide()
        cmd = self.commands.find(command.name)
        if cmd is None:
            await self._system(command_usage(command))
            return
        if cmd.arg_prompt and not command.argument:
            await self._system(cmd.arg_prompt)
            return
        result = cmd.handler(CommandContext(args=command.argument))
        if asyncio.iscoroutine(result):
            await result

    # ---------- 单流对话编排 ----------

    def _system_prompt(self) -> str:
        """组装 system prompt：核心模块 + 环境 + 第 9 章指令与记忆注入。"""
        instructions = load_instructions(self.workdir)
        memory_index = self.memory.load_index() if self.auto_memory else ""
        return build_system_prompt(
            env=detect_environment(self.workdir, model=self.config.model),
            options=BuildOptions(
                custom_instructions=instructions,
                memory_instructions=memory_index,
            ),
        )

    # ---------- 第 10 章命令 ----------

    def _help_text(self) -> str:
        """从注册中心生成 /help 文本（含别名）。"""
        lines = ["可用命令："]
        for cmd in self.commands.list_commands():
            names = [f"/{cmd.name}"] + [f"/{a}" for a in cmd.aliases]
            lines.append(f"  {', '.join(names):<30} {cmd.description}")
        lines.append("")
        lines.append("输入 /help <命令名> 查看详细用法。")
        return "\n".join(lines)

    def _status_text(self) -> str:
        """/status：模式 / Token / 工具 / 记忆 / 目录 / 版本。"""
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
        lines.append(f"版本：v{__version__}")
        return "\n".join(lines)

    async def _handle_compact_command(self, args: str = "") -> None:
        """第 8 章：/compact 手动压缩——显示压缩前后的 Token 变化。"""
        from .context import CompactError

        if not self.session.messages:
            await self._system("当前会话还没有内容，无需压缩。")
            return
        try:
            outcome = await self.context.compact(self.session)
        except CompactError as exc:
            await self._system(f"压缩失败：{exc}")
            return
        if not outcome.performed:
            await self._system("当前会话内容不多，无需压缩。")
            return
        await self._system(
            f"已压缩：{outcome.before_tokens:,} → {outcome.after_tokens:,} token"
            f"（释放 {max(0, outcome.before_tokens - outcome.after_tokens):,}）"
        )
        self._save()

    async def _handle_session_command(self, command) -> None:
        """/session：会话管理（list / delete / path）。"""
        argument = command.argument.strip()
        parts = argument.split(maxsplit=1)
        action = parts[0].lower() if parts else "list"
        if action == "list":
            sessions = self.storage.list_metas()
            if not sessions:
                await self._system("暂无历史会话。")
                return
            await self._system(f"会话目录：{self.storage.directory}")
            for meta in sessions:
                marker = " *" if meta.id == self.session.id else ""
                title = f"（{meta.title[:24]}）" if meta.title else ""
                await self._system(f"{meta.id}  {meta.last_active.isoformat()}  {meta.model}{title}{marker}")
        elif action == "delete":
            if len(parts) < 2:
                await self._system(command_usage(command))
                return
            session_id = parts[1]
            if session_id == self.session.id:
                await self._system("不能删除当前会话。")
                return
            removed = self.storage.delete(session_id)
            if removed:
                await self._system("已删除会话。")
            else:
                await self._system("会话不存在。")
        elif action == "path":
            await self._system(str(self.storage.directory))
        else:
            await self._system(command_usage(command))

    async def _handle_memory_command(self, command) -> None:
        """/memory：查看/清空/定位自动记忆。"""
        argument = command.argument.strip()
        action = argument.split(maxsplit=1)[0].lower() if argument else "show"
        if action == "show":
            view = self.memory.view()
            await self._system(view if view else "暂无记忆。")
        elif action == "clear":
            removed = self.memory.clear()
            await self._system(f"已清空 {removed} 条记忆。")
        elif action == "path":
            await self._system(f"用户级：{self.memory.user_dir}")
            await self._system(f"项目级：{self.memory.project_dir}")
        else:
            await self._system(command_usage(command))

    async def _start_resume(self) -> None:
        """/resume：展示会话列表，等待用户输入编号恢复。"""
        sessions = self.storage.list_metas()
        if not sessions:
            await self._system("暂无历史会话。")
            return
        await self._system("历史会话（输入编号恢复，回车取消）：")
        for index, meta in enumerate(sessions, start=1):
            marker = " *" if meta.id == self.session.id else ""
            title = f"（{meta.title[:24]}）" if meta.title else ""
            await self._system(f"{index}. {meta.id}  {meta.last_active.isoformat()}  {meta.model}{title}{marker}")
        self._pending_resume = True

    async def _finish_resume(self, answer: str) -> None:
        """解析 /resume 选择的会话编号并恢复。"""
        self._pending_resume = False
        if not answer.strip():
            return
        sessions = self.storage.list_metas()
        try:
            index = int(answer.strip()) - 1
            meta = sessions[index]
        except (ValueError, IndexError):
            await self._system("无效的编号。")
            return
        try:
            self.session = self.storage.load(meta.id)
        except SessionStorageError as exc:
            await self._system(str(exc))
            return
        if not self.session.model:
            self.session.model = self.config.model
        await self._render_session()
        self._show_work()
        await self._system(f"已恢复到会话：{meta.id}（共 {len(self.session.messages)} 条消息）")
        self._refresh_status("已连接")

    def _refresh_client_tools(self) -> None:
        """同步客户端携带的工具描述（注册中心可能新增 MCP 工具）。"""
        if hasattr(self.client, "tools"):
            self.client.tools = self.registry.api_specs()

    async def _handle_mcp_command(self) -> None:
        """/mcp 命令：查看 MCP Server 连接状态与注册的工具。"""
        if not self.mcp.configs:
            await self._system("未配置任何 MCP Server（项目级 .mewcode/config.yaml 或用户级 ~/.mewcode/config.yaml）。")
            return
        connected = self.mcp.connected_servers()
        await self._system(f"已配置 {len(self.mcp.configs)} 个 MCP Server：{', '.join(self.mcp.configs)}")
        if connected:
            for name in connected:
                client = self.mcp.get_client(name)
                tool_names = client.tool_names() if client else []
                await self._system(f"✓ {name}：{len(tool_names)} 个工具（{', '.join(tool_names)}）")
        if self.mcp.failures:
            for name, reason in self.mcp.failures.items():
                await self._system(f"✗ {name}：{reason}")
        if not connected and not self.mcp.failures:
            await self._system("MCP Server 尚未连接。")

    async def _handle_permissions_command(self, command) -> None:
        """/permissions 命令：查看或切换权限模式。"""
        argument = command.argument.strip()
        if not argument:
            rules = self.permission.rules
            await self._system(
                f"当前权限模式：{self.permission.mode.value}（本地 {len(rules.rules.local)} 条 / "
                f"项目 {len(rules.rules.project)} 条 / 用户 {len(rules.rules.user)} 条规则）"
            )
            return
        mode = parse_mode(argument)
        if mode == self.permission.mode:
            await self._system(f"当前权限模式已是 {mode.value}。")
            return
        self.permission.set_mode(mode)
        await self._system(f"权限模式已切换为：{mode.value}（可用：default / acceptEdits / plan / bypassPermissions，别名 yolo）")

    async def _permission_check(self, use: ToolUseBlock) -> ToolResult | None:
        """对一次工具调用做五层权限检查。

        返回 None 表示放行；返回 ToolResult 表示被拦截（DENY）或需要先 HITL 确认。
        """
        if self.permission is None:
            return None
        decision = self.permission.check(use.name, use.input)
        if decision.decision == PermissionDecision.DENY:
            return ToolResult(is_error=True, error=decision.reason)
        if decision.decision == PermissionDecision.ASK:
            # 第 5 层 HITL：弹出确认对话框，等待用户回复
            # 注意：wait_for_dismiss=True 时 push_screen 返回 dismiss 的结果（须在 worker 中调用）
            response = await self.push_screen(
                PermissionModal(use.name, self._permission_description(use)),
                wait_for_dismiss=True,
            )
            if response == PermissionResponse.ALLOW_ALWAYS:
                self.permission.remember_always(use.name, use.input)
            if response in (PermissionResponse.ALLOW, PermissionResponse.ALLOW_ALWAYS):
                return None
            return ToolResult(is_error=True, error="操作被用户拒绝：HITL 确认未通过。")
        return None

    @staticmethod
    def _permission_description(use: ToolUseBlock) -> str:
        from .permission import build_permission_description

        return build_permission_description(use.name, use.input)

    async def _ask(self, text: str) -> None:
        self._busy = True
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        self.session.add_message("user", text)
        await self._append_row(f"你  › {text}", classes="user-row")
        self._refresh_status("生成中")
        try:
            turn = 0
            max_turns = 10
            while turn < max_turns:
                turn += 1
                # 每轮前同步工具描述：ToolSearch 发现的 MCP 工具下一轮即可见
                self._refresh_client_tools()
                text_out, uses, card_rows = await self._stream_turn()
                if uses:
                    self._finish_turn(text_out, uses)
                    await self._execute_tools(uses, card_rows)
                else:
                    self._finish_turn(text_out, [])
                    break
            else:
                await self._system(f"已达到工具调用轮数上限（{max_turns} 轮）。")
        except StreamInterruptedError as exc:
            await self._system(str(exc))
        except LLMError as exc:
            await self._system(str(exc))
        except Exception as exc:  # noqa: BLE001 - UI 层兜底，避免异常吞掉输入框
            await self._system(f"处理出错：{exc}")
        finally:
            self._cancel_pending_cards()
            self._busy = False
            prompt.disabled = False
            prompt.focus()
            self._refresh_status("已连接")
            self._save()
            self._schedule_memory_extraction()

    def _schedule_memory_extraction(self) -> None:
        """第 9 章：每轮对话结束后后台异步提取记忆（达到阈值才真正调用 LLM）。"""
        if not self.auto_memory:
            return
        if not self.memory.should_extract(len(self.session.messages)):
            return
        messages = list(self.session.messages)
        client = self.memory_client or self.client

        async def _extract() -> None:
            try:
                result = await self.memory.extract(client, messages)
                if result and "失败" not in result:
                    await self._system(f"记忆已更新：{result}")
            except Exception as exc:  # noqa: BLE001 - 后台提取失败不影响主流程
                await self._system(f"记忆提取跳过：{exc}")

        self.run_worker(_extract(), exclusive=False)

    def _cancel_pending_cards(self) -> None:
        """兜底：取消尚未完成的卡片 spinner 任务（异常/多轮路径的残留）。"""
        for task in self._card_tasks.values():
            task.cancel()
        self._card_tasks.clear()

    async def _stream_turn(self) -> tuple[str, list[ToolUseBlock], dict[int, Static]]:
        """发起一轮请求：单流就地追加文本，工具卡随流挂载并转 spinner。"""
        collected: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        card_rows: dict[int, Static] = {}
        stream_row: Static | None = None
        try:
            # 第 9 章：system prompt 注入指令文件与记忆索引，随后是完整会话历史
            messages = [{"role": "system", "content": self._system_prompt()}] + self.session.api_messages()
            async for event in self.client.stream(messages):
                if isinstance(event, TextDelta):
                    collected.append(event.text)
                    if stream_row is None:
                        stream_row = await self._append_row("", classes="assistant-row")
                    stream_row.update("".join(collected))
                    self._conversation().scroll_end(animate=False)
                elif isinstance(event, ToolUseStart):
                    slot = pending.setdefault(event.index, {"id": event.id, "name": event.name, "args": ""})
                    if event.id:
                        slot["id"] = event.id
                    if event.name:
                        slot["name"] = event.name
                    if event.name and event.index not in card_rows:
                        row = Static(Text(f"⎿ ⎿ {event.name} ⠋", style=COLOR_ORANGE))
                        await self._conversation().mount(row)
                        card_rows[event.index] = row
                        self._card_tasks[event.index] = asyncio.create_task(self._spinner(row, event.name))
                        self._conversation().scroll_end(animate=False)
                elif isinstance(event, ToolUseDelta):
                    slot = pending.setdefault(event.index, {"id": "", "name": "", "args": ""})
                    slot["args"] += event.args_fragment
                elif isinstance(event, StreamEnd):
                    break
        finally:
            if stream_row is not None and not collected and not pending:
                stream_row.remove()
        uses: list[ToolUseBlock] = []
        for index, slot in sorted(pending.items()):
            raw = slot["args"]
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                    args = parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    args = {"_json_error": raw[:200]}
            else:
                args = {}
            uses.append(ToolUseBlock(id=slot["id"] or f"tool_{index}", name=slot["name"], input=args))
        return "".join(collected), uses, card_rows

    def _finish_turn(self, text: str, uses: list[ToolUseBlock]) -> None:
        """把本轮流式文本固化进会话；工具请求在对话流中已就地展示。"""
        blocks: list[Any] = []
        if text:
            blocks.append(TextBlock(text=text))
        blocks.extend(uses)
        self.session.add_message("assistant", text, blocks=blocks or None)
        self._save()

    async def _execute_tools(self, uses: list[ToolUseBlock], card_rows: dict[int, Static] | None = None) -> None:
        """执行工具；复用流式阶段已挂载的卡片行，结果在行内打 ✓/✗。

        第六章：每个工具执行前先经过五层权限检查（黑名单/沙箱/规则/模式/HITL）。
        """
        results: list[tuple[str, str]] = []
        card_rows = card_rows or {}
        for index, use in enumerate(uses):
            row = card_rows.get(index)
            if row is None:
                # 回退：未复用卡片时现场挂载
                label = self._tool_label(use)
                row = Static(Text(f"⎿ ⎿ {label} ⠋", style=COLOR_ORANGE))
                await self._conversation().mount(row)
                self._card_tasks[index] = asyncio.create_task(self._spinner(row, label))
            # 五层权限检查：放行则执行，拦截则直接生成失败结果
            denied_result = await self._permission_check(use)
            if denied_result is not None:
                result = denied_result
            else:
                try:
                    result = await self.registry.execute(use.name, use.input)
                except Exception as exc:  # noqa: BLE001 - 未知工具/异常统一转为失败结果
                    result = ToolResult(is_error=True, error=str(exc) or f"{use.name} 执行失败")
            task = self._card_tasks.pop(index, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._write_tool_result(row, use, result)
            results.append((use.id, result.to_text()))

        if results:
            self.session.add_tool_results(results)
            self._save()

    async def _spinner(self, row: Static, label: str) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        idx = 0
        while True:
            row.update(Text(f"⎿ ⎿ {label} {frames[idx % len(frames)]}", style=COLOR_ORANGE))
            idx += 1
            await asyncio.sleep(0.08)

    def _tool_label(self, use: ToolUseBlock) -> str:
        summary = self._summarize(json.dumps(use.input, ensure_ascii=False), 60)
        return f"{use.name} {summary}".rstrip()

    def _write_tool_result(self, row: Static, use: ToolUseBlock, result: ToolResult) -> None:
        label = self._tool_label(use)
        if not result.is_error:
            summary = self._summarize(result.output, 200)
            row.update(Text(f"✓ ⎿ ⎿ {label}（{result.duration:.2f}s）：{summary}", style=COLOR_SUCCESS))
        else:
            reason = "超时" if result.timed_out else "失败"
            summary = self._summarize(result.error, 200)
            row.update(Text(f"✗ ⎿ ⎿ {label}（{reason}）：{summary}", style=COLOR_FAIL))
        self._conversation().scroll_end(animate=False)

class TuiUIController:
    """第 10 章：TUI 版 UIController——命令 handler 与 UI 的接口。

    Textual 的渲染是异步的，所以这里的方法都通过 run_worker /
    create_task 把工作交还给事件循环；命令本身不关心渲染细节。
    """

    def __init__(self, app: MewCodeTUI) -> None:
        self.app = app

    def add_system_message(self, text: str) -> None:
        import asyncio

        asyncio.create_task(self.app._system(text))

    def send_user_message(self, text: str) -> None:
        self.app.run_worker(self.app._ask(text), exclusive=False)

    def set_plan_mode(self, enabled: bool) -> None:
        import asyncio

        if enabled:
            if not self.app.plan_state.active:
                path = self.app.plan_state.enter(self.app.workdir)
                self.app._refresh_client_tools()
                asyncio.create_task(self.app._system(f"已进入规划模式（只读探索）。计划文件：{path}"))
        else:
            if self.app.plan_state.active:
                self.app.plan_state.exit()
                self.app._refresh_client_tools()

    def get_token_count(self) -> tuple[int, int]:
        estimate = self.app.context.estimate_payload("", self.app.session)
        return estimate, self.app.context.config.context_window

    def refresh_status(self) -> None:
        self.app._refresh_status("已连接")

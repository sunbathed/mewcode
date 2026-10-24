"""第 13 章：统一 Agent 工具——把 Agent 包装成工具。

核心洞察：Agent 和 Tool 的抽象是同构的——都有名字、描述、接受参数、
返回结果。那么把 Agent 包装成一个 Tool 注册到 ToolRegistry，主 Agent
推理时判断子任务应交给专门 Agent 时，调用它就像调用 Bash 一样自然。

一个统一 Agent 工具，通过 subagent_type 参数分流两条路径：
  - 定义式（指定 subagent_type）：从 AgentDef 创建新 Agent，空白对话
  - Fork 式（留空）：继承父 Agent 完整对话历史，无条件后台运行

工具列表始终稳定（不随 Agent 定义文件增减变化），主 Agent 完全
不需要知道调用子 Agent 与调用普通工具有什么区别。

宿主模式：AgentTool 不持有固定的父 Agent（每次 _ask 都会新建），
而从宿主（App/TUI）实时获取当前 Agent 与环境资源：config / workdir /
session / hooks / permission / client / skill 状态。
"""

from __future__ import annotations

import logging
from typing import Any

from ..engine.agent import Agent, AgentConfig
from ..permission import PermissionChecker, PermissionMode, parse_mode
from ..session import Session
from ..tools.base import BaseTool, ToolResult
from .fork import build_forked_messages
from .loader import AgentLoader
from .task_manager import TaskManager
from .tool_filter import resolve_agent_tools
from .trace import TraceRegistry
from .types import AgentDef, AgentToolParams, ForkError, SOURCE_BUILTIN

logger = logging.getLogger(__name__)

# 错误文本前缀：run() 据此把「无法执行」的结果标记为失败
_ERROR_PREFIX = "Unknown agent type"

# 模型别名 → 具体模型名（文档规定的内置映射表）
MODEL_MAP: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-5",
    "opus": "claude-opus-4-6",
}

# 后台任务的默认超时阈值（超过后切后台）——本实现由调用方决定，
# 常量保留供未来「自动超时切后台」功能使用
AUTO_BACKGROUND_MS = 120_000

AGENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "给子 Agent 的任务指令（必填）。",
        },
        "description": {
            "type": "string",
            "description": "任务用途说明，帮助主 Agent 判断该把什么任务交给子 Agent（必填）。",
        },
        "subagent_type": {
            "type": "string",
            "description": "预定义的子 Agent 类型（explore / plan / general-purpose 等）；留空时走 Fork 路径，继承当前对话历史。",
        },
        "model": {
            "type": "string",
            "description": "覆盖定义文件里的模型（haiku / sonnet / opus）。",
        },
        "run_in_background": {
            "type": "boolean",
            "description": "true 时后台异步执行，立即返回任务 ID；默认前台同步等待结果。",
        },
        "name": {
            "type": "string",
            "description": "给子 Agent 命名的展示名（便于在任务列表里区分）。",
        },
        "isolation": {
            "type": "string",
            "description": '文件系统隔离级别：设为 "worktree" 时子 Agent 在独立的 Git Worktree 中工作（并行修改文件互不冲突）；留空共享当前工作目录。',
        },
        "team_name": {
            "type": "string",
            "description": (
                "第 15 章：把子 Agent 派生为该团队的队员（先用 TeamCreate 创建团队）。"
                "队员自动获得独立 worktree 与协作工具（共享任务板 + SendMessage），"
                "在独立上下文中自主工作，空闲时通知你。"
            ),
        },
    },
    "required": ["prompt", "description"],
}


class AgentTool(BaseTool):
    """统一 Agent 工具：通过 subagent_type 选择预定义角色，留空走 Fork。

    绑定宿主（App 或 TUI）：环境资源（config / workdir / session / hooks /
    permission / client / skill 状态）从宿主实时获取，因此工具注册一次即可，
    不随主 Agent 的创建销毁而失效。
    """

    name = "Agent"
    description = (
        "启动一个子 Agent 在独立上下文中完成任务。指定 subagent_type 使用预定义"
        "角色（explore / plan / general-purpose），留空则 Fork 当前对话。"
        "适合需要隔离上下文的子任务。"
    )
    input_schema = AGENT_TOOL_SCHEMA
    safety = "read"

    def __init__(
        self,
        host: Any,
        loader: AgentLoader,
        task_manager: TaskManager,
        trace: TraceRegistry | None = None,
        *,
        enable_fork: bool = True,
    ) -> None:
        self._host = host
        self._loader = loader
        self._task_manager = task_manager
        self._trace = trace or TraceRegistry()
        self._enable_fork = enable_fork

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(self, args: dict[str, Any]) -> ToolResult:
        """执行 Agent 工具：无法执行（未知类型等）标记为失败结果。

        覆盖 BaseTool.run 是为了把「Unknown agent type」这类调用参数
        错误标记为 is_error——模型看到失败结果会调整策略（换个类型）。
        """
        result = await super().run(args)
        if result.output.startswith(_ERROR_PREFIX):
            result.is_error = True
            result.error = result.output
        return result

    async def _run(self, args: dict[str, Any]) -> str:
        """Agent 工具执行：按参数分发到 Team / 定义式 / Fork 三条路径。"""
        params = AgentToolParams(
            prompt=str(args.get("prompt", "")),
            description=str(args.get("description", "")),
            subagent_type=str(args.get("subagent_type", "")),
            model=str(args.get("model", "")),
            run_in_background=bool(args.get("run_in_background", False)),
            name=str(args.get("name", "")),
            isolation=str(args.get("isolation", "")),
            team_name=str(args.get("team_name", "")),
        )
        if not params.prompt:
            return "Error: 'prompt' is required."

        # 第 15 章：Team 路径——team_name 非空，子 Agent 成为团队队员
        # （队员本质上都是后台异步运行：完成时通过 mailbox 通知 Lead）
        if params.team_name:
            from ..teams import TeammateSpawner

            spawner = getattr(self._host, "team_spawner", None)
            if spawner is None:
                spawner = TeammateSpawner(self._host, self)
            return await spawner.spawn(params)

        # 定义式路径：指定了 subagent_type
        if params.subagent_type:
            definition = self._loader.get(params.subagent_type)
            if definition is None:
                return (
                    f"Unknown agent type: '{params.subagent_type}'. "
                    f"Available types: {self._loader.available_types_text()}"
                )
            return await self._execute_defined(definition, params)

        # Fork 路径：未指定 subagent_type
        return await self._execute_fork(params)

    # ------------------------------------------------------------------
    # 宿主环境
    # ------------------------------------------------------------------

    def _parent_agent(self) -> Agent | None:
        """当前运行的主 Agent（App 每次 _ask 设置；TUI 无 engine.Agent 时为 None）。"""
        return getattr(self._host, "current_agent", None)

    def _host_config(self):
        return getattr(self._host, "config", None)

    def _host_workdir(self) -> str:
        return getattr(self._host, "workdir", "") or ""

    def _host_session(self) -> Session | None:
        return getattr(self._host, "session", None)

    def _host_client(self):
        return getattr(self._host, "client", None)

    def _host_hooks(self):
        return getattr(self._host, "hooks", None)

    def _host_permission(self) -> PermissionChecker | None:
        return getattr(self._host, "permission", None)

    def _host_registry(self):
        """未缩减的完整工具集（Skill allowedTools 过滤前的版本）。"""
        return getattr(self._host, "registry", None)

    def _bind_skill_context(self, agent: Agent) -> None:
        """把宿主的 Skill 激活状态与工具过滤绑定到子 Agent（父的 Skill 对子生效）。"""
        host = self._host
        skill_host = getattr(host, "skill_host", None)
        if skill_host is None:
            return
        state = getattr(skill_host, "state", None)
        if state is None:
            return
        # filter_registry_by_active 是无参绑定方法，包一层适配 tool_filter 的签名
        def _tool_filter(registry):
            try:
                return skill_host.filter_registry_by_active()
            except Exception:  # noqa: BLE001 - 过滤失败不阻断子 Agent 构建
                return None

        agent.bind_skill_context(state, tool_filter=_tool_filter)

    # ------------------------------------------------------------------
    # 定义式路径
    # ------------------------------------------------------------------

    async def _execute_defined(self, definition: AgentDef, params: AgentToolParams) -> str:
        """定义式：空白对话 + 固定角色 + 工具过滤 + 权限模式。"""
        # 第 14 章：声明了 worktree 隔离 → 走独立工作目录执行路径
        isolation = params.isolation or definition.isolation
        if isolation == "worktree":
            return await self._execute_with_worktree(definition, params)
        parent = self._parent_agent()
        config = self._host_config()
        if config is None:
            return "Error: agent host is not initialized."
        trace_id = self._start_trace(definition, params)

        # 1. 选择 LLM 客户端（参数模型 > 定义模型 > 宿主）
        client = self._select_llm(config, params, definition)

        # 2. 过滤工具（以未缩减的完整工具集为起点）
        base_registry = self._host_registry()
        if base_registry is None:
            base_registry = getattr(parent, "_full_registry", None) if parent else None
        is_background = params.run_in_background or definition.background
        filtered_registry = resolve_agent_tools(
            base_registry, definition, is_background=is_background
        )

        # 3. 权限检查器（dontAsk 自动批准；沿用宿主工作目录）
        checker = self._make_permission_checker(definition)
        if parent is not None:
            # 继承父 Agent 当前 permissions 模式以外的规则（工作目录一致）
            pass

        # 4. 创建子 Agent 实例（空白会话 + 定义的系统提示）
        model_name = getattr(client, "config", None)
        session_model = model_name.model if model_name is not None else getattr(config, "model", "")
        sub_agent = self._build_agent(
            definition=definition,
            registry=filtered_registry,
            client=client,
            permission=checker,
            session=Session(model=session_model),
        )

        # 5. 前台 or 后台执行
        if is_background:
            task_id = self._task_manager.launch(
                agent=sub_agent,
                task=params.prompt,
                name=params.name or definition.agent_type,
            )
            self._complete_trace(trace_id, status="running")
            return (
                f"Sub-agent launched in background.\n"
                f"Task ID: {task_id}\n"
                f"Type: {definition.agent_type}\n"
                f"Do NOT wait, sleep, or poll."
            )

        try:
            result_text = await sub_agent.run_to_completion(params.prompt)
            self._complete_trace(
                trace_id,
                status="completed",
                tool_calls=_sub_agent_tool_calls(sub_agent),
                input_tokens=getattr(sub_agent, "total_input_tokens", 0) or 0,
                output_tokens=getattr(sub_agent, "total_output_tokens", 0) or 0,
            )
            return result_text
        except Exception as exc:  # noqa: BLE001 - 子 Agent 失败返回错误文本
            self._complete_trace(trace_id, status="failed")
            return f"Sub-agent failed: {exc}"

    # ------------------------------------------------------------------
    # 第 14 章：Worktree 隔离路径
    # ------------------------------------------------------------------

    async def _execute_with_worktree(self, definition: AgentDef, params: AgentToolParams) -> str:
        """Worktree 隔离：子 Agent 在独立 Git Worktree 中运行，文件修改互不冲突。

        完整生命周期由 execute_with_worktree 编排（创建 → 注入上下文通知
        → 运行 → 自动清理）；本方法只注入「如何构建并运行子 Agent」的
        回调。后台模式下整个生命周期（含清理）都在后台协程里跑。
        """
        from ..worktree import execute_with_worktree

        is_background = params.run_in_background or definition.background
        manager = getattr(self._host, "worktree_manager", None)

        async def _whole() -> str:
            async def _run(worktree_path: str, task: str) -> str:
                return await self._run_worktree_sub_agent(
                    definition, params, worktree_path, task, is_background=is_background
                )

            return await execute_with_worktree(
                manager,
                self._host_workdir(),
                definition,
                params,
                run_sub_agent=_run,
            )

        if is_background:
            task_id = self._task_manager.launch_fn(
                _whole, name=params.name or definition.agent_type
            )
            return (
                f"Sub-agent launched in background (worktree isolation).\n"
                f"Task ID: {task_id}\n"
                f"Type: {definition.agent_type}\n"
                f"Do NOT wait, sleep, or poll."
            )
        return await _whole()

    async def _run_worktree_sub_agent(
        self,
        definition: AgentDef,
        params: AgentToolParams,
        worktree_path: str,
        task: str,
        *,
        is_background: bool = False,
    ) -> str:
        """以 worktree 为根构建子 Agent 并跑完任务（由 execute_with_worktree 回调）。"""
        config = self._host_config()
        if config is None:
            return "Error: agent host is not initialized."
        trace_id = self._start_trace(definition, params)

        client = self._select_llm(config, params, definition)
        # 显式 cwd 机制：核心文件工具以 worktree 为根重建，子 Agent 的
        # 每次文件操作都落在隔离目录里
        from ..worktree import reroot_registry

        wt_registry = reroot_registry(self._host_registry(), worktree_path)
        filtered_registry = resolve_agent_tools(wt_registry, definition, is_background=is_background)
        # 权限检查器同样以 worktree 为根：worktree 内的写操作是合法的
        checker = self._make_permission_checker(definition, workdir=worktree_path)

        model_name = getattr(client, "config", None)
        session_model = model_name.model if model_name is not None else getattr(config, "model", "")
        sub_agent = self._build_agent(
            definition=definition,
            registry=filtered_registry,
            client=client,
            permission=checker,
            session=Session(model=session_model),
            workdir=worktree_path,
        )
        try:
            result_text = await sub_agent.run_to_completion(task)
        except Exception:
            self._complete_trace(trace_id, status="failed")
            raise
        self._complete_trace(
            trace_id,
            status="completed",
            tool_calls=_sub_agent_tool_calls(sub_agent),
            input_tokens=getattr(sub_agent, "total_input_tokens", 0) or 0,
            output_tokens=getattr(sub_agent, "total_output_tokens", 0) or 0,
        )
        return result_text

    # ------------------------------------------------------------------
    # Fork 路径
    # ------------------------------------------------------------------

    async def _execute_fork(self, params: AgentToolParams) -> str:
        """Fork 式：继承父对话历史，无条件后台运行。"""
        if not self._enable_fork:
            return "Fork mode is not enabled."
        parent = self._parent_agent()
        config = self._host_config()
        if config is None:
            return "Error: agent host is not initialized."
        parent_session = self._host_session()
        if parent_session is None:
            return "Cannot fork: no active conversation."

        try:
            fork_session = build_forked_messages(parent_session, params.prompt)
        except ForkError as exc:
            return f"Cannot fork: {exc}"

        # 临时 AgentDef：Fork 子 Agent 的行为靠 Boilerplate 约束
        system_prompt = None
        if parent is not None:
            try:
                system_prompt = parent._system_prompt()
            except Exception:  # noqa: BLE001
                system_prompt = None
        definition = AgentDef(
            agent_type="fork",
            when_to_use="Forked from parent agent",
            system_prompt=system_prompt or "",
            disallowed_tools=[],
            model="inherit",
            max_turns=getattr(parent, "agent_config", None)
            and parent.agent_config.max_iterations
            or 50,
            permission_mode="dontAsk",
            source=SOURCE_BUILTIN,
        )
        trace_id = self._start_trace(definition, params)

        client = self._select_llm(config, params, definition)
        base_registry = self._host_registry()
        if base_registry is None:
            base_registry = getattr(parent, "_full_registry", None) if parent else None
        # Fork 在后台运行：后台白名单过滤
        filtered_registry = resolve_agent_tools(base_registry, definition, is_background=True)
        checker = self._make_permission_checker(definition)

        sub_agent = self._build_agent(
            definition=definition,
            registry=filtered_registry,
            client=client,
            permission=checker,
            session=fork_session,
        )

        task_id = self._task_manager.launch(
            agent=sub_agent,
            task="",
            name=params.name or "fork",
            fork_conversation=fork_session,
        )
        self._complete_trace(trace_id, status="running")
        return (
            f"Fork sub-agent launched in background.\n"
            f"Task ID: {task_id}\n"
            f"Do NOT wait, sleep, or poll."
        )

    # ------------------------------------------------------------------
    # 组装辅助
    # ------------------------------------------------------------------

    def _select_llm(self, config, params: AgentToolParams, definition: AgentDef):
        """模型选择：参数级 > 定义级 > 宿主。"""
        model_override = params.model
        if not model_override and definition.model != "inherit":
            model_override = definition.model
        if not model_override or model_override == "inherit":
            return self._host_client()
        resolved = MODEL_MAP.get(model_override, model_override)
        client = self._create_client_for_model(config, resolved)
        return client if client is not None else self._host_client()

    def _create_client_for_model(self, config, model: str):
        """为一个模型别名创建独立客户端（保持与宿主相同的配置端点）。"""
        from ..llm_client import OpenAICompatibleClient

        try:
            from dataclasses import replace

            new_config = replace(config, model=model)
        except Exception:  # noqa: BLE001 - ProviderConfig 不可替换时退化为宿主配置
            new_config = config
        return OpenAICompatibleClient(new_config, tools=None)

    def _make_permission_checker(self, definition: AgentDef, workdir: str | None = None) -> PermissionChecker:
        """按 AgentDef.permission_mode 构造权限检查器。

        workdir 默认宿主工作目录；worktree 隔离时传 worktree 路径，
        让「项目边界」跟着子 Agent 的实际工作目录走。
        """
        # dontAsk：跳过所有权限询问（Fork 默认；工具过滤已锁死能力边界）
        mode = definition.permission_mode
        if mode == "dontAsk":
            mode = "bypassPermissions"
        parsed = parse_mode(mode, default=PermissionMode.DEFAULT)
        return PermissionChecker(
            workdir=workdir if workdir is not None else self._host_workdir(), mode=parsed
        )

    def _build_agent(
        self,
        *,
        definition: AgentDef,
        registry,
        client,
        permission,
        session,
        workdir: str | None = None,
    ) -> Agent:
        """构造子 Agent 实例：复用宿主的全局设施，隔离运行时状态。"""
        sub_agent = Agent(
            self._host_config(),
            session=session,
            registry=registry,
            workdir=workdir if workdir is not None else self._host_workdir(),
            client=client,
            agent_config=AgentConfig(
                max_iterations=definition.max_turns,
                system_prompt=definition.system_prompt or "",
                custom_instructions="",
                memory_instructions="",
                skill_catalog="",
            ),
            cancel_event=None,
            permission=permission,
            context=None,  # 子 Agent 不共享宿主上下文管理（会话隔离）
            hooks=self._host_hooks(),  # Hook 引擎共享：格式化/拦截规则对子 Agent 同样生效
        )
        # 继承宿主 Skill 上下文绑定（激活的 Skill 与工具过滤对子生效）
        self._bind_skill_context(sub_agent)
        return sub_agent

    # ------------------------------------------------------------------
    # 追踪
    # ------------------------------------------------------------------

    def _start_trace(self, definition: AgentDef, params: AgentToolParams) -> str:
        parent = self._parent_agent()
        parent_id = getattr(parent, "_trace_id", "") if parent is not None else ""
        trace_id = self._trace.start_agent(
            parent_id=parent_id,
            agent_type=definition.agent_type,
            name=params.name or definition.agent_type,
        )
        return trace_id

    def _complete_trace(
        self,
        trace_id: str,
        *,
        status: str,
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self._trace.complete_agent(
            trace_id,
            status=status,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _sub_agent_tool_calls(agent: Agent) -> int:
    """统计子 Agent 执行期间的工具调用次数。"""
    return getattr(agent, "total_tool_calls", 0) or 0


__all__ = [
    "AGENT_TOOL_SCHEMA",
    "AUTO_BACKGROUND_MS",
    "AgentTool",
    "MODEL_MAP",
]
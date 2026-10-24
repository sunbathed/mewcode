"""SkillHost：App/TUI 共用的 Skill 系统集成层。

把 loader（加载）、activation（激活状态）、executor（执行）、LoadSkill
工具、allowedTools 过滤全部串起来，暴露给 App/TUI 使用：

  - bind(app, registry)：绑定宿主与主注册中心，注册 LoadSkill 工具
  - register_command(registry)：把所有 Skill 自动注册为 Slash Command
  - activate(name)：LoadSkill 工具用——钉 SOP 到 env context
  - run_skill_inline / run_fork：两种执行模式的宿主实现
  - filter_registry(allowed_tools)：allowedTools 过滤 + 系统工具豁免
"""

from __future__ import annotations

import logging
from typing import Any

from ..commands import Command, CommandRegistry, CommandType
from ..tools import ToolRegistry
from .executor import SkillExecutor, SkillResult
from .loader import SkillLoader, SkillNotFoundError
from .state import SkillActivationState
from .tools import LoadSkillTool, ScriptTool

logger = logging.getLogger(__name__)

# 系统工具：不受 allowedTools 约束（Skill 嵌套的关键）
SYSTEM_TOOL_NAMES = {"LoadSkill": True}


class SkillDefinitionError(RuntimeError):
    """Skill 定义错误（allowedTools 引用了不存在的工具等）。"""


class SkillHost:
    """Skill 系统与 App/TUI 的集成宿主。"""

    def __init__(self, project_root: str | Any | None = None, *, loader: SkillLoader | None = None) -> None:
        self.loader = loader or SkillLoader(project_root=project_root)
        self.state = SkillActivationState()
        self.executor: SkillExecutor | None = None
        self._app = None          # App 或 TUI 实例（提供 session/_ask/_run_sub_agent 等）
        self._main_registry: ToolRegistry | None = None

    def bind(self, app: Any, main_registry: ToolRegistry) -> None:
        """绑定宿主 App/TUI 与主注册中心，注册 LoadSkill 系统工具。"""
        self._app = app
        self._main_registry = main_registry
        # 兼容 executor 的 agent_factory 访问（session / registry）
        self.registry = main_registry
        self.session = getattr(app, "session", None)
        self.executor = SkillExecutor(self.loader, agent_factory=self, fork_runner=self.run_fork)
        if "LoadSkill" not in main_registry.names():
            main_registry.register(LoadSkillTool(self.loader, self.state, tool_host=self))

    # ------------------------------------------------------------------
    # 工具注册 / 过滤
    # ------------------------------------------------------------------

    def has_tool(self, name: str) -> bool:
        """LoadSkill fail-fast：allowedTools 里的工具是否都存在。"""
        if self._main_registry is None:
            return False
        try:
            self._main_registry.get(name)
            return True
        except Exception:
            return False

    def register_skill_tools(self, skill: Any) -> list[str]:
        """注册目录型 Skill 的 tool.json 专属工具，返回新注册的工具名。"""
        if self._main_registry is None or not skill.tools:
            return []
        registered: list[str] = []
        for tool_def in skill.tools:
            script = self.loader.resolve_tool_script(skill, tool_def)
            if script is None:
                logger.warning("Skill %s 工具 %s 的实现脚本缺失，跳过注册", skill.name, tool_def.name)
                continue
            try:
                self._main_registry.register(
                    ScriptTool(tool_def.name, tool_def.description, tool_def.parameters, script)
                )
                registered.append(tool_def.name)
            except ValueError:
                registered.append(tool_def.name)  # 重复加载同名工具：幂等
        return registered

    def filter_registry(self, allowed_tools: tuple[str, ...]) -> ToolRegistry | None:
        """allowedTools 过滤：新建一个只含白名单工具 + 系统工具的注册中心。

        空列表不限制（返回 None，调用方直接用完整工具集）；非空先做
        fail-fast 依赖检查，缺工具立即报错。白名单里可以包含 Skill 自己
        通过 tool.json 注册的专属工具（它们在主 registry 里）。
        """
        if self._main_registry is None or not allowed_tools:
            return None
        missing = [name for name in allowed_tools if not self.has_tool(name)]
        if missing:
            raise SkillDefinitionError(
                f"Skill 声明了不存在的工具：{', '.join(missing)}。请修复后 /skill reload。"
            )
        filtered = ToolRegistry(workdir=getattr(self._app, "workdir", None))
        # ToolRegistry 构造会自动注册 7 个基础工具，先清掉不在白名单里的
        keep = set(allowed_tools) | set(SYSTEM_TOOL_NAMES)
        for name in list(filtered.names()):
            if name not in keep:
                filtered.unregister(name)
        for name in allowed_tools:
            if name in SYSTEM_TOOL_NAMES:
                continue
            try:
                filtered.register(self._main_registry.get(name))
            except Exception:
                continue
        # 系统工具始终可见（支持 Skill 嵌套）
        for name in SYSTEM_TOOL_NAMES:
            try:
                filtered.register(self._main_registry.get(name))
            except Exception:
                pass
        return filtered

    # ------------------------------------------------------------------
    # LoadSkill 激活
    # ------------------------------------------------------------------

    def activate(self, skill_name: str) -> str:
        """LoadSkill 工具调用：把完整 SOP 钉到 env context。"""
        try:
            skill = self.loader.get(skill_name)
        except SkillNotFoundError as exc:
            return f"错误：{exc}"
        # 专属工具（tool.json 声明的）由本方法注册，它们不参与 fail-fast
        own_tool_names = {t.name for t in skill.tools}
        missing = [
            name
            for name in skill.allowed_tools
            if name not in own_tool_names and not self.has_tool(name)
        ]
        if missing:
            return f"错误：Skill '{skill_name}' 声明了不存在的工具：{', '.join(missing)}。"
        registered = self.register_skill_tools(skill)
        rendered = skill.render("")
        self.state.activate(
            skill_name,
            rendered,
            tool_names=registered,
            allowed_tools=skill.allowed_tools,
        )
        note = f"，注册 {len(registered)} 个专属工具" if registered else ""
        return f"Skill {skill_name} 已激活{note}。SOP 已钉到环境上下文。"

    def filter_registry_by_active(self) -> ToolRegistry | None:
        """按当前所有激活 Skill 的 allowedTools 并集过滤工具集。

        Agent 每轮构建工具列表时调用；没有激活 Skill 或它们的白名单
        为空时不限制（返回 None，用完整工具集）。
        """
        if self._main_registry is None:
            return None
        allowed = self.state.allowed_tools_union()
        if not allowed:
            return None
        return self.filter_registry(allowed)

    # ------------------------------------------------------------------
    # 执行（inline / fork）
    # ------------------------------------------------------------------

    async def run_skill_inline(self, skill: Any, prompt: str, args: str) -> None:
        """inline：把渲染后的 SOP 注入当前会话，走 Agent Loop。"""
        if self._app is None:
            return
        # 激活 SOP 到 env context（本轮之后的迭代持续可见）
        self.state.activate(skill.name, prompt, allowed_tools=skill.allowed_tools)
        await self._app._ask(prompt)

    async def run_fork(self, skill: Any, prompt: str, context_messages: list[Any]) -> str:
        """fork：独立上下文执行，结果回流主对话。"""
        if self._app is None or self._main_registry is None:
            return "错误：fork 执行需要宿主。"
        result_text = await self._app._run_sub_agent(skill, prompt, context_messages, self)
        return result_text

    # ------------------------------------------------------------------
    # Slash Command 自动注册
    # ------------------------------------------------------------------

    def register_command(self, registry: CommandRegistry) -> None:
        """为所有 Skill 注册 PROMPT 类型 Slash Command（/name，标 [skill]）。

        与内置命令重名的 Skill 跳过命令注册（意图识别 LoadSkill 仍可用），
        保证 /help 唯一性。闭包显式拷贝 _name，避免循环变量捕获陷阱。
        """
        for skill in self.loader.load_catalog():
            name = skill.name
            if registry.find(name) is not None:
                logger.warning("Skill %s 与内置命令冲突，跳过命令注册（意图识别仍可用）", name)
                continue

            async def _handler(ctx, _name=name):
                result = await self.execute_skill(_name, ctx.args)
                if result.text and ctx.ui is not None:
                    ctx.ui.add_system_message(result.text)

            registry.register_sync(
                Command(
                    name=name,
                    description=f"{skill.description} [skill]",
                    type=CommandType.PROMPT,
                    handler=_handler,
                    usage=f"/{name} [参数]",
                )
            )

    async def execute_skill(self, name: str, args: str = "") -> SkillResult:
        if self.executor is None:
            return SkillResult(text="错误：Skill 系统未初始化。", mode="error")
        return await self.executor.execute(name, args)

    def reload(self) -> list[str]:
        return self.loader.reload()


__all__ = [
    "SYSTEM_TOOL_NAMES",
    "SkillDefinitionError",
    "SkillHost",
]
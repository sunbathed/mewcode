"""LoadSkill 系统工具 + 目录型 Skill 专属工具。

LoadSkill 是 Skill 系统的运行基础设施，作用对象是 Agent 自己
（加载 SOP、注册工具，副作用只在 Agent 内部循环），所以它是
「系统工具」，不受 allowedTools 白名单约束——Skill 之间可以嵌套触发。

目录型 Skill 的 tool.json 声明的专属工具用 ScriptTool 承载：
给一个名字 + schema + references/ 下的实现脚本，就变成一个可执行的工具。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..tools.base import SAFETY_COMMAND, SAFETY_READ, BaseTool, ToolResult
from .loader import SkillLoader, SkillNotFoundError


class LoadSkillTool(BaseTool):
    """两阶段加载第二阶段：把完整 SKILL.md 钉到 env context + 注册专属工具。

    标记为 read-only（safety=read），走只读权限通道，不被权限系统拦截：
    它没有外部副作用，只改 Agent 自己的对话状态。
    """

    name = "LoadSkill"
    description = (
        "加载并激活一个 Skill：把该 Skill 的完整 SOP 指令钉到环境上下文，"
        "同时注册它声明的专属工具。参数 name 为 Skill 名称（如 commit / review）。"
    )
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "要加载的 Skill 名称"}},
        "required": ["name"],
    }
    safety = SAFETY_READ

    def __init__(
        self,
        loader: SkillLoader,
        activation_state: Any,
        *,
        tool_host: Any = None,
    ) -> None:
        """tool_host 负责把专属工具真正注册进会话注册中心。"""
        super().__init__()
        self.loader = loader
        self.state = activation_state
        self.tool_host = tool_host

    async def _run(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip().lower()
        if not name:
            return "错误：LoadSkill 需要参数 name（Skill 名称）。"
        try:
            skill = self.loader.get(name)
        except SkillNotFoundError as exc:
            return f"错误：{exc}"

        # fail-fast：检查 allowedTools 里的每个工具是否存在
        missing = self._check_allowed_tools(skill.allowed_tools)
        if missing:
            return (
                f"错误：Skill '{name}' 声明了不存在的工具：{', '.join(missing)}。"
                "请修复 Skill 定义后重试 /skill reload。"
            )

        # ActivateSkill：把完整 SOP 钉到 env context（下一轮迭代起持久可见）
        rendered = skill.render("")
        registered: list[str] = []
        if self.tool_host is not None:
            registered = self.tool_host.register_skill_tools(skill) or []
        self.state.activate(name, rendered, tool_names=registered)

        tool_note = f"，注册 {len(registered)} 个专属工具" if registered else ""
        return f"Skill {name} 已激活{tool_note}。SOP 已钉到环境上下文，下一轮迭代起生效。"

    def _check_allowed_tools(self, allowed_tools: tuple[str, ...]) -> list[str]:
        """确认 allowedTools 里的每个名字都能在主注册中心找到。"""
        if self.tool_host is None or not allowed_tools:
            return []
        missing = []
        for tool_name in allowed_tools:
            if not self.tool_host.has_tool(tool_name):
                missing.append(tool_name)
        return missing


class ScriptTool(BaseTool):
    """目录型 Skill 的 tool.json 声明的专属工具：schema + 实现脚本。

    执行方式：用 python 解释器跑 references/ 下的脚本，脚本拿到 JSON 参数
    从 stdin 读，结果输出到 stdout。
    """

    safety = SAFETY_COMMAND

    def __init__(self, name: str, description: str, input_schema: dict[str, Any], script: Path) -> None:
        super().__init__()
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._script = script

    async def _run(self, args: dict[str, Any]) -> str:
        payload = json.dumps(args, ensure_ascii=False)
        cmd = [sys.executable, str(self._script), payload]
        try:
            proc = await asyncio_create_subprocess(cmd, payload)
        except OSError as exc:
            return f"专属工具执行失败：{exc}"
        return proc


async def asyncio_create_subprocess(cmd: list[str], payload: str) -> str:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(payload.encode("utf-8"))
    if proc.returncode != 0:
        return f"错误（退出码 {proc.returncode}）：{stderr.decode('utf-8', errors='replace')[:500]}"
    return stdout.decode("utf-8", errors="replace").strip()


__all__ = ["LoadSkillTool", "ScriptTool"]
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..session import ToolUseBlock
from ..tools import ToolRegistry

# 只读模式：规划阶段仅开放这些工具；其余工具一律拦截并提示
PLAN_MODE_TOOLS = {"ReadFile", "Glob", "Grep"}
PLAN_DIR_NAME = ".mewcode/plans"
PLAN_FILE_SUFFIX = ".plan.md"
PLAN_WRITE_TOOL = "WriteFile"

# 注入 System Prompt 的规划指令（保留向后兼容）
PLAN_MODE_INSTRUCTIONS = """Plan mode is active. 当前处于规划模式，你只能做只读探索：
1. 用 ReadFile、Glob、Grep（以及 Bash 只读命令）探索项目结构，理解需求；
2. 分析用户需求，设计实现方案；
3. 把完整的开发计划写入指定的 plan 文件（唯一允许写入的文件）；
4. 等待用户确认后再执行。
严禁执行任何修改操作：不能编辑代码、不能创建项目文件、不能修改配置。"""

# 第五阶段重构：Plan Mode 三段式模板（通过 system-reminder 动态注入，不破坏 System Prompt 缓存）
PLAN_MODE_FULL_REMINDER = """# 规划模式（Plan Mode）进行中

你当前处于只读规划模式。你的目标是理解用户需求、探索代码库并制定详细实现计划。
严禁在此模式下修改任何项目代码或配置。唯一允许写入的目标是指定的 plan 文件。

## 五阶段工作流：
1. 探索（Exploration）：使用 ReadFile、Glob、Grep 查看项目结构，定位相关代码。
2. 分析（Analysis）：理清现有代码逻辑、依赖关系和潜在风险。
3. 起草（Drafting）：{plan_action}，包含改动范围、实现步骤与验证方案。
4. 确认（Confirmation）：向用户展示计划要点，询问是否有需要调整的地方。
5. 就绪（Ready）：等待用户退出规划模式（/do）或明确授权后再进行实施。

当前计划文件路径：{plan_file_path}
{plan_status_hint}"""

PLAN_MODE_SPARSE_REMINDER = """# 规划模式提醒
当前处于只读规划模式。请继续分析或更新计划文件：{plan_file_path}。严禁修改项目代码。"""

PLAN_MODE_EXIT_REMINDER = """规划模式已退出，所有工具权限已恢复正常。你可以开始按照计划执行修改。"""


def build_plan_mode_reminder(
    plan_file_path: str,
    plan_exists: bool = False,
    iteration: int = 1,
    reminder_interval: int = 5,
) -> str:
    """按轮次生成 Plan Mode 提醒文本：第 1 轮完整版，之后每 reminder_interval 轮重复完整版，其余精简版。"""
    is_full = (iteration <= 1) or ((iteration - 1) % reminder_interval == 0)
    if is_full:
        plan_action = "阅读并深化已有计划文件" if plan_exists else "使用 WriteFile 将完整计划写入计划文件"
        plan_status_hint = (
            "注意：计划文件已存在，请先读取该文件以了解当前规划进度。"
            if plan_exists
            else "注意：计划文件尚未创建，在完成探索分析后请创建并写入该文件。"
        )
        return PLAN_MODE_FULL_REMINDER.format(
            plan_file_path=plan_file_path,
            plan_action=plan_action,
            plan_status_hint=plan_status_hint,
        )
    return PLAN_MODE_SPARSE_REMINDER.format(plan_file_path=plan_file_path)



def _safe_plan_dir(workdir: str | None) -> Path:
    base = Path(workdir).expanduser() if workdir else Path.cwd()
    return base / PLAN_DIR_NAME


@dataclass(slots=True)
class PlanModeState:
    """规划模式的运行时状态：开关、工作目录与当前 plan 文件路径。"""

    active: bool = False
    workdir: str | None = None
    plan_file: str | None = None
    _plan_counter: int = field(default=0, repr=False)

    def enter(self, workdir: str | None) -> str:
        """进入规划模式：创建 plans 目录，生成一份新 plan 文件。"""
        self.active = True
        self.workdir = workdir
        directory = _safe_plan_dir(workdir)
        directory.mkdir(parents=True, exist_ok=True)
        self._plan_counter += 1
        name = f"plan-{self._plan_counter:03d}{PLAN_FILE_SUFFIX}"
        path = directory / name
        self.plan_file = str(path)
        return self.plan_file

    def exit(self) -> None:
        self.active = False
        self.workdir = None
        self.plan_file = None


def build_plan_mode_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """规划模式下只向模型暴露只读工具（仅启用中且属于只读集合的）。"""
    return [tool.to_api_format() for tool in registry.enabled_tools() if tool.name in PLAN_MODE_TOOLS]


def _is_write_to_plan_file(state: PlanModeState, use: ToolUseBlock) -> bool:
    """判断某个工具调用是否是「写入当前 plan 文件」（唯一放行的写操作）。"""
    if not state.active or use.name != PLAN_WRITE_TOOL or not state.plan_file:
        return False
    target = str(use.input.get("path", "")).strip()
    if not target:
        return False
    resolved = Path(target).expanduser()
    if not resolved.is_absolute():
        base = Path(state.workdir).expanduser() if state.workdir else Path.cwd()
        resolved = base / resolved
    return str(resolved).lower() == str(Path(state.plan_file).expanduser()).lower()


def intercept_write(state: PlanModeState, use: ToolUseBlock) -> str | None:
    """规划模式下拦截写类工具（除 plan 文件外），返回提示文本；放行返回 None。"""
    if not state.active:
        return None
    if use.name not in ("WriteFile", "EditFile", "ReplaceInFile", "Bash"):
        return None
    if _is_write_to_plan_file(state, use):
        return None
    return (
        f"规划模式下不允许执行修改操作（{use.name} 被拦截）。"
        f"如需执行，请退出规划模式：/do。唯一允许写入的是 plan 文件：{state.plan_file}。"
    )


def save_plan_file(plan_file: str, content: str) -> Path:
    """把模型生成的计划写入 plan 文件（写入前创建目录）。"""
    path = Path(plan_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read_plan_file(plan_file: str) -> str | None:
    """读取 plan 文件内容；不存在返回 None。"""
    path = Path(plan_file).expanduser()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_plan_files(workdir: str | None) -> list[str]:
    """列出工作目录下已生成的 plan 文件（文件名列表）。"""
    directory = _safe_plan_dir(workdir)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.glob(f"*{PLAN_FILE_SUFFIX}"))


def ensure_plan_dir(workdir: str | None) -> Path:
    directory = _safe_plan_dir(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


__all__ = [
    "PlanModeState",
    "PLAN_MODE_INSTRUCTIONS",
    "PLAN_MODE_FULL_REMINDER",
    "PLAN_MODE_SPARSE_REMINDER",
    "PLAN_MODE_EXIT_REMINDER",
    "build_plan_mode_reminder",
    "PLAN_MODE_TOOLS",
    "PLAN_DIR_NAME",
    "PLAN_FILE_SUFFIX",
    "PLAN_WRITE_TOOL",
    "build_plan_mode_tools",
    "intercept_write",
    "save_plan_file",
    "read_plan_file",
    "list_plan_files",
    "ensure_plan_dir",
]


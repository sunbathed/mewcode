from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    priority: int
    content: str


class PromptBuilder:
    def __init__(self) -> None:
        self._sections: list[Section] = []

    def add(self, section: Section) -> PromptBuilder:
        self._sections.append(section)
        return self

    def build(self) -> str:
        # 按 priority 升序排序
        sorted_sections = sorted(self._sections, key=lambda s: s.priority)
        # 过滤空内容
        valid_contents = [s.content.strip() for s in sorted_sections if s.content and s.content.strip()]
        return "\n\n".join(valid_contents)


@dataclass(frozen=True, slots=True)
class EnvironmentContext:
    work_dir: str = ""
    os_name: str = ""
    arch: str = ""
    shell: str = ""
    is_git_repo: bool = False
    git_branch: str = ""
    date: str = ""
    model: str = ""


def _detect_os_name() -> str:
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def _detect_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return Path(shell).name
    if sys.platform == "win32":
        # 常见 Windows 终端
        if os.environ.get("PSModulePath"):
            return "powershell"
        comspec = os.environ.get("COMSPEC")
        return Path(comspec).name if comspec else "cmd"
    return "bash"


def detect_environment(workdir: str | None = None, model: str = "") -> EnvironmentContext:
    resolved_workdir = str(Path(workdir).resolve()) if workdir else os.getcwd()
    os_name = _detect_os_name()
    arch = platform.machine() or "unknown"
    shell = _detect_shell()
    date_str = datetime.now().strftime("%Y-%m-%d")

    is_git = False
    git_branch = ""
    try:
        res = subprocess.run(
            ["git", "-C", resolved_workdir, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip() == "true":
            is_git = True
            branch_res = subprocess.run(
                ["git", "-C", resolved_workdir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if branch_res.returncode == 0:
                git_branch = branch_res.stdout.strip()
    except Exception:
        is_git = False
        git_branch = ""

    return EnvironmentContext(
        work_dir=resolved_workdir,
        os_name=os_name,
        arch=arch,
        shell=shell,
        is_git_repo=is_git,
        git_branch=git_branch,
        date=date_str,
        model=model,
    )


@dataclass(frozen=True, slots=True)
class BuildOptions:
    custom_instructions: str = ""
    skill_instructions: str = ""
    memory_instructions: str = ""


def build_system_prompt(
    env: EnvironmentContext | None = None,
    options: BuildOptions | None = None,
) -> str:
    from .sections import (
        custom_instructions_section,
        doing_tasks_section,
        environment_section,
        executing_actions_section,
        identity_section,
        memory_section,
        output_efficiency_section,
        skill_section,
        system_section,
        tone_style_section,
        using_tools_section,
    )

    builder = PromptBuilder()

    # 1. 七个核心模块
    builder.add(identity_section())
    builder.add(system_section())
    builder.add(doing_tasks_section())
    builder.add(executing_actions_section())
    builder.add(using_tools_section())
    builder.add(tone_style_section())
    builder.add(output_efficiency_section())

    # 2. 环境上下文 (70)
    current_env = env or detect_environment()
    builder.add(environment_section(current_env))

    # 3. 条件注入可选模块 (80 / 90 / 95)
    opts = options or BuildOptions()
    if opts.custom_instructions and opts.custom_instructions.strip():
        builder.add(custom_instructions_section(opts.custom_instructions))
    if opts.skill_instructions and opts.skill_instructions.strip():
        builder.add(skill_section(opts.skill_instructions))
    if opts.memory_instructions and opts.memory_instructions.strip():
        builder.add(memory_section(opts.memory_instructions))

    return builder.build()


__all__ = [
    "Section",
    "PromptBuilder",
    "EnvironmentContext",
    "detect_environment",
    "BuildOptions",
    "build_system_prompt",
]

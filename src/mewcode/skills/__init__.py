"""第 11 章：Skill 技能包系统。

Skill 是写给 Agent 的 SOP（标准操作流程）：把可复用的 prompt + 工具子集
从源码搬进可编辑的 Markdown 文件，两阶段加载让 Agent 平时只看到摘要、
按需才加载完整 SOP 与专属工具。

- types.py      Skill 数据模型 + frontmatter/body 解析 + $ARGUMENTS 替换
- loader.py     三级搜索路径（项目 > 用户 > 内置）、同名覆盖、热加载
- state.py      已激活 Skill 的 env context 持久源（/clear 时清空）
- tools.py      LoadSkill 系统工具（意图识别按需激活）+ 目录型 Skill 专属工具
- executor.py   inline / fork 两种执行模式、allowedTools 过滤、fail-fast
- builtin/      三个内置 Skill 样板（commit / review / test）
"""

from .executor import SkillExecutor, SkillNotFoundError, SkillResult
from .host import SkillDefinitionError, SkillHost
from .loader import SkillLoader
from .state import SkillActivationState
from .types import (
    ARGUMENTS_PLACEHOLDER,
    Skill,
    parse_skill_file,
    render_prompt_body,
)

__all__ = [
    "ARGUMENTS_PLACEHOLDER",
    "Skill",
    "SkillActivationState",
    "SkillDefinitionError",
    "SkillExecutor",
    "SkillHost",
    "SkillLoader",
    "SkillNotFoundError",
    "SkillResult",
    "parse_skill_file",
    "render_prompt_body",
]
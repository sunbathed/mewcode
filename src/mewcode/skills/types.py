"""Skill 数据模型与解析。

一个 Skill 文件 = YAML frontmatter（元信息身份证）+ Markdown body（SOP 指令）。
frontmatter 字段：
  - name（必填）：唯一标识 + 命令名，小写字母/数字/连字符
  - description（必填）：一句话说明，出现在 /help 与意图识别
  - allowedTools（推荐）：工具白名单（渐进式披露 + 最小权限）
  - model（可选）：指定使用的模型
  - mode（可选）：inline（默认，共享上下文）/ fork（独立上下文）
  - context（可选，仅 fork）：full（摘要）/ recent（最近 5 条）/ none（隔离）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"

# 执行模式
MODE_INLINE = "inline"
MODE_FORK = "fork"
VALID_MODES = (MODE_INLINE, MODE_FORK)

# fork 模式上下文携带策略
CONTEXT_FULL = "full"
CONTEXT_RECENT = "recent"
CONTEXT_NONE = "none"
VALID_CONTEXTS = (CONTEXT_FULL, CONTEXT_RECENT, CONTEXT_NONE)

# fork 模式默认带最近 N 条消息
FORK_RECENT_MESSAGES = 5

# 目录型 Skill 的文件
SKILL_ENTRY_FILE = "SKILL.md"
SKILL_TOOL_FILE = "tool.json"
SKILL_REFERENCES_DIR = "references"


class SkillParseError(ValueError):
    """Skill 文件解析失败（frontmatter 缺失 / name 缺失 / 格式错误）。"""


@dataclass(frozen=True, slots=True)
class SkillToolDef:
    """目录型 Skill 的 tool.json 里声明的专属工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    script: str = ""  # references/ 下的实现脚本（相对 Skill 目录）


@dataclass(frozen=True, slots=True)
class Skill:
    """一个 Skill 的完整定义。"""

    name: str
    description: str
    prompt_body: str
    allowed_tools: tuple[str, ...] = ()
    model: str = ""
    mode: str = MODE_INLINE
    context: str = CONTEXT_FULL
    source: str = ""          # 来源路径（用于 /skill info 展示）
    source_rank: int = 0      # 0 项目级 / 1 用户级 / 2 内置级
    tools: tuple[SkillToolDef, ...] = ()  # tool.json 专属工具

    @property
    def display(self) -> str:
        """两阶段加载第一阶段的轻量描述。"""
        marker = "[skill]"
        return f"- {self.name}：{self.description}。{marker}"

    def render(self, args: str) -> str:
        """渲染完整 SOP：把 body 里的 $ARGUMENTS 替换为用户参数（空则移除）。"""
        return render_prompt_body(self.prompt_body, args)


def render_prompt_body(body: str, args: str) -> str:
    """把 prompt body 中所有的 $ARGUMENTS 替换为传入参数。

    用户没传参数时替换为空字符串，prompt 可以写「如果没有指定…」兜底。
    """
    return body.replace(ARGUMENTS_PLACEHOLDER, args or "")


def parse_skill_file(
    content: str,
    *,
    name_hint: str = "",
    source: str = "",
    source_rank: int = 2,
) -> Skill:
    """解析一个 Skill 文件：分离 frontmatter（YAML）与 body（Markdown）。

    name/description 缺失抛 SkillParseError（调用方记日志跳过，不中断加载）。
    """
    if not content.strip():
        raise SkillParseError("Skill 文件为空")

    # 分离 frontmatter：以 --- 开头的第一个 YAML 块
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillParseError("缺少 YAML frontmatter（文件必须以 --- 开头）")

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise SkillParseError("frontmatter 缺少结尾的 ---")

    frontmatter_text = "\n".join(lines[1:end_idx])
    body_text = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        meta: dict[str, Any] = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"frontmatter YAML 解析失败：{exc}") from exc
    if not isinstance(meta, dict):
        raise SkillParseError("frontmatter 必须是 YAML 映射")

    name = str(meta.get("name") or name_hint or "").strip().lower()
    if not name:
        raise SkillParseError("frontmatter 缺少必填字段 name")
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillParseError("frontmatter 缺少必填字段 description")

    mode = str(meta.get("mode") or MODE_INLINE).strip().lower()
    if mode not in VALID_MODES:
        raise SkillParseError(f"mode 只能是 {VALID_MODES}，收到：{mode}")
    context = str(meta.get("context") or CONTEXT_FULL).strip().lower()
    if context not in VALID_CONTEXTS:
        raise SkillParseError(f"context 只能是 {VALID_CONTEXTS}，收到：{context}")

    allowed = meta.get("allowedTools") or ()
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, (list, tuple)):
        raise SkillParseError("allowedTools 必须是列表")
    allowed_tools = tuple(str(item).strip() for item in allowed if str(item).strip())

    model = str(meta.get("model") or "").strip()
    return Skill(
        name=name,
        description=description,
        prompt_body=body_text,
        allowed_tools=allowed_tools,
        model=model,
        mode=mode,
        context=context,
        source=source,
        source_rank=source_rank,
    )


def parse_skill_tools(json_data: Any, base_dir: Path) -> tuple[SkillToolDef, ...]:
    """解析目录型 Skill 的 tool.json（function calling schema 兼容格式）。"""
    if isinstance(json_data, dict):
        # 兼容 {tools: [...]} 包装
        json_data = json_data.get("tools", [])
    if not isinstance(json_data, list):
        raise SkillParseError("tool.json 必须是工具定义数组")
    tools: list[SkillToolDef] = []
    for item in json_data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        function = item.get("function", item)
        if isinstance(function, dict):
            item = function
        params = item.get("parameters") or item.get("input_schema") or {"type": "object", "properties": {}}
        script = str(item.get("script") or "").strip()
        tools.append(
            SkillToolDef(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                parameters=params if isinstance(params, dict) else {"type": "object", "properties": {}},
                script=script,
            )
        )
    return tuple(tools)


__all__ = [
    "ARGUMENTS_PLACEHOLDER",
    "CONTEXT_FULL",
    "CONTEXT_NONE",
    "CONTEXT_RECENT",
    "FORK_RECENT_MESSAGES",
    "MODE_FORK",
    "MODE_INLINE",
    "SKILL_ENTRY_FILE",
    "SKILL_REFERENCES_DIR",
    "SKILL_TOOL_FILE",
    "Skill",
    "SkillParseError",
    "SkillToolDef",
    "parse_skill_file",
    "parse_skill_tools",
    "render_prompt_body",
]
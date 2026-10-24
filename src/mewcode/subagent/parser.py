"""第 13 章：Agent 定义文件的解析。

Agent 定义用 YAML frontmatter + Markdown body 的格式（和 Skill 系统
结构相同但字段不同）：frontmatter 里放结构化元数据（name / description /
tools / model 等），body 部分就是子 Agent 启动时的 system prompt——
决定这个新 Agent 是谁、能做什么、按什么风格干活，注入一次后伴随
子 Agent 整个生命周期。

校验逻辑检查必填字段与合法值（model / permission_mode / max_turns），
非法配置抛 AgentParseError，由加载器回退缓存版本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境未装 PyYAML 时使用内置兜底
    yaml = None

from .types import AgentDef, SOURCE_BUILTIN

VALID_MODELS = {"inherit", "sonnet", "opus", "haiku", ""}
VALID_PERMISSION_MODES = {"default", "acceptEdits", "dontAsk", "bypassPermissions", ""}
VALID_ISOLATION = {"", "worktree"}  # 第 14 章：文件系统隔离级别
DEFAULT_MAX_TURNS = 50


class AgentParseError(ValueError):
    """Agent 定义文件解析错误（缺 frontmatter / 缺必填字段 / 非法值）。"""


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """拆分 YAML frontmatter 与 Markdown body。"""
    stripped = raw.lstrip("\ufeff")  # 容忍 BOM
    if not stripped.startswith("---"):
        raise AgentParseError("Missing YAML frontmatter")
    end = stripped.find("---", 3)
    if end == -1:
        raise AgentParseError("Unclosed YAML frontmatter")
    yaml_block = stripped[3:end]
    body = stripped[end + 3 :].lstrip("\n")
    if yaml is None:
        raise AgentParseError("缺少 PyYAML 依赖，无法解析 frontmatter")
    meta = yaml.safe_load(yaml_block)
    if not isinstance(meta, dict):
        raise AgentParseError("Frontmatter must be a YAML mapping")
    return meta, body


def _validate_agent_meta(meta: dict[str, Any], context: str) -> None:
    """校验必填字段与合法值。"""
    if not str(meta.get("name", "")).strip():
        raise AgentParseError(f"Missing required field 'name'{context}")
    if not str(meta.get("description", "")).strip():
        raise AgentParseError(f"Missing required field 'description'{context}")
    model = str(meta.get("model", "inherit")).strip()
    if model not in VALID_MODELS:
        raise AgentParseError(f"Invalid model '{model}'{context}")
    permission_mode = str(meta.get("permissionMode", "default")).strip()
    if permission_mode not in VALID_PERMISSION_MODES:
        raise AgentParseError(
            f"Invalid permissionMode '{permission_mode}'{context} "
            f"(valid: {', '.join(v for v in VALID_PERMISSION_MODES if v)})"
        )
    isolation = str(meta.get("isolation", "")).strip()
    if isolation not in VALID_ISOLATION:
        raise AgentParseError(
            f"Invalid isolation '{isolation}'{context} "
            f"(valid: {', '.join(sorted(v for v in VALID_ISOLATION if v))})"
        )


def _str_list(value: Any) -> list[str]:
    """把 frontmatter 里的列表字段规范化为字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _optional_int(meta: dict[str, Any], key: str, default: int) -> int:
    value = meta.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_agent_file(path: str | Path, *, source: str = SOURCE_BUILTIN) -> AgentDef:
    """从定义文件解析出 AgentDef。"""
    path = Path(path).expanduser()
    context = f" (in {path})"
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    _validate_agent_meta(meta, context)

    name = str(meta.get("name", "")).strip().lower()
    return AgentDef(
        agent_type=name,
        when_to_use=str(meta.get("description", "")).strip(),
        system_prompt=body.strip(),
        tools=_str_list(meta.get("tools")),
        disallowed_tools=_str_list(meta.get("disallowedTools")),
        model=str(meta.get("model", "inherit")).strip() or "inherit",
        max_turns=_optional_int(meta, "maxTurns", DEFAULT_MAX_TURNS),
        permission_mode=str(meta.get("permissionMode", "default")).strip() or "default",
        background=bool(meta.get("background", False)),
        isolation=str(meta.get("isolation", "")).strip(),
        file_path=path,
        source=source,
    )


__all__ = [
    "AgentParseError",
    "parse_agent_file",
    "parse_frontmatter",
]
"""第 12 章：Hook 配置加载与校验。

Hook 配置与 Skill 的「找到一个就停」查找语义不同，三份文件按顺序
追加合并，声明的 Hook 全部生效：
  1. 用户级  ~/.mewcode/config.yaml          所有项目共享
  2. 项目级  {workdir}/.mewcode/config.yaml  跟着项目走，随仓库 review
  3. 本地级  {workdir}/.mewcode/config.local.yaml  本地临时改动，最高优先级

加载时集中做两件事：把条件表达式字符串解析成 ConditionGroup，
以及校验配置合法性。校验规则（fail-fast，错误定位到具体 Hook）：
  - 事件名必须在 15 个生命周期事件里
  - action 类型必须是 command/prompt/http/agent 之一
  - reject 只能用在 pre_tool_use 事件上
  - async 不能用于 pre_tool_use 事件上
  - 各 action 类型的必填字段（command→command、prompt→message、
    http→url、agent→prompt）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境未装 PyYAML 时使用内置兜底
    yaml = None

from .conditions import ConditionGroup, ConditionParseError
from .models import Action, Hook, LifecycleEvent

logger = logging.getLogger(__name__)

_VALID_EVENTS = {event.value for event in LifecycleEvent}
_VALID_ACTION_TYPES = {"command", "prompt", "http", "agent"}

# 各动作类型的必填字段映射表（校验逻辑不用写一堆 if-else）
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "command": ["command"],
    "prompt": ["message"],
    "http": ["url"],
    "agent": ["prompt"],
}


class HookConfigError(ValueError):
    """Hook 配置非法（事件名 / action 类型 / 约束 / 必填字段）。"""


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """加载 YAML 文件；文件不存在返回空 dict。"""
    if not path.is_file():
        return {}
    if yaml is None:
        raise HookConfigError(f"缺少 PyYAML 依赖，无法解析配置文件 {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HookConfigError(f"配置文件 {path} 格式错误：{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HookConfigError(f"配置文件 {path} 必须是键值对结构")
    return raw


def _entry_condition(entry: dict[str, Any]) -> ConditionGroup:
    """解析 entry 里的条件表达式。"""
    raw = entry.get("if")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HookConfigError(f"条件表达式必须是字符串，得到 {raw!r}")
    try:
        return ConditionGroup.parse(raw)
    except ConditionParseError as exc:
        raise HookConfigError(f"条件表达式非法：{exc}") from exc


def _entry_action(entry: dict[str, Any]) -> Action:
    """解析并校验 action 段。"""
    action_raw = entry.get("action")
    if not isinstance(action_raw, dict):
        raise HookConfigError("缺少 action 段（动作是 Hook 的必要组成）")
    action_type = str(action_raw.get("type", "")).strip()
    if action_type not in _VALID_ACTION_TYPES:
        raise HookConfigError(
            f"未知 action 类型：{action_type!r}（支持 {sorted(_VALID_ACTION_TYPES)}）"
        )
    action = Action(
        type=action_type,
        command=_optional_str(action_raw, "command"),
        message=_optional_str(action_raw, "message"),
        url=_optional_str(action_raw, "url"),
        method=str(action_raw.get("method", "POST")).strip().upper() or "POST",
        body=_optional_str(action_raw, "body"),
        headers=_optional_str_dict(action_raw, "headers"),
        prompt=_optional_str(action_raw, "prompt"),
        timeout=_optional_int(action_raw, "timeout", default=30),
    )
    missing = [key for key in _REQUIRED_FIELDS[action_type] if not getattr(action, key)]
    if missing:
        raise HookConfigError(f"action 类型 {action_type} 缺少必填字段：{', '.join(missing)}")
    return action


def _optional_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _optional_str_dict(entry: dict[str, Any], key: str) -> dict[str, str]:
    value = entry.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HookConfigError(f"配置项 {key} 必须是键值对结构")
    return {str(k): str(v) for k, v in value.items()}


def _optional_int(entry: dict[str, Any], key: str, default: int) -> int:
    value = entry.get(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_hook_entry(index: int, entry: Any) -> Hook:
    """解析单个 hooks 列表条目（dict）。"""
    if not isinstance(entry, dict):
        raise HookConfigError(f"hooks[{index}] 必须是键值对结构")
    label = f"hooks[{index}]"

    event = str(entry.get("event", "")).strip()
    if not event:
        raise HookConfigError(f"{label}: 缺少 event 字段（事件是 Hook 的必要组成）")
    if event not in _VALID_EVENTS:
        raise HookConfigError(
            f"{label}: 未知事件 {event!r}（合法事件：{', '.join(sorted(_VALID_EVENTS))}）"
        )

    action = _entry_action(entry)
    condition = _entry_condition(entry)

    reject = bool(entry.get("reject", False))
    if reject and event != LifecycleEvent.PRE_TOOL_USE:
        raise HookConfigError(f"{label}: 'reject' 只能用于 'pre_tool_use' 事件")

    async_exec = bool(entry.get("async", False))
    if async_exec and event == LifecycleEvent.PRE_TOOL_USE:
        raise HookConfigError(f"{label}: 'async' 不能与 'pre_tool_use' 事件一起使用")

    hook_id = str(entry.get("id", "")).strip() or f"hook-{index + 1}"
    return Hook(
        id=hook_id,
        event=event,
        action=action,
        condition=condition,
        reject=reject,
        once=bool(entry.get("once", False)),
        async_exec=async_exec,
    )


def load_hooks(
    *,
    workdir: str | None = None,
    user_path: str | Path | None = None,
    project_path: str | Path | None = None,
    local_path: str | Path | None = None,
) -> list[Hook]:
    """加载并合并三份配置文件的 hooks 段（追加合并，本地级优先级最高）。

    任一路径的解析错误都会抛出 HookConfigError（fail-fast），
    保证 Hook 在运行前就是合法的。
    """
    if user_path is None:
        user_path = Path.home() / ".mewcode" / "config.yaml"
    if project_path is None:
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        project_path = base / ".mewcode" / "config.yaml"
    if local_path is None:
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        local_path = base / ".mewcode" / "config.local.yaml"

    hooks: list[Hook] = []
    for path in (Path(user_path).expanduser(), Path(project_path).expanduser(), Path(local_path).expanduser()):
        config = _load_yaml_file(path)
        raw_hooks = config.get("hooks", [])
        if raw_hooks is None:
            continue
        if not isinstance(raw_hooks, list):
            raise HookConfigError(f"{path}: hooks 段必须是列表结构")
        for index, entry in enumerate(raw_hooks):
            hooks.append(_parse_hook_entry(index, entry))
    return hooks


class HookLoader:
    """Hook 配置文件加载器（便捷封装：加载后可直接取 hooks 列表）。"""

    def __init__(self, workdir: str | None = None) -> None:
        self.workdir = workdir

    def load(self) -> list[Hook]:
        return load_hooks(workdir=self.workdir)


__all__ = [
    "Action",
    "Hook",
    "HookConfigError",
    "HookLoader",
    "load_hooks",
]
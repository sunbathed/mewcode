"""第 12 章：Hook 系统——在 Agent 生命周期事件上挂载自动化动作。

一个 Hook 由「事件 + 条件 + 动作」三要素组成：事件决定什么时候触发，
条件决定什么情况下执行（可省略），动作决定执行什么。支持四种执行器
（command / prompt / http / agent），pre_tool_use 事件配合 reject 可以
拦截工具调用，once / async / timeout 提供执行控制。

模块划分（对应文档「Python 源码解析」六文件结构）：
  - models.py      核心数据结构（Hook / Action / HookContext / 事件常量）
  - conditions.py  条件表达式解析与求值（==/!=/=~/~=，&&/|| 组合）
  - executors.py   四种动作执行器 + 分发表路由
  - loader.py      三文件 YAML 配置加载与校验（追加合并）
  - engine.py      HookEngine：匹配、执行、拦截、prompt 收集
"""

from .conditions import Condition, ConditionGroup, ConditionParseError
from .engine import HookEngine, _build_hook_context
from .executors import ActionResult, execute_action
from .loader import HookConfigError, HookLoader, load_hooks
from .models import (
    Action,
    Hook,
    HookContext,
    LifecycleEvent,
    ToolRejectedError,
)

__all__ = [
    "Action",
    "ActionResult",
    "Condition",
    "ConditionGroup",
    "ConditionParseError",
    "Hook",
    "HookConfigError",
    "HookContext",
    "HookEngine",
    "HookLoader",
    "LifecycleEvent",
    "ToolRejectedError",
    "_build_hook_context",
    "execute_action",
    "load_hooks",
]
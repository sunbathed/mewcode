"""第六章：五层纵深权限防御。

安全层贯穿所有操作，但不干预具体业务逻辑。五层防线：
  1. 危险命令黑名单（仅 Bash，正则硬拦截）
  2. 路径沙箱（文件工具，解析符号链接后做前缀检查）
  3. 权限规则（ToolName(pattern) + glob 匹配，三层优先级）
  4. 权限模式（default / acceptEdits / plan / bypassPermissions）
  5. HITL 人在回路（Agent 发事件阻塞等待，UI 渲染确认对话框）

决策链只返回三种结果：ALLOW / DENY / ASK（ASK 由调用方触发 HITL）。
前两层是硬防线，任何配置都无法绕过；后三层是软防线，可灵活配置。
"""

from .checker import (
    PermissionChecker,
    PermissionDecision,
    PermissionDecisionResult,
    extract_content,
    extract_target_path,
)
from .dangerous import DEFAULT_DANGEROUS_PATTERNS, DangerousCommandDetector
from .hitl import PermissionResponse, build_allow_rule_text, build_permission_description
from .modes import MODE_ALIASES, PermissionMode, decide_mode, parse_mode
from .rules import PermissionRule, RuleEngine, load_rules, parse_rules
from .sandbox import PathSandbox

__all__ = [
    "DEFAULT_DANGEROUS_PATTERNS",
    "DangerousCommandDetector",
    "PathSandbox",
    "PermissionRule",
    "RuleEngine",
    "load_rules",
    "parse_rules",
    "PermissionMode",
    "MODE_ALIASES",
    "parse_mode",
    "decide_mode",
    "PermissionChecker",
    "PermissionDecision",
    "PermissionDecisionResult",
    "extract_content",
    "extract_target_path",
    "PermissionResponse",
    "build_allow_rule_text",
    "build_permission_description",
]
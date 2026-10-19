"""权限检查器：把五层防线串成决策链。

关键原则：上一层能决策就直接返回，不能决策才往下走。
  - 危险命令拦截（仅 Bash）：命中直接 DENY，不可覆盖
  - 路径沙箱（仅文件工具）：越界直接 DENY，不可覆盖
  - 权限规则：匹配到 allow / deny 直接返回
  - 权限模式：ALLOW / DENY 直接返回；只有 ASK 时需触发第 5 层 HITL

返回值是一个决策加一个原因字符串。决策只有三种可能：
ALLOW / DENY / ASK。如果是 ASK，调用方（Agent Loop）触发 HITL 确认流程。

「提取内容」规则（对应理论篇表格）：
  Bash                        -> command 参数
  ReadFile/WriteFile/EditFile -> path 参数
  Glob/Grep                   -> pattern 参数
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import Any

from .dangerous import DangerousCommandDetector
from .modes import PermissionMode, decide_mode, infer_safety, parse_mode
from .rules import EFFECT_ALLOW, EFFECT_DENY, RuleEngine, UNKNOWN
from .sandbox import PathSandbox


class PermissionDecision(enum.StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PermissionDecisionResult:
    """一次权限检查的决策结果：决策 + 原因说明。"""

    decision: PermissionDecision
    reason: str = ""


# 文件类工具：受路径沙箱保护
FILE_TOOLS = {"ReadFile", "WriteFile", "EditFile", "ReplaceInFile"}
# 提取哪个参数作为规则匹配内容
CONTENT_FIELD = {
    "Bash": "command",
    "ReadFile": "path",
    "WriteFile": "path",
    "EditFile": "path",
    "ReplaceInFile": "path",
    "Glob": "pattern",
    "Grep": "pattern",
}


def extract_content(tool_name: str, tool_input: dict[str, Any]) -> str:
    """从工具输入参数中提取「内容」用于规则匹配。"""
    field = CONTENT_FIELD.get(tool_name)
    if field:
        value = tool_input.get(field)
        if isinstance(value, str):
            return value
    return json.dumps(tool_input, ensure_ascii=False)


def extract_target_path(tool_name: str, tool_input: dict[str, Any]) -> str:
    """文件类工具提取 path 参数用于沙箱校验。"""
    path = tool_input.get("path")
    return str(path).strip() if isinstance(path, str) else ""


class PermissionChecker:
    """五层权限决策链：黑名单 -> 沙箱 -> 规则 -> 模式 -> (HITL 由调用方触发)。"""

    def __init__(
        self,
        *,
        workdir: str | None = None,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        detector: DangerousCommandDetector | None = None,
        sandbox: PathSandbox | None = None,
        rules: RuleEngine | None = None,
        safety_map: dict[str, str] | None = None,
    ) -> None:
        self.mode = parse_mode(mode)
        self.detector = detector or DangerousCommandDetector()
        self.sandbox = sandbox or PathSandbox(workdir)
        self.rules = rules or RuleEngine(workdir)
        self.safety_map = safety_map

    def set_mode(self, mode: PermissionMode | str) -> None:
        """运行时切换权限模式（/permissions 命令）。"""
        self.mode = parse_mode(mode)

    def _safety(self, tool_name: str) -> str:
        return infer_safety(tool_name, self.safety_map)

    # ------------------------------------------------------------------
    # 五层决策链
    # ------------------------------------------------------------------

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecisionResult:
        """对一次工具调用执行五层权限决策。"""
        content = extract_content(tool_name, tool_input)

        # 第 1 层：危险命令拦截（仅 Bash）
        if tool_name == "Bash":
            reason = self.detector.match(content)
            if reason is not None:
                return PermissionDecisionResult(
                    PermissionDecision.DENY,
                    f'操作被拒绝：检测到危险命令 "{content}"。此操作可能造成不可逆的系统损坏，已被安全策略硬拦截（{reason}）。',
                )

        # 第 2 层：路径沙箱（仅文件工具）
        if tool_name in FILE_TOOLS:
            path = extract_target_path(tool_name, tool_input)
            allowed, detail = self.sandbox.validate(path)
            if not allowed:
                return PermissionDecisionResult(
                    PermissionDecision.DENY,
                    f"操作被拒绝：路径 {path} 超出沙箱范围。文件操作被限制在项目目录内。",
                )

        # 第 3 层：权限规则
        rule_result = self.rules.evaluate(tool_name, content)
        if rule_result == EFFECT_DENY:
            return PermissionDecisionResult(
                PermissionDecision.DENY,
                f'操作被拒绝：权限规则禁止 {tool_name}({content[:80]})。',
            )
        if rule_result == EFFECT_ALLOW:
            return PermissionDecisionResult(
                PermissionDecision.ALLOW,
                f'权限规则允许 {tool_name}({content[:80]})。',
            )

        # 第 4 层：权限模式（可能返回 ASK，触发第 5 层 HITL）
        decision = decide_mode(self.mode, self._safety(tool_name))
        if decision == "allow":
            return PermissionDecisionResult(
                PermissionDecision.ALLOW,
                f"权限模式 {self.mode.value} 自动放行只读/编辑操作。",
            )
        return PermissionDecisionResult(
            PermissionDecision.ASK,
            f"{tool_name} 需要用户确认（权限模式：{self.mode.value}）。",
        )

    # ------------------------------------------------------------------
    # 「始终允许」学习循环
    # ------------------------------------------------------------------

    def remember_always(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """用户选择「始终允许」后，把一条 allow 规则追加到本地配置文件。

        返回生成的规则文本。
        """
        from .hitl import build_allow_rule_text

        rule_text = build_allow_rule_text(tool_name, tool_input)
        self.rules.append_local_rule(rule_text, EFFECT_ALLOW)
        return rule_text


__all__ = [
    "PermissionDecision",
    "PermissionDecisionResult",
    "PermissionChecker",
    "extract_content",
    "extract_target_path",
    "FILE_TOOLS",
    "CONTENT_FIELD",
]
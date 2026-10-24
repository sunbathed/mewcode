"""第 12 章：Hook 条件表达式的解析与求值。

条件语法复用权限规则的匹配语法，支持四种比较操作符：
  ==   精确匹配
  !=   反向匹配
  =~   正则匹配（支持 /pattern/ 写法）
  ~=   glob 匹配（fnmatch）

组合逻辑用 &&（全部满足）或 ||（任一满足），但两者不允许混用——
一旦混用就需要运算符优先级与括号，需要一个完整的表达式引擎，
对 Hook 条件来说没必要，拆成多个 Hook 即可。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与 models.py 循环导入
    from .models import HookContext

_OPERATORS = ("==", "!=", "=~", "~=")


class ConditionParseError(ValueError):
    """条件表达式格式错误（无法拆分 / 逻辑混用）。"""


def _split_top_level(expression: str) -> tuple[str, list[str]]:
    """按 && 或 || 拆分（不含括号，直接全量匹配）；返回 (logic, 子条件列表)。

    混用会抛 ConditionParseError——这是有意限制：不支持优先级，
    复杂逻辑请拆成多个 Hook。
    """
    if "&&" in expression and "||" in expression:
        raise ConditionParseError(
            "Cannot mix '&&' and '||' in a single condition expression. "
            "Split into separate hooks instead."
        )
    if "&&" in expression:
        return "and", [part.strip() for part in expression.split("&&")]
    if "||" in expression:
        return "or", [part.strip() for part in expression.split("||")]
    return "and", [expression.strip()]


@dataclass(slots=True)
class Condition:
    """单个条件：field operator value 三部分。"""

    field: str
    operator: str
    value: str

    def evaluate(self, ctx: HookContext) -> bool:
        """按操作符对上下文里的字段值做匹配。"""
        field_value = ctx.get_field(self.field)
        if self.operator == "==":
            return field_value == self.value
        if self.operator == "!=":
            return field_value != self.value
        if self.operator == "=~":
            pattern = self.value
            if pattern.startswith("/") and pattern.endswith("/"):
                pattern = pattern[1:-1]
            try:
                return bool(re.search(pattern, field_value))
            except re.error:
                # 防御性编程：正则语法错误视为不匹配，不抛异常
                return False
        if self.operator == "~=":
            return fnmatch.fnmatch(field_value, self.value)
        return False

    @classmethod
    def parse(cls, text: str) -> Condition:
        """把 "field operator value" 三部分拆成 Condition。

        按空格拆分时取第一个空格为分隔：field 与 operator 之间无空格，
        value 里允许含空格（如 glob 或正则表达式中）。
        """
        stripped = text.strip()
        if not stripped:
            raise ConditionParseError("Empty condition expression.")
        first_space = stripped.find(" ")
        if first_space <= 0:
            raise ConditionParseError(f"Invalid condition expression: {text!r}")
        field = stripped[:first_space]
        rest = stripped[first_space + 1 :].strip()
        for operator in _OPERATORS:
            if rest.startswith(operator):
                value = rest[len(operator) :].strip()
                # 引号包裹的值去掉引号（'...' / "..."）
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                return cls(field, operator, value)
        raise ConditionParseError(
            f"Unknown operator in condition: {text!r} (supported: {' / '.join(_OPERATORS)})"
        )


@dataclass(slots=True)
class ConditionGroup:
    """一组条件：and 模式要求全部通过，or 模式任一通过即可。"""

    conditions: list[Condition] = field(default_factory=list)
    logic: str = "and"

    def evaluate(self, ctx: HookContext) -> bool:
        if not self.conditions:
            return True
        if self.logic == "and":
            return all(c.evaluate(ctx) for c in self.conditions)
        return any(c.evaluate(ctx) for c in self.conditions)

    @classmethod
    def parse(cls, expression: str) -> ConditionGroup:
        """解析整个条件表达式字符串为 ConditionGroup。"""
        logic, parts = _split_top_level(expression)
        conditions = [Condition.parse(part) for part in parts if part]
        return cls(conditions=conditions, logic=logic)


__all__ = [
    "Condition",
    "ConditionGroup",
    "ConditionParseError",
    "_OPERATORS",
]
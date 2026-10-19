"""第三道防线：权限规则引擎。

规则语法：ToolName(content_pattern)，其中 ToolName 是工具名称，
content_pattern 是对工具输入内容的匹配模式，支持 glob 通配符。

规则三层优先级（越具体、越靠近当前项目，优先级越高）：
  1. 本地级（最高）：{project}/.mewcode/permissions.local.yaml —— 个人覆盖，不入版本控制
  2. 项目级：        {project}/.mewcode/permissions.yaml —— 团队共享规则
  3. 用户级（最低）：~/.mewcode/permissions.yaml —— 全局偏好

安全原则：deny 跨层合并，不可翻转。三层 deny 规则收集到一起，
任何一层说了 deny，其他层的 allow 都盖不掉。同一层级内，后定义的规则优先级更高。

规则文件格式（YAML 子集）：
    # 注释
    - rule: Bash(git *)
      effect: allow
    # 单行简写也支持
    - ReadFile(*.env*): deny
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
UNKNOWN = "unknown"

# 规则文件目录名
RULES_DIR_NAME = ".mewcode"
LOCAL_RULES_FILE = "permissions.local.yaml"
PROJECT_RULES_FILE = "permissions.yaml"
USER_RULES_FILE = "permissions.yaml"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """一条权限规则：ToolName(pattern) + 生效效果。"""

    rule: str            # 完整规则文本，如 "Bash(git *)"
    effect: str          # allow | deny
    source: str = ""     # 来源文件（用于提示信息）
    line: int = 0        # 来源行号

    @property
    def tool_name(self) -> str:
        """提取工具名：第一个 ( 之前的部分。"""
        first = self.rule.find("(")
        return self.rule[:first].strip() if first >= 0 else self.rule.strip()

    @property
    def pattern(self) -> str:
        """提取内容模式：第一个 ( 与最后一个 ) 之间的部分。"""
        first = self.rule.find("(")
        last = self.rule.rfind(")")
        if first < 0 or last <= first:
            return ""
        return self.rule[first + 1 : last]

    def matches(self, tool_name: str, content: str) -> bool:
        """工具名精确匹配，内容按 glob 模式匹配（大小写敏感、跨平台一致）。"""
        if tool_name != self.tool_name:
            return False
        return fnmatch.fnmatchcase(content, self.pattern)


def parse_rule_text(text: str) -> tuple[str, str] | None:
    """解析 "ToolName(pattern)" 文本，返回 (工具名, 模式)；格式非法返回 None。"""
    first = text.find("(")
    last = text.rfind(")")
    if first <= 0 or last <= first:
        return None
    tool = text[:first].strip()
    pattern = text[first + 1 : last]
    return tool, pattern


def parse_rules(content: str, source: str = "") -> list[PermissionRule]:
    """解析规则文件文本（YAML 子集），返回规则列表。

    支持两种写法：
      - rule: Bash(git *)
        effect: allow
      - Bash(git *): allow
    """
    rules: list[PermissionRule] = []
    pending_rule: str | None = None
    pending_effect: str | None = None

    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            # 提交上一条完整规则
            if pending_rule is not None:
                rules.append(PermissionRule(pending_rule, pending_effect or EFFECT_ALLOW, source, 0))
                pending_rule, pending_effect = None, None
            body = line[2:].strip()
            # 单行简写：ToolName(pattern): effect
            if "):" in body and "rule:" not in body:
                rule_text, _, eff = body.rpartition("):")
                pending_rule = rule_text.strip() + ")"
                pending_effect = eff.strip().lower() or EFFECT_ALLOW
            elif body.startswith("rule:"):
                pending_rule = body[len("rule:"):].strip()
                pending_effect = None
            else:
                pending_rule = body
                pending_effect = None
        elif line.startswith("effect:"):
            pending_effect = line[len("effect:"):].strip().lower()
        elif line.startswith("rule:"):
            if pending_rule is not None:
                rules.append(PermissionRule(pending_rule, pending_effect or EFFECT_ALLOW, source, 0))
            pending_rule = line[len("rule:"):].strip()
            pending_effect = None
    if pending_rule is not None:
        rules.append(PermissionRule(pending_rule, pending_effect or EFFECT_ALLOW, source, 0))
    # 过滤格式非法的规则
    return [r for r in rules if parse_rule_text(r.rule) is not None]


def load_rules(path: Path | str) -> list[PermissionRule]:
    """从文件加载规则；文件不存在返回空列表，不报错。"""
    target = Path(path).expanduser()
    if not target.is_file():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_rules(text, source=str(target))


@dataclass(slots=True)
class RuleSet:
    """三层规则集合：本地（最高）> 项目 > 用户（最低）。"""

    local: list[PermissionRule] = field(default_factory=list)
    project: list[PermissionRule] = field(default_factory=list)
    user: list[PermissionRule] = field(default_factory=list)


class RuleEngine:
    """加载三层优先级规则，提供 glob 匹配求值。"""

    def __init__(
        self,
        workdir: str | None = None,
        *,
        user_path: str | os.PathLike[str] | None = None,
        project_path: str | os.PathLike[str] | None = None,
        local_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.workdir = workdir
        self.user_path = Path(user_path).expanduser() if user_path else self._default_user_path()
        project, local = self._project_paths()
        self.project_path = Path(project_path).expanduser() if project_path else project
        self.local_path = Path(local_path).expanduser() if local_path else local
        self.reload()

    def _default_user_path(self) -> Path:
        return Path.home() / ".mewcode" / USER_RULES_FILE

    def _project_paths(self) -> tuple[Path, Path]:
        base = Path(self.workdir).expanduser() if self.workdir else Path.cwd()
        return base / RULES_DIR_NAME / PROJECT_RULES_FILE, base / RULES_DIR_NAME / LOCAL_RULES_FILE

    def reload(self) -> None:
        """重新从磁盘加载三层规则。"""
        self.rules = RuleSet(
            local=load_rules(self.local_path),
            project=load_rules(self.project_path),
            user=load_rules(self.user_path),
        )

    def _layers(self) -> tuple[list[PermissionRule], list[PermissionRule], list[PermissionRule]]:
        """从高优先级到低优先级返回 (local, project, user)。"""
        return self.rules.local, self.rules.project, self.rules.user

    def evaluate(self, tool_name: str, content: str) -> str:
        """求值规则：返回 allow / deny / unknown。

        伪代码（理论篇）：
          1. 先收集所有层的 deny 规则（deny 跨层合并不可翻转）
          2. 再从高优先级到低优先级找 allow，同层内后定义优先（从后往前）
        """
        # 第 1 步：deny 跨层合并，不可翻转
        for rules in self._layers():
            for rule in rules:
                if rule.matches(tool_name, content) and rule.effect == EFFECT_DENY:
                    return EFFECT_DENY
        # 第 2 步：从高优先级到低优先级找 allow；同层内后定义优先
        for rules in self._layers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return UNKNOWN

    def append_local_rule(self, rule_text: str, effect: str = EFFECT_ALLOW, note: str | None = None) -> None:
        """把一条规则追加到本地配置文件并立即 reload。

        用于「始终允许」学习循环：用户选择始终允许后自动生成 allow 规则。
        """
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = ""
        try:
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001 - 时间获取失败不影响规则写入
            timestamp = ""
        lines: list[str] = []
        if note is None:
            note = f"自动生成于 {timestamp}"
        lines.append(f"# {note}".rstrip())
        lines.append(f"- rule: {rule_text}")
        lines.append(f"  effect: {effect}")
        lines.append("")
        with open(self.local_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.reload()


__all__ = [
    "EFFECT_ALLOW",
    "EFFECT_DENY",
    "UNKNOWN",
    "PermissionRule",
    "RuleSet",
    "RuleEngine",
    "parse_rules",
    "parse_rule_text",
    "load_rules",
    "RULES_DIR_NAME",
    "LOCAL_RULES_FILE",
    "PROJECT_RULES_FILE",
    "USER_RULES_FILE",
]
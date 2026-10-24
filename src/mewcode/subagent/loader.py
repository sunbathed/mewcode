"""第 13 章：Agent 定义的多来源加载器。

按优先级从高到低扫描三个位置（对应源码解析文档的三级优先级）：
  1. 项目级：{projectDir}/.mewcode/agents/   ← 优先级最高
  2. 用户级：~/.mewcode/agents/
  3. 内置级：包内 builtin/ 目录

同名覆盖策略是「先到先得」：高优先级目录先扫描，低优先级同名定义
被 seen 字典跳过，项目级定义自然盖住用户级与内置级。

热重载：每次 get() 都重新解析定义文件拿到最新内容；解析失败时
回退缓存版本，保证一处坏文件不会把子 Agent 创建搞挂。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .parser import AgentParseError, parse_agent_file
from .types import AgentDef, SOURCE_BUILTIN, SOURCE_PROJECT, SOURCE_USER

logger = logging.getLogger(__name__)

PROJECT_RANK = 0
USER_RANK = 1
BUILTIN_RANK = 2


def _builtin_dir() -> Path:
    """内置 Agent 定义目录：包内 builtin/（随代码分发）。"""
    return Path(__file__).resolve().parent / "builtin"


class AgentLoader:
    """Agent 定义加载器：三级搜索路径 + 同名覆盖 + 热重载。"""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root).expanduser() if project_root else Path.cwd()
        self._cache: dict[str, AgentDef] = {}

    # ------------------------------------------------------------------
    # 目录扫描
    # ------------------------------------------------------------------

    def _collect_roots(self) -> list[tuple[Path, str]]:
        """三级搜索根：项目级 > 用户级 > 内置级。"""
        return [
            (self.project_root / ".mewcode" / "agents", SOURCE_PROJECT),
            (Path.home() / ".mewcode" / "agents", SOURCE_USER),
            (_builtin_dir(), SOURCE_BUILTIN),
        ]

    def _all_agent_files(self) -> list[tuple[Path, str]]:
        """按优先级扫描所有定义文件：(file_path, source)。"""
        seen: set[str] = set()
        result: list[tuple[Path, str]] = []
        for root, source in self._collect_roots():
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_file() or entry.suffix.lower() != ".md":
                    continue
                name_hint = entry.stem.strip().lower()
                if name_hint in seen:
                    continue  # 高优先级已收录，低优先级版本被覆盖
                seen.add(name_hint)
                result.append((entry, source))
        return result

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load_all(self) -> dict[str, AgentDef]:
        """加载全部 Agent 定义（按优先级，同名先到先得）。"""
        definitions: dict[str, AgentDef] = {}
        for path, source in self._all_agent_files():
            try:
                definition = parse_agent_file(path, source=source)
            except (AgentParseError, OSError) as exc:
                logger.warning("Agent 定义解析失败，跳过：%s（%s）", path, exc)
                continue
            if definition.agent_type not in definitions:
                definitions[definition.agent_type] = definition
        return definitions

    def list_agents(self) -> list[AgentDef]:
        """返回全部可用 Agent 定义（含热重载）。"""
        result = []
        for agent_type in sorted(self.load_all()):
            definition = self.get(agent_type)
            if definition is not None:
                result.append(definition)
        return result

    def available_types_text(self) -> str:
        """「可用类型」提示文本（未知 agent_type 时报给模型自我纠正）。"""
        types = [d.agent_type for d in self.list_agents()]
        return ", ".join(types) if types else "(none)"

    # ------------------------------------------------------------------
    # 热重载
    # ------------------------------------------------------------------

    def get(self, agent_type: str) -> AgentDef | None:
        """按类型取定义：每次重新读文件（热加载），失败回退缓存。

        找不到时返回 None（AgentTool 据此报「未知类型」并列出可用类型）。
        """
        key = agent_type.strip().lower()
        for path, source in self._all_agent_files():
            if path.stem.lower() != key:
                continue
            try:
                reloaded = parse_agent_file(path, source=source)
                self._cache[key] = reloaded
                return reloaded
            except (AgentParseError, OSError) as exc:
                cached = self._cache.get(key)
                if cached is not None:
                    logger.warning("Agent 定义 %s 重载失败，回退缓存：%s", key, exc)
                    return cached
                logger.warning("Agent 定义 %s 解析失败：%s", key, exc)
                return None
        return self._cache.get(key)

    def has(self, agent_type: str) -> bool:
        return self.get(agent_type) is not None

    def reload(self) -> list[str]:
        """强制清空缓存并重扫；返回全部定义类型名。"""
        self._cache.clear()
        return [d.agent_type for d in self.list_agents()]


__all__ = [
    "BUILTIN_RANK",
    "PROJECT_RANK",
    "USER_RANK",
    "AgentLoader",
    "_builtin_dir",
]
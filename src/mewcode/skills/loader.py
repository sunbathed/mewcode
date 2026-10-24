"""Skill 加载器：三级搜索路径 + 同名覆盖 + 热加载。

搜索路径（优先级从高到低）：
  1. 项目级：{projectDir}/.mewcode/skills/
  2. 用户级：~/.mewcode/skills/
  3. 内置级：包内 builtin/ 目录

同名覆盖：按优先级扫描，seen 字典去重，高优先级目录里的同名 Skill
自然盖住低优先级版本。单个文件解析失败记日志跳过，不阻断整体加载。

热加载：每次 get() 重新读文件拿到最新内容；解析失败时回退缓存版本，
保证一处坏文件不会把整个命令搞挂。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from .types import (
    SKILL_ENTRY_FILE,
    SKILL_REFERENCES_DIR,
    SKILL_TOOL_FILE,
    Skill,
    SkillParseError,
    SkillToolDef,
    parse_skill_file,
    parse_skill_tools,
)

logger = logging.getLogger(__name__)

# 三级来源的展示名与优先级
PROJECT_RANK = 0
USER_RANK = 1
BUILTIN_RANK = 2


class SkillNotFoundError(KeyError):
    """按名字找不到 Skill。"""


def _builtin_dir() -> Path:
    """内置 Skill 目录：包内 builtin/（随代码分发，等价于编译进二进制）。"""
    return Path(__file__).resolve().parent / "builtin"


def _discover_skill_dirs(directory: Path) -> list[Path]:
    """扫描一个技能目录，返回每个 Skill 的目录（单文件 Skill 也包一层）。

    目录型 Skill：skill/ 里含 SKILL.md
    单文件 Skill：skill.md 直接是 Markdown
    返回 [(skill_dir, rank)] 的排序列表。
    """
    found: list[tuple[Path, int]] = []
    if not directory.exists():
        return found
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            if (entry / SKILL_ENTRY_FILE).is_file():
                found.append((entry, 0))
        elif entry.is_file() and entry.suffix.lower() == ".md":
            found.append((entry, 1))
    return [(path, rank) for path, rank in found]


class SkillLoader:
    """Skill 的三级加载器：catalog（轻量摘要）+ get（完整，热加载）。"""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root).expanduser() if project_root else Path.cwd()
        self.dirs: list[tuple[Path, int, str]] = []
        self._cache: dict[str, Skill] = {}

    # ------------------------------------------------------------------
    # 目录扫描
    # ------------------------------------------------------------------

    def _collect_roots(self) -> list[tuple[Path, int, str]]:
        """三级搜索根：项目级 > 用户级 > 内置级。"""
        roots: list[tuple[Path, int, str]] = [
            (self.project_root / ".mewcode" / "skills", PROJECT_RANK, str(self.project_root / ".mewcode" / "skills")),
            (Path.home() / ".mewcode" / "skills", USER_RANK, "~/.mewcode/skills"),
            (_builtin_dir(), BUILTIN_RANK, "builtin"),
        ]
        return roots

    def _all_skill_paths(self) -> list[tuple[Path, str, int]]:
        """按优先级扫描所有 Skill 入口：(entry_path, source_label, rank)。"""
        seen: set[str] = set()
        result: list[tuple[Path, str, int]] = []
        for root, rank, label in self._collect_roots():
            for entry, kind in _discover_skill_dirs(root):
                if kind == 0:  # 目录型
                    path = entry / SKILL_ENTRY_FILE
                    name_hint = entry.name.strip().lower()
                else:  # 单文件
                    path = entry
                    name_hint = entry.stem.strip().lower()
                if name_hint in seen:
                    continue  # 高优先级已收录，低优先级版本被覆盖
                seen.add(name_hint)
                result.append((path, f"{label}/{entry.name}", rank))
        return result

    # ------------------------------------------------------------------
    # 轻量 Catalog（两阶段加载第一阶段）
    # ------------------------------------------------------------------

    def load_catalog(self) -> list[Skill]:
        """加载所有 Skill 的 frontmatter（name/description/...），不读完整 body。

        返回按优先级排序的 Skill 列表（source_rank 越小越靠前）。
        """
        catalog: list[Skill] = []
        for path, source, rank in self._all_skill_paths():
            try:
                skill = self._parse_skill(path, source_label=source, rank=rank)
            except (SkillParseError, OSError) as exc:
                logger.warning("Skill 解析失败，跳过：%s（%s）", path, exc)
                continue
            catalog.append(skill)
        return catalog

    def catalog_text(self) -> str:
        """两阶段加载阶段一：全部 Skill 的轻量摘要文本（注入 system prompt）。"""
        skills = self.load_catalog()
        if not skills:
            return ""
        lines = ["你可以使用以下 Skill："]
        for skill in sorted(skills, key=lambda s: s.source_rank):
            lines.append(f"- {skill.name}：{skill.description}。")
        lines.append("如果用户的请求匹配某个 Skill，请调用 LoadSkill 工具加载它。")
        return "\n".join(lines)

    def _parse_skill(self, path: Path, *, source_label: str, rank: int) -> Skill:
        """解析单个 Skill 入口（目录型会附带读取 tool.json）。"""
        content = path.read_text(encoding="utf-8")
        skill = parse_skill_file(
            content,
            name_hint=path.parent.name if path.name == SKILL_ENTRY_FILE else path.stem,
            source=source_label,
            source_rank=rank,
        )
        # 目录型 Skill：读取 tool.json 专属工具
        base = path.parent if path.name == SKILL_ENTRY_FILE else None
        if base is not None:
            tool_json = base / SKILL_TOOL_FILE
            if tool_json.is_file():
                try:
                    data = json.loads(tool_json.read_text(encoding="utf-8"))
                    tools = parse_skill_tools(data, base)
                except (json.JSONDecodeError, SkillParseError, OSError) as exc:
                    logger.warning("Skill %s 的 tool.json 解析失败：%s", skill.name, exc)
                    tools = ()
                skill = Skill(
                    name=skill.name,
                    description=skill.description,
                    prompt_body=skill.prompt_body,
                    allowed_tools=skill.allowed_tools,
                    model=skill.model,
                    mode=skill.mode,
                    context=skill.context,
                    source=skill.source,
                    source_rank=skill.source_rank,
                    tools=tools,
                )
        return skill

    # ------------------------------------------------------------------
    # 完整加载（热加载）
    # ------------------------------------------------------------------

    def get(self, name: str) -> Skill:
        """按名字取 Skill：每次重新读文件（热加载），失败回退缓存版本。

        解析失败时若缓存里有旧版本，回退缓存而不是抛错，保证
        一处坏文件不会把整个命令搞挂。
        """
        name = name.strip().lower()
        for path, source, rank in self._all_skill_paths():
            hint = path.parent.name if path.name == SKILL_ENTRY_FILE else path.stem
            if hint != name:
                continue
            try:
                skill = self._parse_skill(path, source_label=source, rank=rank)
            except (SkillParseError, OSError) as exc:
                cached = self._cache.get(name)
                if cached is not None:
                    logger.warning("Skill %s 重载失败，回退缓存：%s", name, exc)
                    return cached
                raise SkillNotFoundError(
                    f"Skill '{name}' 解析失败：{exc}。可编辑源文件修复后重试 /skill reload。"
                ) from exc
            self._cache[name] = skill
            return skill
        # 不在文件里：尝试缓存（例如构建中临时移除）
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        raise SkillNotFoundError(f"未知 Skill：{name}。输入 /skill list 查看已加载的 Skill。")

    def has(self, name: str) -> bool:
        try:
            self.get(name)
            return True
        except SkillNotFoundError:
            return False

    def reload(self) -> list[str]:
        """强制重新扫描并加载所有 Skill；返回新加载的名字列表。"""
        self._cache.clear()
        return [skill.name for skill in self.load_catalog()]

    def skill_dir_for(self, name: str) -> Path | None:
        """返回 Skill 的目录（目录型 Skill 的 references/ 与 tool.json 所在处）。"""
        for path, _, _ in self._all_skill_paths():
            hint = path.parent.name if path.name == SKILL_ENTRY_FILE else path.stem
            if hint == name:
                return path.parent if path.name == SKILL_ENTRY_FILE else path
        return None

    # ------------------------------------------------------------------
    # 工具引用解析（目录型 Skill 专属工具实现）
    # ------------------------------------------------------------------

    def resolve_tool_script(self, skill: Skill, tool: SkillToolDef) -> Path | None:
        """把 tool.json 里的 script 字段解析为 references/ 下的实现文件。"""
        base = self.skill_dir_for(skill.name)
        if base is None or not tool.script:
            return None
        script = base / SKILL_REFERENCES_DIR / tool.script
        return script if script.is_file() else None


# 便于脚本与测试直接获取内置目录
__all__ = [
    "BUILTIN_RANK",
    "PROJECT_RANK",
    "SkillLoader",
    "SkillNotFoundError",
    "USER_RANK",
    "_builtin_dir",
]
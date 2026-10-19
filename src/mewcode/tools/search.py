from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolError

GLOB_LIMIT = 100
GREP_LIMIT = 200
# 搜索时默认忽略的目录
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".idea", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def _resolve_base(raw: str, workdir: str | None = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        path = base / path
    return path


def _ignored(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part.startswith(".") or part in IGNORED_DIRS for part in parts)


class Glob(BaseTool):
    name = "Glob"
    description = (
        "按通配模式列出匹配的文件路径，默认忽略隐藏目录，限制结果数量。"
        "查找文件优先用此工具，而不是 Bash find 或 ls。"
    )
    safety = "read"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "通配模式，如 **/*.py 或 src/**"},
            "path": {"type": "string", "description": "可选的搜索起点目录，默认项目工作目录"},
        },
        "required": ["pattern"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        accept: list[Path] = []
        pattern = str(args.get("pattern", "")).strip()
        base = _resolve_base(str(args.get("path", "")).strip() or ".", self.workdir)
        if not pattern:
            raise ToolError("缺少 pattern 参数")
        if not base.is_dir():
            raise ToolError(f"搜索起点不是目录：{base}")

        for candidate in base.glob(pattern):
            rel = candidate.relative_to(base)
            if _ignored(str(rel)):
                continue
            if candidate.is_file() or candidate.is_dir():
                accept.append(candidate)
                if len(accept) >= GLOB_LIMIT:
                    break
        if not accept:
            return "未匹配到任何文件（忽略隐藏目录）。"
        lines = [str(p.relative_to(base)).replace("\\", "/") for p in accept]
        text = "\n".join(lines)
        if len(accept) >= GLOB_LIMIT:
            text += f"\n...（结果过多，仅显示前 {GLOB_LIMIT} 条）"
        return text


class Grep(BaseTool):
    name = "Grep"
    description = (
        "按正则表达式搜索文件内容，返回「文件:行号:内容」形式的匹配行，默认忽略隐藏目录，限制结果数量。"
        "搜索文件内容优先用此工具，而不是 Bash grep 或 rg。"
    )
    safety = "read"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "可选的搜索起点目录，默认项目工作目录"},
            "glob": {"type": "string", "description": "可选的 glob 过滤，如 *.py，只搜索匹配的文件"},
        },
        "required": ["pattern"],
    }

    def __init__(self, *, workdir: str | None = None, case_sensitive: bool = False) -> None:
        self.workdir = workdir
        self.case_sensitive = case_sensitive

    async def _run(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern", "")).strip()
        base = _resolve_base(str(args.get("path", "")).strip() or ".", self.workdir)
        glob_filter = str(args.get("glob", "")).strip()
        if not pattern:
            raise ToolError("缺少 pattern 参数")

        try:
            regex = re.compile(pattern) if self.case_sensitive else re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"正则表达式无效：{exc}") from exc
        if not base.is_dir():
            raise ToolError(f"搜索起点不是目录：{base}")

        matches: list[str] = []
        truncated = False
        for candidate in base.rglob("*"):
            if not candidate.is_file():
                continue
            rel = str(candidate.relative_to(base)).replace("\\", "/")
            if _ignored(rel):
                continue
            if glob_filter and not candidate.match(glob_filter):
                continue
            try:
                text_content = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    import locale

                    enc = locale.getpreferredencoding(False) or "gbk"
                    text_content = candidate.read_text(encoding=enc)
                except Exception:
                    continue
            except OSError:
                continue
            lines = text_content.splitlines()
            for lineno, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                    if len(matches) >= GREP_LIMIT:
                        truncated = True
                        break
            if truncated:
                break
        if not matches:
            return "未找到匹配。"
        text = "\n".join(matches)
        if truncated:
            text += f"\n...（结果过多，仅显示前 {GREP_LIMIT} 条）"
        return text


__all__ = ["Glob", "Grep", "GLOB_LIMIT", "GREP_LIMIT", "IGNORED_DIRS"]
"""第 9 章：项目指令文件加载器。

MEWCODE.md 是给 Agent 看的「入职文档」，MewCode 每次启动新会话时自动读取，
注入 messages 上下文。支持多层优先级（项目级 > 用户级）与 `@include` 模块化引用。

优先级栈（高优先级在前，内容用 --- 分隔线拼接，不是覆盖）：
  1. <项目根>/MEWCODE.md
  2. <项目根>/.mewcode/MEWCODE.md（可被 .gitignore 忽略）
  3. ~/.mewcode/MEWCODE.md（用户级个人偏好）
"""

from __future__ import annotations

from pathlib import Path

INCLUDE_PREFIX = "@include"
MAX_INCLUDE_DEPTH = 5

_BLOCKED_MARKER = "<!-- @include blocked: path outside project -->"
_MISSING_MARKER = "<!-- @include skipped: file not found -->"
_LOOP_MARKER = "<!-- @include skipped: already included -->"


def _resolve_project_root(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve()


def process_includes(
    content: str,
    base_dir: str | Path,
    project_root: str | Path,
    *,
    depth: int = 0,
    visited: set[Path] | None = None,
) -> str:
    """处理 @include 指令：逐行扫描，把 @include 行替换为被引用文件的内容。

    两道安全防线：
      1. depth 限制嵌套深度（最多 5 层），防止 A include B include A 的深循环
      2. visited 集合记录已展开的绝对路径，防止环路重复展开（精确去重）
      3. 路径必须在项目根目录内，越界直接拦截
    文件不存在时不报错，插入 HTML 注释标记后继续处理后面的内容。
    """
    if depth >= MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    base = Path(base_dir).expanduser().resolve()
    root = _resolve_project_root(project_root)
    result: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped.startswith(INCLUDE_PREFIX + " "):
            result.append(line)
            continue
        rel_path = stripped[len(INCLUDE_PREFIX) :].strip()
        if not rel_path:
            result.append(line)
            continue
        target = base / rel_path
        try:
            abs_path = target.resolve(strict=False)
        except (OSError, RuntimeError):
            result.append(_MISSING_MARKER)
            continue
        try:
            abs_path.relative_to(root)
        except ValueError:
            # 越界：@include ../../etc/passwd 这类尝试一律拦截
            result.append(_BLOCKED_MARKER)
            continue
        if abs_path in visited:
            # 环路：A include B、B include A 时跳过重复展开
            result.append(_LOOP_MARKER)
            continue
        if not abs_path.is_file():
            result.append(_MISSING_MARKER)
            continue
        visited.add(abs_path)
        try:
            included = abs_path.read_text(encoding="utf-8")
        except OSError:
            result.append(_MISSING_MARKER)
            continue
        processed = process_includes(
            included, abs_path.parent, root, depth=depth + 1, visited=visited
        )
        result.append(processed)

    return "\n".join(result)


def load_instructions(project_root: str | Path) -> str:
    """按优先级加载所有 MEWCODE.md，用 --- 分隔线拼接成一个完整的指令文本。

    高优先级排在前面：LLM 对 prompt 前面的内容通常更为重视，
    当项目需求与个人偏好冲突时，项目说了算。
    """
    root = _resolve_project_root(project_root)
    home = Path.home()

    candidates = [
        root / "MEWCODE.md",
        root / ".mewcode" / "MEWCODE.md",
        home / ".mewcode" / "MEWCODE.md",
    ]

    sections: list[str] = []
    for path in candidates:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            processed = process_includes(content, path.parent, root)
            if processed.strip():
                sections.append(processed.strip())
    return "\n---\n".join(sections)


__all__ = [
    "INCLUDE_PREFIX",
    "MAX_INCLUDE_DEPTH",
    "load_instructions",
    "process_includes",
]

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseTool, ToolError

# 输出长度上限：超过即截断
MAX_OUTPUT_LENGTH = 30000


def _truncate(text: str, limit: int = MAX_OUTPUT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（内容过长，已截断）"


def _resolve_path(raw: str, workdir: str | None = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        path = base / path
    return path


class ReadFile(BaseTool):
    name = "ReadFile"
    description = (
        "读取文件内容。路径参数必须用绝对路径或相对于项目工作目录的路径。"
        "优先用这个工具而不是 Bash cat/head/tail/sed。编辑文件之前必须先用这个工具读取。"
        "文件过大时截断并提示。"
    )
    safety = "read"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径（绝对路径或相对项目工作目录的路径）"},
        },
        "required": ["path"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        raw = str(args.get("path", "")).strip()
        if not raw:
            raise ToolError("缺少 path 参数")
        target = _resolve_path(raw, self.workdir)
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if target.is_dir():
            raise ToolError(f"路径是目录，无法读取：{target}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                import locale

                enc = locale.getpreferredencoding(False) or "gbk"
                content = target.read_text(encoding=enc)
            except Exception:
                raise ToolError(f"文件不是 UTF-8 文本：{target}") from None
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
        return _truncate(content)


class WriteFile(BaseTool):
    name = "WriteFile"
    description = (
        "创建新文件或覆盖现有文件，自动创建缺失的父目录。"
        "创建文件优先用此工具而非 Bash echo 或 cat heredoc 重定向。"
    )
    safety = "write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径"},
            "content": {"type": "string", "description": "文件内容"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        raw = str(args.get("path", "")).strip()
        content = args.get("content", "")
        if not raw:
            raise ToolError("缺少 path 参数")
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        target = _resolve_path(raw, self.workdir)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        return f"已写入 {len(content)} 字符到 {target}"


class _EditOp:
    __slots__ = ("old_text", "new_text", "replacement")

    def __init__(self, op: dict[str, Any]) -> None:
        self.old_text = str(op.get("old_text", ""))
        self.new_text = str(op.get("new_text", ""))
        # 可选：整段替换而非逐处替换
        self.replacement = bool(op.get("replacement", False))

    def apply(self, content: str) -> tuple[str, int]:
        count = content.count(self.old_text)
        if count == 0:
            raise ToolError(f"未找到待替换文本：{self.old_text[:60]}")
        if count > 1 and not self.replacement:
            raise ToolError(
                f"待替换文本出现 {count} 次，匹配不唯一：{self.old_text[:60]}；如需全部替换请设置 replacement=true"
            )
        replaced = content.replace(self.old_text, self.new_text)
        return replaced, count


class EditFile(BaseTool):
    name = "EditFile"
    description = (
        "对同一文件做多处查找→替换编辑。在编辑文件之前必须先用 ReadFile 读取内容获取准确上下文。"
        "任一段匹配失败则整体撤销，文件保持原样。优先用此工具而非 Bash sed 或 awk。"
    )
    safety = "write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件路径"},
            "edits": {
                "type": "array",
                "description": "编辑段列表，按顺序应用到文件",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "description": "待替换的原文"},
                        "new_text": {"type": "string", "description": "替换后的文本"},
                        "replacement": {
                            "type": "boolean",
                            "description": "为 true 时全部替换；缺省时匹配必须唯一",
                        },
                    },
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        raw = str(args.get("path", "")).strip()
        edits_raw = args.get("edits")
        if not raw:
            raise ToolError("缺少 path 参数")
        if not isinstance(edits_raw, list) or not edits_raw:
            raise ToolError("edits 必须是非空列表")
        try:
            ops = [_EditOp(op) for op in edits_raw]
        except (TypeError, AttributeError, ValueError) as exc:
            raise ToolError(f"edits 格式无效：{exc}") from exc

        target = _resolve_path(raw, self.workdir)
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ToolError(f"文件不存在：{target}") from None
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc

        changes = content
        applied: list[str] = []
        try:
            for op in ops:
                changes, count = op.apply(changes)
                applied.append(f"{count} 处")
        except ToolError:
            raise  # 编辑未生效，文件保持原样

        try:
            target.write_text(changes, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        summary = ", ".join(applied)
        return f"已编辑 {target}：{summary}"


class ReplaceInFile(BaseTool):
    """单段替换的兜底工具：不适配多段编辑的模型可退化为单段替换。"""

    name = "ReplaceInFile"
    description = "替换文件中的一段文本为另一段文本（旧文本必须唯一匹配）。"
    safety = "write"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件路径"},
            "old_text": {"type": "string", "description": "待替换的原文（必须唯一匹配）"},
            "new_text": {"type": "string", "description": "替换后的文本"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        payload = dict(args)
        payload["edits"] = [
            {"old_text": payload.pop("old_text", ""), "new_text": payload.pop("new_text", "")}
        ]
        return await EditFile(workdir=self.workdir)._run(payload)


__all__ = ["ReadFile", "WriteFile", "EditFile", "ReplaceInFile", "MAX_OUTPUT_LENGTH"]
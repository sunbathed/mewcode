"""第 8 章第 1 层：大结果存磁盘 + 决策冻结。

工具结果是 Token 消耗的绝对大头（约 85%）且最容易过时。这一层几乎零
信息损失、零 API 开销：

- 单个工具结果超过阈值（默认 50,000 字符）：完整内容写盘，对话历史里
  只留「预览 + 文件路径」，模型需要时可以用 ReadFile 读回完整内容。
- 单条消息（一轮并发的全部工具结果）合计超过聚合预算（默认 200,000
  字符）：从大到小把结果依次存盘，直到总量回到预算以内。
- 幂等写入：文件名用 tool_use_id，`wx` 模式（已存在即跳过），每轮重复
  执行不会重复写盘。
- 决策冻结（Claude Code 同款策略）：每轮 API 请求前都会重新执行本层
  检查，但每个 tool_use_id 的「替换 / 不替换」决策一经做出就永久冻结。
  已替换的用缓存预览原样重放；已决定不替换的永远保留原文。这样对话
  历史前缀逐字节稳定，Anthropic 式 Prompt Cache 才能持续命中。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..session import Message, ToolResultBlock

# 理论篇定下的默认值（可通过 ContextConfig 覆盖，见 manager.py）
DEFAULT_RESULT_MAX_CHARS = 50_000
DEFAULT_AGGREGATE_BUDGET_CHARS = 200_000
DEFAULT_PREVIEW_CHARS = 2_000

# 存盘点：<workdir>/.mewcode/session/tool-results/
PERSISTED_OUTPUT_DIR = Path(".mewcode") / "session" / "tool-results"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(slots=True)
class ContentReplacementState:
    """决策冻结状态：两本账本。

    seen_ids      之前评估过的 tool_use_id 集合（无论答案是替换还是不替换）
    replacements  id → 当时生成的预览字符串（答案 = 替换）

    规则：
      - seen 且不在 replacements → 已决定「不替换」，永远保留原文，不再评估
      - 在 replacements 中     → 每轮用缓存预览字符串原样重放，前缀逐字节不变
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seen": sorted(self.seen_ids), "replacements": dict(self.replacements)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContentReplacementState":
        data = data or {}
        seen = {str(item) for item in data.get("seen") or []}
        raw_replacements = data.get("replacements") or {}
        replacements = {str(key): str(value) for key, value in raw_replacements.items()}
        return cls(seen_ids=seen, replacements=replacements)


def persisted_output_dir(workdir: str) -> Path:
    """大结果存盘的目录：<workdir>/.mewcode/session/tool-results/。"""
    return Path(workdir) / PERSISTED_OUTPUT_DIR


def _safe_result_filename(use_id: str) -> str:
    """tool_use_id 落盘文件名：只保留安全字符，避免路径穿越之类的问题。"""
    name = _SAFE_ID_RE.sub("_", use_id or "result")
    return name or "result"


def _display_path(path: Path, workdir: str) -> str:
    """对话里展示的路径：workdir 下用相对路径（与理论篇示例一致）。"""
    try:
        rel = os.path.relpath(path, workdir)
    except ValueError:
        rel = str(path)
    if rel.startswith(".."):
        return str(path)
    return rel


def build_preview(use_id: str, content: str, path: Path, workdir: str, preview_chars: int) -> str:
    """构造替换原文的预览字符串；一旦生成就缓存进 replacements，逐字节重放。"""
    size_kb = max(1, len(content) // 1024)
    display = _display_path(path, workdir)
    snippet = content[:preview_chars]
    lines = [
        "<persisted-output>",
        f"输出太大（{size_kb}KB），完整内容已保存到：",
        display,
        "",
        f"预览（前 {len(snippet)} 字符）：",
        snippet,
    ]
    if len(content) > preview_chars:
        lines.append("...")
    lines.append("</persisted-output>")
    return "\n".join(lines)


def persist_result(workdir: str, use_id: str, content: str) -> Path:
    """把完整结果写到磁盘，幂等：文件名含 tool_use_id，`wx` 模式已存在即跳过。"""
    directory = persisted_output_dir(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_safe_result_filename(use_id)}.txt"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return target
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return target


class ToolResultBudget:
    """第 1 层执行器：单结果阈值 + 聚合预算 + 决策冻结，就地改写消息块。"""

    def __init__(
        self,
        *,
        workdir: str,
        state: ContentReplacementState,
        result_max_chars: int = DEFAULT_RESULT_MAX_CHARS,
        aggregate_budget_chars: int = DEFAULT_AGGREGATE_BUDGET_CHARS,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> None:
        self.workdir = workdir
        self.state = state
        self.result_max_chars = result_max_chars
        self.aggregate_budget_chars = aggregate_budget_chars
        self.preview_chars = preview_chars

    def apply(self, messages: Iterable[Message]) -> dict[str, Path]:
        """对一组会话消息执行预算检查，就地替换超限的工具结果。

        返回本轮真正新执行的替换 {tool_use_id: 存盘路径}；已被冻结的
        决策（重放 / 保留原文）不重复计账。
        """
        candidates: list[ToolResultBlock] = []
        for message in messages or []:
            for block in message.blocks or []:
                if not isinstance(block, ToolResultBlock):
                    continue
                if block.tool_use_id in self.state.replacements:
                    # 已替换过：原样重放缓存的预览字符串，保证前缀逐字节不变
                    block.content = self.state.replacements[block.tool_use_id]
                elif block.tool_use_id not in self.state.seen_ids:
                    candidates.append(block)

        if not candidates:
            return {}

        replaced: dict[str, Path] = {}

        def _replace(block: ToolResultBlock) -> None:
            path = persist_result(self.workdir, block.tool_use_id, block.content)
            preview = build_preview(
                block.tool_use_id,
                block.content,
                path,
                self.workdir,
                self.preview_chars,
            )
            self.state.replacements[block.tool_use_id] = preview
            block.content = preview
            replaced[block.tool_use_id] = path

        # 单个结果超阈值：直接替换（最大的优先处理）
        for block in sorted(candidates, key=lambda b: len(b.content), reverse=True):
            if len(block.content) > self.result_max_chars:
                _replace(block)

        # 聚合预算：本轮请求里全部工具结果合计超预算时，从剩余候选里
        # 挑最大的依次存盘，直到总量降到预算以内。
        remaining = [b for b in candidates if b.tool_use_id not in replaced]
        total = self._total_chars(messages)
        while total > self.aggregate_budget_chars and remaining:
            block = max(remaining, key=lambda b: len(b.content))
            remaining.remove(block)
            old_len = len(block.content)
            _replace(block)
            total = total - old_len + len(block.content)

        # 决策冻结：剩下的候选本轮决定「不替换」，永久保留原文
        for block in candidates:
            if block.tool_use_id not in self.state.replacements:
                self.state.seen_ids.add(block.tool_use_id)
        return replaced

    @staticmethod
    def _total_chars(messages: Iterable[Message]) -> int:
        return sum(
            len(block.content)
            for message in messages or []
            for block in message.blocks or []
            if isinstance(block, ToolResultBlock)
        )


__all__ = [
    "ContentReplacementState",
    "ToolResultBudget",
    "build_preview",
    "persist_result",
    "persisted_output_dir",
    "DEFAULT_RESULT_MAX_CHARS",
    "DEFAULT_AGGREGATE_BUDGET_CHARS",
    "DEFAULT_PREVIEW_CHARS",
]
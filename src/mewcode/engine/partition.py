from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..session import ToolUseBlock
from ..tools import ToolRegistry, ToolResult


@dataclass(frozen=True, slots=True)
class Batch:
    """一批可并行的工具调用：批次内全部 is_concurrency_safe 为 True。"""

    calls: tuple[ToolUseBlock, ...]
    is_concurrency_safe: bool

    @property
    def size(self) -> int:
        return len(self.calls)


def _is_safe(registry: ToolRegistry, use: ToolUseBlock) -> bool:
    """查询工具注册中心的安全分类：只读（read）才允许并发，写/命令/未知一律串行。"""
    try:
        return registry.get(use.name).spec.safety == "read"
    except KeyError:
        return False


def partition_tool_calls(tool_uses: list[ToolUseBlock], registry: ToolRegistry) -> list[Batch]:
    """把一次响应里的多个工具调用分批：读类（安全）并发，写类/命令/未知串行。

    规则：同一批内的所有调用必须都是并发安全的；遇到不安全调用就起新批。
    例：[Read, Read, Edit, Read, Read] -> [Read, Read] 并发 -> [Edit] 串行 -> [Read, Read] 并发。
    """
    batches: list[Batch] = []
    for use in tool_uses:
        safe = _is_safe(registry, use)
        if safe and batches and batches[-1].is_concurrency_safe:
            previous = batches[-1]
            batches[-1] = Batch(previous.calls + (use,), True)
        else:
            batches.append(Batch((use,), safe))
    return batches


async def _run_one(registry: ToolRegistry, use: ToolUseBlock) -> ToolResult:
    try:
        return await registry.execute(use.name, use.input)
    except Exception as exc:  # noqa: BLE001 - 工具不存在/禁用等统一转为失败结果，不让循环崩溃
        return ToolResult(is_error=True, error=str(exc) or f"{use.name} 执行失败")


async def run_batch_concurrently(
    registry: ToolRegistry,
    calls: list[ToolUseBlock],
    max_concurrency: int | None = None,
) -> list[ToolResult]:
    """并发执行一批安全调用；可设上限防止无限并发拖垮系统。"""
    if max_concurrency and max_concurrency > 0:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def limited(use: ToolUseBlock) -> ToolResult:
            async with semaphore:
                return await _run_one(registry, use)

        return list(await asyncio.gather(*(limited(use) for use in calls)))
    return list(await asyncio.gather(*(_run_one(registry, use) for use in calls)))


async def run_batch_serially(registry: ToolRegistry, calls: list[ToolUseBlock]) -> list[ToolResult]:
    """串行执行一批（写操作或有副作用的命令），逐个跑。"""
    return [await _run_one(registry, use) for use in calls]


async def execute_batches(
    registry: ToolRegistry,
    tool_uses: list[ToolUseBlock],
    *,
    max_concurrency: int | None = None,
) -> list[ToolResult]:
    """按批次执行：安全批并发、不安全批串行，结果顺序与调用顺序一一对应。"""
    results: list[ToolResult] = []
    for batch in partition_tool_calls(tool_uses, registry):
        if batch.is_concurrency_safe:
            results.extend(
                await run_batch_concurrently(registry, list(batch.calls), max_concurrency=max_concurrency)
            )
        else:
            results.extend(await run_batch_serially(registry, list(batch.calls)))
    return results


__all__ = [
    "Batch",
    "partition_tool_calls",
    "execute_batches",
    "run_batch_concurrently",
    "run_batch_serially",
]

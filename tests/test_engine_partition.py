"""第四章：工具分批执行测试（partitionToolCalls 的并发/串行调度）。

核心契约：读类工具并发、写类工具串行、分批切分正确、执行顺序与错误结果。
"""

import asyncio

import pytest

from mewcode.engine.partition import (
    execute_batches,
    partition_tool_calls,
    run_batch_concurrently,
)
from mewcode.session import ToolUseBlock
from mewcode.tools import ToolRegistry

from conftest import DelayRead, DelayWrite

registry = ToolRegistry()


def use(name: str, args: dict | None = None, tool_id: str = "id") -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=args or {})


def test_empty_tool_uses_yields_no_batches():
    assert partition_tool_calls([], registry) == []


def test_all_read_tools_one_concurrent_batch():
    batches = partition_tool_calls(
        [use("ReadFile"), use("Glob"), use("ReadFile")], registry
    )
    assert len(batches) == 1
    assert batches[0].is_concurrency_safe
    assert batches[0].size == 3


def test_read_read_write_read_read_splits_into_three():
    batches = partition_tool_calls(
        [
            use("ReadFile", tool_id="a"),
            use("ReadFile", tool_id="b"),
            use("WriteFile", tool_id="c"),
            use("ReadFile", tool_id="d"),
            use("ReadFile", tool_id="e"),
        ],
        registry,
    )
    assert [b.size for b in batches] == [2, 1, 2]
    assert [b.is_concurrency_safe for b in batches] == [True, False, True]
    assert batches[1].calls[0].name == "WriteFile"


def test_all_write_tools_each_own_serial_batch():
    batches = partition_tool_calls(
        [use("WriteFile", tool_id="a"), use("Bash", tool_id="b")], registry
    )
    assert [b.is_concurrency_safe for b in batches] == [False, False]
    assert batches[0].size == 1 and batches[1].size == 1


def test_unknown_tool_is_not_concurrency_safe():
    batches = partition_tool_calls([use("NoSuchTool")], registry)
    assert batches[0].is_concurrency_safe is False


@pytest.mark.asyncio
async def test_execute_batches_preserves_call_order():
    local = ToolRegistry()
    local.register(DelayRead())
    local.register(DelayWrite())
    calls = [
        use("DelayRead", {"tag": "1", "delay": 0.01}, tool_id="r1"),
        use("DelayWrite", {"tag": "w", "delay": 0.01}, tool_id="w1"),
        use("DelayRead", {"tag": "2", "delay": 0.01}, tool_id="r2"),
    ]
    results = await execute_batches(local, calls)
    assert [r.output for r in results] == ["read:1", "wrote:w", "read:2"]


@pytest.mark.asyncio
async def test_read_tools_run_concurrently():
    local = ToolRegistry()
    local.register(DelayRead())
    calls = [
        use("DelayRead", {"delay": 0.06}, tool_id="a"),
        use("DelayRead", {"delay": 0.06}, tool_id="b"),
    ]
    start = asyncio.get_event_loop().time()
    await execute_batches(local, calls)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.11  # 并发：总耗时明显小于串行的 0.12s


@pytest.mark.asyncio
async def test_write_tools_run_serially():
    local = ToolRegistry()
    local.register(DelayWrite())
    calls = [
        use("DelayWrite", {"delay": 0.05}, tool_id="a"),
        use("DelayWrite", {"delay": 0.05}, tool_id="b"),
    ]
    start = asyncio.get_event_loop().time()
    await execute_batches(local, calls)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.09  # 串行两个 0.05s


@pytest.mark.asyncio
async def test_run_batch_concurrently_max_concurrency_limits():
    local = ToolRegistry()
    local.register(DelayRead())
    calls = [
        use("DelayRead", {"delay": 0.05}, tool_id="a"),
        use("DelayRead", {"delay": 0.05}, tool_id="b"),
        use("DelayRead", {"delay": 0.05}, tool_id="c"),
    ]
    start = asyncio.get_event_loop().time()
    results = await run_batch_concurrently(local, calls, max_concurrency=1)
    elapsed = asyncio.get_event_loop().time() - start
    assert len(results) == 3
    assert elapsed >= 0.14  # max_concurrency=1 时退化为串行


@pytest.mark.asyncio
async def test_unknown_tool_execution_returns_error_result():
    results = await execute_batches(registry, [use("NoSuchTool", tool_id="x")])
    assert len(results) == 1
    assert results[0].is_error
    assert "不存在" in results[0].error or "NoSuchTool" in results[0].error
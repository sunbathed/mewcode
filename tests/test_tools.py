"""工具系统核心测试：文件工具、搜索工具、Bash、BaseTool 错误/超时、注册中心。

剪掉了磁盘 I/O 高频但低价值的重复用例（超时、cwd、缺失参数等）。
"""

import asyncio

import pytest

from mewcode.tools import ToolRegistry
from mewcode.tools.base import BaseTool
from mewcode.tools.bash import Bash
from mewcode.tools.files import EditFile, ReadFile, WriteFile
from mewcode.tools.search import Glob, Grep


class FailingTool(BaseTool):
    name = "FailingTool"
    description = "总是失败"
    timeout = 1.0

    async def _run(self, args):
        raise ValueError("boom")


class SleepingTool(BaseTool):
    name = "SleepingTool"
    description = "总是超时"
    timeout = 0.05

    async def _run(self, args):
        await asyncio.sleep(1)
        return "done"


@pytest.mark.asyncio
async def test_read_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld", encoding="utf-8")
    tool = ReadFile(workdir=str(tmp_path))
    result = await tool.run({"path": "a.txt"})
    assert result.success
    assert result.output == "hello\nworld"


@pytest.mark.asyncio
async def test_write_file_creates_parents(tmp_path):
    tool = WriteFile(workdir=str(tmp_path))
    result = await tool.run({"path": "sub/b.txt", "content": "hi"})
    assert result.success
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_edit_file_multi_edit(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    tool = EditFile(workdir=str(tmp_path))
    result = await tool.run(
        {
            "path": "c.txt",
            "edits": [
                {"old_text": "alpha", "new_text": "ALPHA"},
                {"old_text": "gamma", "new_text": "GAMMA"},
            ],
        }
    )
    assert result.success
    assert f.read_text(encoding="utf-8") == "ALPHA beta GAMMA"


@pytest.mark.asyncio
async def test_edit_file_fails_atomically(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text("only alpha", encoding="utf-8")
    tool = EditFile(workdir=str(tmp_path))
    result = await tool.run(
        {
            "path": "d.txt",
            "edits": [
                {"old_text": "alpha", "new_text": "ALPHA"},
                {"old_text": "missing", "new_text": "X"},
            ],
        }
    )
    assert not result.success
    assert f.read_text(encoding="utf-8") == "only alpha"  # 任一段失败整体不生效
    assert "未找到" in result.error


@pytest.mark.asyncio
async def test_edit_file_ambiguous_rejected(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("same same", encoding="utf-8")
    tool = EditFile(workdir=str(tmp_path))
    result = await tool.run(
        {"path": "e.txt", "edits": [{"old_text": "same", "new_text": "SAME"}]}
    )
    assert not result.success
    assert "唯一" in result.error


@pytest.mark.asyncio
async def test_glob_ignores_hidden_dirs(tmp_path):
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "z.py").write_text("", encoding="utf-8")
    tool = Glob(workdir=str(tmp_path))
    result = await tool.run({"pattern": "**/*.py"})
    assert result.success
    assert "x.py" in result.output
    assert "z.py" not in result.output


@pytest.mark.asyncio
async def test_grep(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    pass\n# comment", encoding="utf-8")
    tool = Grep(workdir=str(tmp_path))
    result = await tool.run({"pattern": "hello"})
    assert result.success
    assert "a.py:1:def hello()" in result.output


@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path):
    tool = Grep(workdir=str(tmp_path))
    result = await tool.run({"pattern": "(["})
    assert not result.success
    assert "无效" in result.error


@pytest.mark.asyncio
async def test_base_tool_error_conversion():
    tool = FailingTool()
    result = await tool.run({})
    assert not result.success
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_base_tool_timeout():
    tool = SleepingTool()
    result = await tool.run({})
    assert not result.success
    assert result.timed_out
    assert "超时" in result.error


def test_registry_defaults_and_api_format():
    registry = ToolRegistry()
    # 第七章：默认注册 6 个内置工具 + ToolSearch（延迟加载入口，始终可见）
    assert set(registry.names()) == {"ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep", "ToolSearch"}
    specs = registry.api_specs()
    assert len(specs) == 7
    assert specs[0]["type"] == "function"


@pytest.mark.asyncio
async def test_registry_disable_and_unknown():
    registry = ToolRegistry()
    registry.disable("Grep")
    assert all(t.name != "Grep" for t in registry.enabled_tools())
    result = await registry.execute("Grep", {"pattern": "x"})
    assert result.is_error
    assert "已禁用" in result.error
    with pytest.raises(KeyError):
        registry.get("NoSuchTool")


@pytest.mark.asyncio
async def test_bash_output(tmp_path):
    tool = Bash(workdir=str(tmp_path))
    result = await tool.run({"command": "echo hello"})
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error():
    tool = Bash()
    result = await tool.run({"command": "exit 3"})
    assert result.is_error
    assert "3" in result.error
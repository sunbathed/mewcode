"""第四章：Plan Mode 测试（/plan 只读探索、plan 文件放行、拦截写操作）。

核心契约：状态机、只读工具集、写拦截与 plan 文件放行、文件的读写列。
"""

from pathlib import Path

import pytest

from mewcode.engine.plan_mode import (
    PlanModeState,
    build_plan_mode_tools,
    intercept_write,
    list_plan_files,
    read_plan_file,
    save_plan_file,
)
from mewcode.session import ToolUseBlock

from conftest import make_registry


def use(name: str, args: dict | None = None, tool_id: str = "id") -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=args or {})


def test_default_state_inactive():
    state = PlanModeState()
    assert state.active is False
    assert state.plan_file is None


def test_enter_creates_plans_dir_and_file(tmp_path):
    state = PlanModeState()
    path = state.enter(str(tmp_path))
    assert state.active is True
    assert (tmp_path / ".mewcode" / "plans").is_dir()
    assert Path(path).parent == (tmp_path / ".mewcode" / "plans")
    assert path.endswith(".plan.md")


def test_exit_clears_state(tmp_path):
    state = PlanModeState()
    state.enter(str(tmp_path))
    state.exit()
    assert state.active is False
    assert state.plan_file is None


def test_build_plan_mode_tools_only_read_tools(tmp_path):
    registry = make_registry(tmp_path, extra=False)
    specs = build_plan_mode_tools(registry)
    names = {spec["function"]["name"] for spec in specs}
    assert names == {"ReadFile", "Glob", "Grep"}
    assert "WriteFile" not in names
    assert "Bash" not in names


def test_intercept_write_blocks_non_plan_file(tmp_path):
    state = PlanModeState()
    state.enter(str(tmp_path))
    reason = intercept_write(state, use("WriteFile", {"path": "src/app.py"}))
    assert reason is not None
    assert "plan" in reason.lower() or "规划模式" in reason


def test_intercept_write_allows_plan_file(tmp_path):
    state = PlanModeState()
    path = state.enter(str(tmp_path))
    reason = intercept_write(state, use("WriteFile", {"path": path}))
    assert reason is None


def test_intercept_write_blocks_edit_and_bash(tmp_path):
    state = PlanModeState()
    state.enter(str(tmp_path))
    assert intercept_write(state, use("EditFile", {"path": "a.py"})) is not None
    assert intercept_write(state, use("Bash", {"command": "rm -rf ."})) is not None


def test_intercept_write_inactive_allows_everything():
    state = PlanModeState()  # 未激活
    assert intercept_write(state, use("WriteFile", {"path": "a.py"})) is None
    assert intercept_write(state, use("Bash", {"command": "ls"})) is None


def test_save_and_read_plan_file(tmp_path):
    path = save_plan_file(str(tmp_path / ".mewcode" / "plans" / "p.plan.md"), "# 计划\n1. 调研")
    assert path.exists()
    assert read_plan_file(str(path)) == "# 计划\n1. 调研"


def test_list_plan_files(tmp_path):
    directory = tmp_path / ".mewcode" / "plans"
    directory.mkdir(parents=True)
    (directory / "plan-001.plan.md").write_text("a", encoding="utf-8")
    (directory / "plan-002.plan.md").write_text("b", encoding="utf-8")
    files = list_plan_files(str(tmp_path))
    assert files == ["plan-001.plan.md", "plan-002.plan.md"]

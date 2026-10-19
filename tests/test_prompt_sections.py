from __future__ import annotations

from mewcode.engine.builder import EnvironmentContext
from mewcode.engine.sections import (
    doing_tasks_section,
    environment_section,
    executing_actions_section,
    identity_section,
    using_tools_section,
)


def test_identity_section():
    sec = identity_section()
    assert sec.name == "Identity"
    assert sec.priority == 0
    assert "你是 MewCode" in sec.content
    assert "AI 编程助手" in sec.content


def test_doing_tasks_section():
    sec = doing_tasks_section()
    assert sec.name == "DoingTasks"
    assert sec.priority == 20
    assert "三行相似代码比过早抽象好" in sec.content
    assert "默认不写注释" in sec.content
    assert "不要做出超出任务范围的功能" in sec.content


def test_executing_actions_section():
    sec = executing_actions_section()
    assert sec.name == "ExecutingActions"
    assert sec.priority == 30
    assert "谨慎执行操作" in sec.content
    assert "rm -rf" in sec.content
    assert "难以撤销" in sec.content


def test_using_tools_section():
    sec = using_tools_section()
    assert sec.name == "UsingTools"
    assert sec.priority == 40
    assert "有专用工具时绝不要用 Bash" in sec.content
    assert "读文件用 ReadFile，而不是 cat" in sec.content
    assert "编辑文件用 EditFile，而不是 sed" in sec.content


def test_environment_section():
    env = EnvironmentContext(
        work_dir="/app/demo",
        os_name="Linux",
        arch="arm64",
        shell="zsh",
        is_git_repo=True,
        git_branch="main",
        date="2026-10-14",
        model="claude-3-5-sonnet",
    )
    sec = environment_section(env)
    assert sec.name == "Environment"
    assert sec.priority == 70
    assert "- 工作目录：/app/demo" in sec.content
    assert "- 平台：Linux/arm64" in sec.content
    assert "- 是否 Git 仓库：true" in sec.content
    assert "- 模型：claude-3-5-sonnet" in sec.content

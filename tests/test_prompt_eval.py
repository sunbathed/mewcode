from __future__ import annotations

from mewcode.engine.builder import EnvironmentContext, build_system_prompt
from mewcode.tools.bash import Bash
from mewcode.tools.files import EditFile, ReadFile


def _get_full_prompt() -> str:
    env = EnvironmentContext(
        work_dir="/workspace/demo",
        os_name="Linux",
        arch="x86_64",
        shell="bash",
        is_git_repo=True,
        git_branch="main",
        date="2026-10-14",
        model="demo-model",
    )
    return build_system_prompt(env=env)


def test_eval_scenario_2_tool_preference():
    """场景 2：工具偏好与双重强化（查看文件内容：优先专用工具而非 Bash cat）。"""
    prompt = _get_full_prompt()
    # 1. System Prompt 全局规则
    assert "有专用工具时绝不要用 Bash" in prompt
    assert "读文件用 ReadFile，而不是 cat" in prompt
    assert "编辑文件用 EditFile，而不是 sed" in prompt

    # 2. Tool Description 局部规则双重强化
    read_desc = ReadFile().description
    assert "优先用这个工具而不是 Bash cat" in read_desc
    bash_desc = Bash().description
    assert "读文件、编辑文件、创建文件、查找或搜索文件绝不要用 Bash" in bash_desc


def test_eval_scenario_4_cautious_operations():
    """场景 4：高风险操作识别（你觉得哪些执行操作，需要谨慎）。"""
    prompt = _get_full_prompt()
    assert "谨慎执行操作" in prompt
    assert "破坏性操作：删除文件/分支、删除数据库表、rm -rf、覆盖未提交改动" in prompt
    assert "难以撤销的操作：force-push、git reset --hard、修改已发布 commit、卸载依赖包" in prompt
    assert "影响他人的操作：push 代码、创建/关闭 PR 或 issue、发送消息、修改共享基础设施" in prompt

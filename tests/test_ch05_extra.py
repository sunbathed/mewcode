"""第五章补充核心测试。

从第五章补充测试中保留最能代表行为契约的用例：
  - PromptBuilder 边界与 Section 不可变
  - environment_section 分支（非 git / 无 model）
  - System Prompt 不含 Plan Mode 污染、空 BuildOptions 不注入
  - Section 深度内容（先读后改、工具映射）
  - Plan Mode 提醒的自定义间隔
  - prompt_pipeline 不传 tools 时无 tool_choice
  - UsageEvent cached_tokens 默认值
  - 工具描述双重强化（EditFile 先读、Bash 禁读写搜索）
  - detect_environment 路径解析
"""

from __future__ import annotations

import pytest

from mewcode.engine.builder import (
    BuildOptions,
    EnvironmentContext,
    PromptBuilder,
    Section,
    build_system_prompt,
    detect_environment,
)
from mewcode.engine.events import UsageEvent
from mewcode.engine.plan_mode import build_plan_mode_reminder
from mewcode.engine.prompt_pipeline import assemble_api_payload
from mewcode.engine.sections import doing_tasks_section, environment_section, using_tools_section
from mewcode.tools.bash import Bash
from mewcode.tools.files import EditFile


def _make_env(**kwargs) -> EnvironmentContext:
    defaults = dict(
        work_dir="/proj", os_name="Linux", arch="x86_64", shell="bash",
        is_git_repo=False, git_branch="", date="2026-01-01", model="",
    )
    defaults.update(kwargs)
    return EnvironmentContext(**defaults)


# ── PromptBuilder 边界 ────────────────────────────────────────────────


def test_builder_empty_returns_empty_string():
    assert PromptBuilder().build() == ""


def test_builder_duplicate_priorities_stable():
    builder = PromptBuilder()
    builder.add(Section("X", 5, "alpha"))
    builder.add(Section("Y", 5, "beta"))
    out = builder.build()
    assert out.index("alpha") < out.index("beta")  # stable sort：先插入在前


def test_section_is_frozen():
    sec = Section("A", 1, "hello")
    with pytest.raises((AttributeError, TypeError)):
        sec.content = "modified"  # type: ignore[misc]


# ── environment_section 分支 ──────────────────────────────────────────


def test_environment_context_non_git():
    env = _make_env(is_git_repo=False, git_branch="")
    sec = environment_section(env)
    assert "- 是否 Git 仓库：false" in sec.content
    assert "- Git 分支：" not in sec.content


def test_environment_section_no_model_omits_model_line():
    sec = environment_section(_make_env(model=""))
    assert "- 模型：" not in sec.content


# ── build_system_prompt 完整性 ─────────────────────────────────────────


def test_build_system_prompt_no_plan_mode_contamination():
    prompt = build_system_prompt(env=_make_env())
    assert "Plan mode is active" not in prompt
    assert "规划模式" not in prompt
    assert "五阶段工作流" not in prompt


def test_build_system_prompt_empty_options_omit_optional():
    prompt = build_system_prompt(env=_make_env(), options=BuildOptions())
    assert "CustomInstructions" not in prompt
    assert "SkillInstructions" not in prompt
    assert "MemoryInstructions" not in prompt


# ── Section 深度内容 ───────────────────────────────────────────────────


def test_doing_tasks_section_file_read_before_edit():
    assert "如果用户问或要求某个文件，先读它" in doing_tasks_section().content


def test_using_tools_section_tool_mapping():
    sec = using_tools_section()
    assert "创建文件用 WriteFile，而不是 echo 或 cat heredoc" in sec.content
    assert "查找文件用 Glob，而不是 find 或 ls" in sec.content
    assert "搜索文件内容用 Grep，而不是 grep 或 rg" in sec.content


# ── Plan Mode 提醒：自定义间隔 ─────────────────────────────────────────


def test_plan_reminder_interval_custom_3():
    path = "/workspace/.mewcode/plans/plan-001.plan.md"
    for turn in (1, 4, 7):
        r = build_plan_mode_reminder(path, plan_exists=True, iteration=turn, reminder_interval=3)
        assert "五阶段工作流" in r, f"turn {turn} should be full"
    for turn in (2, 3, 5, 6):
        r = build_plan_mode_reminder(path, plan_exists=True, iteration=turn, reminder_interval=3)
        assert "五阶段工作流" not in r, f"turn {turn} should be sparse"


# ── prompt_pipeline 边界 ───────────────────────────────────────────────


def test_assemble_payload_no_tools_no_tool_choice():
    payload = assemble_api_payload("sys", [{"role": "user", "content": "hi"}])
    assert "tools" not in payload
    assert "tool_choice" not in payload


# ── UsageEvent cached_tokens ───────────────────────────────────────────


def test_usage_event_default_cached_tokens_zero():
    ev = UsageEvent(turn=1, input_tokens=100, output_tokens=50)
    assert ev.cached_tokens == 0
    assert ev.total_cached_tokens == 0


# ── 工具描述双重强化 ───────────────────────────────────────────────────


def test_edit_file_description_requires_read_first():
    desc = EditFile().description
    assert "ReadFile" in desc
    assert "读取" in desc


def test_bash_description_no_read_write_search():
    desc = Bash().description
    assert "读文件" in desc and "绝不要用 Bash" in desc


# ── detect_environment ─────────────────────────────────────────────────


def test_detect_environment_with_tmp_path(tmp_path):
    env = detect_environment(workdir=str(tmp_path))
    assert env.work_dir == str(tmp_path.resolve())
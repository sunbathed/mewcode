from __future__ import annotations

from mewcode.engine.builder import (
    BuildOptions,
    EnvironmentContext,
    PromptBuilder,
    Section,
    build_system_prompt,
)


def test_builder_sorts_by_priority():
    builder = PromptBuilder()
    builder.add(Section("C", 30, "third"))
    builder.add(Section("A", 10, "first"))
    builder.add(Section("B", 20, "second"))
    out = builder.build()
    assert out == "first\n\nsecond\n\nthird"


def test_builder_filters_empty_sections():
    builder = PromptBuilder()
    builder.add(Section("A", 10, "first"))
    builder.add(Section("Empty", 15, ""))
    builder.add(Section("Whitespace", 16, "   \n\t  "))
    builder.add(Section("B", 20, "second"))
    out = builder.build()
    assert out == "first\n\nsecond"


def test_build_system_prompt_includes_core_and_env():
    env = EnvironmentContext(
        work_dir="/test/project",
        os_name="Linux",
        arch="x86_64",
        shell="bash",
        is_git_repo=True,
        git_branch="feature/ch05",
        date="2026-10-14",
        model="glm-5.1",
    )
    prompt = build_system_prompt(env=env)
    assert "你是 MewCode" in prompt
    assert "# 系统" in prompt
    assert "# 任务执行" in prompt
    assert "# 谨慎执行操作" in prompt
    assert "# 使用你的工具" in prompt
    assert "# 语气与风格" in prompt
    assert "# 文本输出（不适用于工具调用）" in prompt
    assert "- 工作目录：/test/project" in prompt
    assert "- Git 分支：feature/ch05" in prompt
    assert "- 模型：glm-5.1" in prompt


def test_build_system_prompt_conditional_options():
    env = EnvironmentContext(work_dir="/test/project", os_name="Linux")
    options = BuildOptions(
        custom_instructions="项目要求：所有函数必须带类型注解",
        skill_instructions="可用技能：[web-search, test-runner]",
        memory_instructions="用户偏好：使用 pytest 代替 unittest",
    )
    prompt = build_system_prompt(env=env, options=options)
    assert "所有函数必须带类型注解" in prompt
    assert "可用技能：[web-search, test-runner]" in prompt
    assert "使用 pytest 代替 unittest" in prompt
    # 可选模块排在核心七模块和环境模块（70）之后
    idx_env = prompt.find("# 环境")
    idx_custom = prompt.find("所有函数必须带类型注解")
    idx_skill = prompt.find("可用技能")
    idx_memory = prompt.find("用户偏好")
    assert idx_env < idx_custom < idx_skill < idx_memory

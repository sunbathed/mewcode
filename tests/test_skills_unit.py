"""第 11 章：Skill 系统单元测试（解析 / 加载 / 覆盖 / 热加载 / 目录型）。"""

import json
from pathlib import Path

import pytest

from mewcode.skills import (
    ARGUMENTS_PLACEHOLDER,
    Skill,
    SkillLoader,
    SkillNotFoundError,
    parse_skill_file,
    render_prompt_body,
)
from mewcode.skills.types import (
    SKILL_ENTRY_FILE,
    SKILL_TOOL_FILE,
    MODE_FORK,
    MODE_INLINE,
    SkillParseError,
    parse_skill_tools,
)

SAMPLE = """---
name: commit
description: 分析 git diff 并生成规范的 commit
allowedTools:
  - Bash
  - ReadFile
mode: inline
---

# 任务

$ARGUMENTS
"""


def test_parse_skill_frontmatter_and_body():
    skill = parse_skill_file(SAMPLE)
    assert skill.name == "commit"
    assert skill.description == "分析 git diff 并生成规范的 commit"
    assert skill.allowed_tools == ("Bash", "ReadFile")
    assert skill.mode == MODE_INLINE
    assert "# 任务" in skill.prompt_body
    assert "$ARGUMENTS" in skill.prompt_body


def test_parse_skill_missing_frontmatter():
    with pytest.raises(SkillParseError):
        parse_skill_file("plain text without frontmatter")


def test_parse_skill_missing_required_fields():
    with pytest.raises(SkillParseError, match="name"):
        parse_skill_file("---\ndescription: 没有名字\n---\n body")


def test_parse_skill_invalid_mode():
    with pytest.raises(SkillParseError, match="mode"):
        parse_skill_file(
            "---\nname: x\ndescription: d\nmode: weird\n---\nbody"
        )


def test_parse_skill_bad_yaml():
    with pytest.raises(SkillParseError):
        parse_skill_file("---\nname: [unclosed\n---\nbody")


def test_parse_skill_fork_context():
    skill = parse_skill_file(
        "---\nname: review\ndescription: 审查\nmode: fork\ncontext: none\n---\nbody"
    )
    assert skill.mode == MODE_FORK
    assert skill.context == "none"


def test_render_prompt_body_replaces_arguments():
    body = "审查代码。\n$ARGUMENTS\n如果没有指定审查范围…"
    rendered = render_prompt_body(body, "重点关注安全问题")
    assert "重点关注安全问题" in rendered
    assert ARGUMENTS_PLACEHOLDER not in rendered
    # 无参数替换为空
    empty = render_prompt_body(body, "")
    assert "$ARGUMENTS" not in empty


def test_skill_render_method():
    skill = parse_skill_file(SAMPLE)
    rendered = skill.render("feat: add login")
    assert "feat: add login" in rendered


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def test_builtin_skills_loaded(tmp_path):
    loader = SkillLoader(tmp_path)
    catalog = loader.load_catalog()
    names = {s.name for s in catalog}
    assert {"commit", "review", "test"} <= names
    # 内置来源标记
    assert any(s.source_rank == 2 for s in catalog)


def test_project_skill_overrides_userlevel(tmp_path, monkeypatch):
    """同名 Skill 按优先级覆盖：项目级盖用户级。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

    project_skills = tmp_path / ".mewcode" / "skills"
    project_skills.mkdir(parents=True)
    (project_skills / "commit.md").write_text(
        "---\nname: commit\ndescription: 项目版 commit\n---\n项目 SOP",
        encoding="utf-8",
    )

    user_skills = tmp_path / "home" / ".mewcode" / "skills"
    user_skills.mkdir(parents=True)
    (user_skills / "commit.md").write_text(
        "---\nname: commit\ndescription: 用户版 commit\n---\n用户 SOP",
        encoding="utf-8",
    )

    loader = SkillLoader(tmp_path)
    catalog = loader.load_catalog()
    commits = [s for s in catalog if s.name == "commit"]
    assert len(commits) == 1  # 同名只保留高优先级
    assert commits[0].source_rank == 0  # 项目级
    assert "项目版" in commits[0].description


def test_user_skill_overrides_builtin(tmp_path, monkeypatch):
    """用户级 Skill 覆盖内置同名 Skill。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    user_skills = tmp_path / "home" / ".mewcode" / "skills"
    user_skills.mkdir(parents=True)
    (user_skills / "test.md").write_text(
        "---\nname: test\ndescription: 用户自定义测试流程\n---\n自定义",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    skill = loader.get("test")
    assert skill.description == "用户自定义测试流程"
    assert skill.source_rank == 1


def test_get_unknown_skill_raises(tmp_path):
    loader = SkillLoader(tmp_path)
    with pytest.raises(SkillNotFoundError):
        loader.get("no-such-skill")


def test_hot_reload_picks_up_changes(tmp_path, monkeypatch):
    """热加载：修改 Skill 文件后 get() 拿到最新内容。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    user_skills = tmp_path / "home" / ".mewcode" / "skills"
    user_skills.mkdir(parents=True)
    path = user_skills / "deploy.md"
    path.write_text("---\nname: deploy\ndescription: 部署\n---\n旧 SOP", encoding="utf-8")
    loader = SkillLoader(tmp_path)
    assert "旧 SOP" in loader.get("deploy").prompt_body
    path.write_text("---\nname: deploy\ndescription: 部署\n---\n新 SOP", encoding="utf-8")
    assert "新 SOP" in loader.get("deploy").prompt_body


def test_bad_file_skipped_and_cache_fallback(tmp_path, monkeypatch):
    """解析失败的文件跳过 + get 失败回退缓存。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    user_skills = tmp_path / "home" / ".mewcode" / "skills"
    user_skills.mkdir(parents=True)
    # 正常 Skill
    good = user_skills / "good.md"
    good.write_text("---\nname: good\ndescription: 好\n---\n内容", encoding="utf-8")
    # 坏 Skill（缺 frontmatter）——被跳过不阻断加载
    bad = user_skills / "bad.md"
    bad.write_text("不是 frontmatter", encoding="utf-8")

    loader = SkillLoader(tmp_path)
    names = {s.name for s in loader.load_catalog()}
    assert "good" in names
    assert "bad" not in names

    # get 后缓存；文件变坏回退缓存
    assert loader.get("good").prompt_body == "内容"
    good.write_text("{broken content no frontmatter", encoding="utf-8")
    assert loader.get("good").prompt_body == "内容"  # 回退缓存


def test_catalog_text_lists_skills(tmp_path):
    loader = SkillLoader(tmp_path)
    text = loader.catalog_text()
    assert "skill：" in text.lower() or "Skill" in text
    assert "LoadSkill" in text


# ---------------------------------------------------------------------------
# 目录型 Skill（tool.json）
# ---------------------------------------------------------------------------


def test_directory_skill_with_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    root = tmp_path / "proj"
    skill_dir = root / ".mewcode" / "skills" / "backend-interview"
    refs = skill_dir / "references"
    refs.mkdir(parents=True)
    (skill_dir / SKILL_ENTRY_FILE).write_text(
        "---\nname: backend-interview\ndescription: 后端面试\nallowedTools:\n  - parse_resume\nmode: inline\n---\nSOP",
        encoding="utf-8",
    )
    (skill_dir / SKILL_TOOL_FILE).write_text(
        json.dumps(
            [
                {
                    "name": "parse_resume",
                    "description": "解析简历",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    "script": "parse_resume.py",
                }
            ]
        ),
        encoding="utf-8",
    )
    (refs / "parse_resume.py").write_text("print('script')", encoding="utf-8")

    loader = SkillLoader(root)
    skill = loader.get("backend-interview")
    assert len(skill.tools) == 1
    assert skill.tools[0].name == "parse_resume"
    assert skill.tools[0].script == "parse_resume.py"
    script = loader.resolve_tool_script(skill, skill.tools[0])
    assert script is not None and script.exists()


def test_parse_skill_tools_helper():
    tools = parse_skill_tools(
        [{"name": "t1", "description": "d1", "parameters": {"type": "object"}}],
        Path("."),
    )
    assert len(tools) == 1
    assert tools[0].name == "t1"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


def test_skill_display_marker():
    skill = parse_skill_file(SAMPLE)
    assert "[skill]" in skill.display
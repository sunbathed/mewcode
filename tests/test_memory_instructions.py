"""第 9 章：指令文件加载器（MEWCODE.md 多层优先级 + @include 安全展开）。"""

from pathlib import Path

import pytest

from mewcode.memory.instructions import load_instructions, process_includes


def test_project_root_mewcode(tmp_path):
    (tmp_path / "MEWCODE.md").write_text("# 项目\n\n技术栈：Python", encoding="utf-8")
    assert "技术栈：Python" in load_instructions(tmp_path)


def test_priority_stack_project_over_user(tmp_path, monkeypatch):
    """三层优先级全部拼接，项目级排在最前面。"""
    user_dir = tmp_path / "home" / ".mewcode"
    user_dir.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

    (tmp_path / "MEWCODE.md").write_text("项目级指令", encoding="utf-8")
    (tmp_path / ".mewcode").mkdir()
    (tmp_path / ".mewcode" / "MEWCODE.md").write_text("项目级（可忽略）指令", encoding="utf-8")
    (user_dir / "MEWCODE.md").write_text("用户级偏好", encoding="utf-8")

    out = load_instructions(tmp_path)
    assert out.index("项目级指令") < out.index("用户级偏好")
    assert "项目级（可忽略）指令" in out
    assert out.count("---") >= 2


def test_include_expansion(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "coding.md").write_text("commit message 用英文", encoding="utf-8")
    (tmp_path / "MEWCODE.md").write_text(
        "# 项目\n@include ./docs/coding.md", encoding="utf-8"
    )
    out = load_instructions(tmp_path)
    assert "commit message 用英文" in out
    assert "@include" not in out


def test_include_nested_recursion(tmp_path):
    (tmp_path / "a.md").write_text("A 内容\n@include ./b.md", encoding="utf-8")
    (tmp_path / "b.md").write_text("B 内容", encoding="utf-8")
    out = process_includes((tmp_path / "a.md").read_text(encoding="utf-8"), tmp_path, tmp_path)
    assert "A 内容" in out
    assert "B 内容" in out


def test_include_depth_limit(tmp_path):
    """深度超过 5 层时停止展开，不报错。"""
    for i in range(8):
        (tmp_path / f"f{i}.md").write_text(
            f"level{i}\n@include ./f{i + 1}.md", encoding="utf-8"
        )
    (tmp_path / "f8.md").write_text("deep", encoding="utf-8")
    out = process_includes((tmp_path / "f0.md").read_text(encoding="utf-8"), tmp_path, tmp_path)
    assert "level0" in out
    assert "level5" in out  # depth 5 以内展开


def test_include_loop_terminates(tmp_path):
    """A include B、B include A：visited 集合防环路，不会死循环。"""
    (tmp_path / "a.md").write_text("@include ./b.md", encoding="utf-8")
    (tmp_path / "b.md").write_text("@include ./a.md", encoding="utf-8")
    out = process_includes((tmp_path / "a.md").read_text(encoding="utf-8"), tmp_path, tmp_path)
    assert "already included" in out


def test_include_path_escape_blocked(tmp_path):
    """@include 路径越出项目目录直接拦截。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("系统机密", encoding="utf-8")
    (tmp_path / "MEWCODE.md").write_text("@include ../secret.txt", encoding="utf-8")
    out = load_instructions(tmp_path)
    assert "系统机密" not in out
    assert "blocked" in out


def test_include_missing_file_no_error(tmp_path):
    (tmp_path / "MEWCODE.md").write_text("@include ./ghost.md", encoding="utf-8")
    out = load_instructions(tmp_path)
    assert "not found" in out


def test_include_non_include_lines_preserved(tmp_path):
    (tmp_path / "extra.md").write_text("额外内容", encoding="utf-8")
    content = "第一行\n@include ./extra.md\n@include ./ghost.md\n"
    out = process_includes(content, tmp_path, tmp_path)
    assert out.splitlines()[0] == "第一行"
    assert "额外内容" in out
    assert "not found" in out  # 不存在的文件插入标记，不影响其余行


def test_absolute_include_within_project(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("子目录内容", encoding="utf-8")
    abs_path = (tmp_path / "sub" / "x.md").resolve()
    out = process_includes(f"@include {abs_path}", tmp_path, tmp_path)
    assert "子目录内容" in out

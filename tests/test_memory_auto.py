"""第 9 章：自动记忆（显式记住、四类分类、LLM 提取、索引上限、占位符过滤）。"""

import pytest

from mewcode.llm_client import StreamEnd, TextDelta
from mewcode.memory.auto_memory import (
    EXTRACTION_EVERY_N_MESSAGES,
    MemoryManager,
    classify_memory,
)
from mewcode.session import Message


class MemoryExtractClient:
    """回放 LLM 提取输出：四分类 + 条目。"""

    def __init__(self, response: str):
        self.response = response
        self.requests = []

    async def stream(self, messages):
        self.requests.append(messages)
        yield TextDelta(self.response)
        yield StreamEnd("stop")


def make_manager(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.memory.auto_memory.Path.home", lambda: tmp_path / "home")
    return MemoryManager(tmp_path)


def test_classify_memory():
    assert classify_memory("我是 Go 工程师") == "user"
    assert classify_memory("不要用 tab") == "feedback"
    assert classify_memory("项目用 PostgreSQL 15") == "project"
    assert classify_memory("文档在 https://docs.example.com") == "reference"


def test_remember_user_role_writes_memory_and_user_role(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    written = manager.remember("记住我是 Go 工程师，具备深入 Go 语言专业知识")
    assert len(written) == 2  # 记忆文件 + user_role.md

    index = manager.load_index()
    assert "Go 工程师" in index
    user_role = (tmp_path / "home" / ".mewcode" / "memory" / "user_role.md").read_text(encoding="utf-8")
    assert "Go 工程师" in user_role


def test_remember_project_knowledge_writes_project_dir(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    manager.remember("记住项目用 golang-migrate 做数据库迁移")
    # 项目知识 → 项目级目录
    project_index = (tmp_path / ".mewcode" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "golang-migrate" in project_index
    # 用户画像没有写入（非自我描述）
    assert not (tmp_path / "home" / ".mewcode" / "memory" / "user_role.md").exists()


def test_remember_empty_raises(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    from mewcode.memory.auto_memory import MemoryManagerError

    with pytest.raises(MemoryManagerError):
        manager.remember("记住")


async def test_auto_extract_writes_four_sections(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    response = (
        "### 用户偏好\n"
        "- 回复尽量用中文\n"
        "### 纠正反馈\n"
        "- 这里应该用互斥锁而不是消息通道\n"
        "### 项目知识\n"
        "- 部署脚本在 scripts/ 目录\n"
        "### 参考资料\n"
        "- API 文档在 https://docs.example.com\n"
    )
    client = MemoryExtractClient(response)
    messages = [Message("user", "帮我看看部署脚本"), Message("assistant", "部署脚本在 scripts/ 目录")]
    result = await manager.extract(client, messages)
    assert "用户偏好" in result
    assert "项目知识" in result

    index = manager.load_index()
    assert "回复尽量用中文" in index
    assert "部署脚本" in index
    # 用户偏好/纠正反馈 → 用户级；项目知识/参考资料 → 项目级
    user_memory = (tmp_path / "home" / ".mewcode" / "memory")
    project_memory = (tmp_path / ".mewcode" / "memory")
    assert "回复尽量用中文" in (user_memory / "MEMORY.md").read_text(encoding="utf-8")
    assert "部署脚本" in (project_memory / "MEMORY.md").read_text(encoding="utf-8")


async def test_auto_extract_filters_placeholders(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    response = (
        "### 用户偏好\n"
        "- ...\n"
        "- 暂无\n"
        "### 项目知识\n"
        "- N/A\n"
        "### 参考资料\n"
        "- 真实链接 https://docs.example.com/api\n"
    )
    client = MemoryExtractClient(response)
    result = await manager.extract(client, [Message("user", "hi"), Message("assistant", "hello")])
    index = manager.load_index()
    assert "..." not in index
    assert "暂无" not in index
    assert "N/A" not in index
    assert "真实链接" in index


async def test_auto_extract_no_new_messages_skips_llm(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    client = MemoryExtractClient("### 项目知识\n- 不该被调用\n")
    await manager.extract(client, [Message("user", "第一次")])
    result2 = await manager.extract(client, [])  # 无新消息：不调 LLM
    assert result2 == ""
    assert len(client.requests) == 1


def test_extraction_frequency():
    manager = MemoryManager(".")  # 目录随意，只测计数逻辑
    assert manager.should_extract(EXTRACTION_EVERY_N_MESSAGES) is True
    manager._last_extraction_msg_count = 5
    assert manager.should_extract(5) is False
    assert manager.should_extract(EXTRACTION_EVERY_N_MESSAGES + 5) is True


def test_index_truncation_limit(tmp_path, monkeypatch):
    """索引超过 200 行时截断并附加警告。"""
    manager = make_manager(tmp_path, monkeypatch)
    project_dir = tmp_path / ".mewcode" / "memory"
    from mewcode.memory.auto_memory import MemoryEntry

    for i in range(210):
        entry = MemoryEntry(name=f"mem-{i}", description=f"条目 {i}", type="project", content=f"内容 {i}")
        manager._write_entry(project_dir, entry)
    manager._rebuild_index(project_dir)
    index = manager.load_index()
    assert "⚠️" in index  # 截断警告
    assert index.count("条目 ") < 210


def test_view_lists_user_role_and_memories(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    manager.remember("记住我是 Python 开发者")
    view = manager.view()
    assert "Python 开发者" in view
    assert "用户画像" in view


def test_clear_removes_all(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch)
    manager.remember("记住我是 Go 工程师")
    manager.remember("记住项目用 PostgreSQL")
    removed = manager.clear()
    assert removed >= 2
    assert manager.load_index() == ""

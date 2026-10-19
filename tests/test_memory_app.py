"""第 9 章：App 集成（指令注入、显式记忆、/session、/memory、/resume、时间跨度提示）。"""

import io
from datetime import datetime, timedelta, timezone

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta
from mewcode.memory.auto_memory import MemoryManager
from mewcode.session import Session
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal

from conftest import ScriptedClient, text_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """把 Path.home 重定向到临时目录，避免测试写入真实用户记忆目录。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")


class RecordingClient:
    """记录每次请求的 messages，方便断言 system prompt 是否注入指令/记忆。"""

    def __init__(self, script=None):
        self.script = script or []
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.script and self.calls <= len(self.script):
            entry = self.script[self.calls - 1]
            if isinstance(entry, Exception):
                raise entry
            for event in entry:
                yield event
        else:
            yield TextDelta("回答")
            yield StreamEnd("stop")


def make_app(tmp_path, client=None, *, monkeypatch=None, memory=None, storage=None, **kwargs):
    output = io.StringIO()
    storage = storage or SessionStorage(tmp_path)
    if monkeypatch is not None:
        monkeypatch.setattr("mewcode.memory.auto_memory.Path.home", lambda: tmp_path / "home")
    from conftest import make_registry
    app = App(
        PROVIDER,
        storage=storage,
        client=client or RecordingClient(),
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=make_registry(tmp_path),
        workdir=str(tmp_path),
        memory=memory,
        **kwargs,
    )
    return app, output


@pytest.mark.asyncio
async def test_instructions_injected_into_system_prompt(tmp_path):
    (tmp_path / "MEWCODE.md").write_text("项目用 Go 1.22 开发", encoding="utf-8")
    app, _ = make_app(tmp_path, RecordingClient([text_turn("好的")]))
    await app._ask("这个项目用什么技术栈？")
    system = app.client.requests[0][0]
    assert system["role"] == "system"
    assert "项目用 Go 1.22 开发" in system["content"]


@pytest.mark.asyncio
async def test_memory_index_injected_into_system_prompt(tmp_path, monkeypatch):
    memory = MemoryManager(tmp_path)
    memory.remember("记住我是 Go 工程师")
    app, _ = make_app(tmp_path, RecordingClient([text_turn("好的")]), memory=memory)
    await app._ask("你好")
    system = app.client.requests[0][0]
    assert "Go 工程师" in system["content"]


@pytest.mark.asyncio
async def test_remember_prompt_writes_memory_without_agent(tmp_path, monkeypatch):
    app, output = make_app(tmp_path)
    handled = app._remember_prompt("记住我是 Go 工程师")
    assert handled is True
    assert app.client.calls == 0  # 没有进入 Agent 对话
    assert "已记住" in output.getvalue()
    assert "Go 工程师" in app.memory.load_index()


@pytest.mark.asyncio
async def test_remember_prompt_not_matched(tmp_path):
    app, _ = make_app(tmp_path)
    assert app._remember_prompt("帮我写个函数") is False


@pytest.mark.asyncio
async def test_session_command_list_and_delete(tmp_path):
    storage = SessionStorage(tmp_path)
    s1 = Session(model="demo")
    s1.add_message("user", "第一个会话")
    storage.save(s1)
    s2 = Session(model="demo")
    s2.add_message("user", "第二个会话")
    storage.save(s2)

    app, output = make_app(tmp_path, storage=storage)
    app._handle_session_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/session"))
    text = output.getvalue()
    assert s1.id in text
    assert s2.id in text

    app._handle_session_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command(f"/session delete {s1.id}"))
    assert s1.id not in app.storage.list_sessions()[0].id


@pytest.mark.asyncio
async def test_memory_command_show_clear(tmp_path, monkeypatch):
    app, output = make_app(tmp_path)
    app.memory.remember("记住我是 Go 工程师")
    app._handle_memory_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/memory"))
    assert "Go 工程师" in output.getvalue()

    app._handle_memory_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/memory clear"))
    assert app.memory.load_index() == ""


@pytest.mark.asyncio
async def test_resume_command_switches_session(tmp_path):
    storage = SessionStorage(tmp_path)
    s1 = Session(model="demo")
    s1.add_message("user", "第一个会话")
    storage.save(s1)
    s2 = Session(model="demo")
    s2.add_message("user", "第二个会话")
    storage.save(s2)

    answers = iter(["1"])
    output = io.StringIO()
    app = App(
        PROVIDER,
        storage=storage,
        client=RecordingClient(),
        terminal=Terminal(input_fn=lambda _: next(answers), output=output),
        registry=__import__("conftest", fromlist=["make_registry"]).make_registry(tmp_path),
        workdir=str(tmp_path),
    )
    await app._handle_resume_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/resume"))
    assert "已恢复" in output.getvalue()
    assert app.session.id == storage.list_sessions()[0].id


@pytest.mark.asyncio
async def test_resume_with_no_sessions(tmp_path):
    app, output = make_app(tmp_path)
    await app._handle_resume_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/resume"))
    assert "暂无历史会话" in output.getvalue()


@pytest.mark.asyncio
async def test_use_command_time_gap_notice(tmp_path):
    """恢复一个 30 小时前活跃的会话：插入时间跨度提醒。"""
    storage = SessionStorage(tmp_path)
    session = Session(id="old", model="demo")
    session.add_message("user", "旧对话")
    storage.save(session)
    # 把 meta 的 last_active 改成 30 小时前
    meta = storage.load_meta("old")
    meta.last_active = datetime.now(timezone.utc) - timedelta(hours=30)
    meta.save(storage.directory / "old.meta")

    app, output = make_app(tmp_path, storage=storage)
    app._handle_command(__import__("mewcode.commands", fromlist=["parse_command"]).parse_command("/use old"))
    assert "时间跨度提醒" in output.getvalue()
    assert app.session.messages[0].content.startswith("[系统提示]")


@pytest.mark.asyncio
async def test_auto_memory_extraction_scheduled(tmp_path, monkeypatch):
    """达到提取频率阈值后，对话结束触发后台提取。"""
    import asyncio

    from mewcode.memory.auto_memory import EXTRACTION_EVERY_N_MESSAGES

    memory = MemoryManager(tmp_path)
    # 用 ScriptedClient 作为对话客户端；记忆提取用独立客户端
    client = RecordingClient([text_turn("回答")])
    app, _ = make_app(tmp_path, client, memory=memory, auto_memory=True, memory_client=ScriptedClient([]))
    # 塞够消息数触发阈值
    for _ in range(EXTRACTION_EVERY_N_MESSAGES):
        app.session.add_message("user", "x")
    await app._ask("触发提取")
    # 后台任务已调度：等它跑完
    if app._memory_tasks:
        await asyncio.gather(*app._memory_tasks, return_exceptions=True)
    assert memory._last_extraction_msg_count == len(app.session.messages)


@pytest.mark.asyncio
async def test_auto_memory_disabled_skips(tmp_path, monkeypatch):
    memory = MemoryManager(tmp_path)
    app, _ = make_app(tmp_path, RecordingClient([text_turn("回答")]), memory=memory, auto_memory=False)
    await app._ask("你好")
    assert memory._last_extraction_msg_count == 0

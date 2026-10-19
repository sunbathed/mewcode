"""第六章：App 层权限集成核心测试（/permissions 命令、HITL 确认、模式放行）。"""

import io

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.permission import PermissionMode
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal

from conftest import ScriptedClient, make_registry, text_turn, tools_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client, *, answers=None, permission_mode="bypassPermissions", **kwargs):
    output = io.StringIO()
    if answers is None:
        answers = iter(["/exit"])
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client,
        terminal=Terminal(input_fn=lambda _: next(answers), output=output),
        registry=make_registry(tmp_path),
        workdir=str(tmp_path),
        permission_mode=permission_mode,
        **kwargs,
    )
    return app, output


class WriteClient:
    """第一轮 WriteFile，第二轮最终回答。"""

    def __init__(self):
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_1", name="WriteFile")
            yield ToolUseDelta(index=0, args_fragment='{"path":"hello.txt","content":"1"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("已写入")
            yield StreamEnd("stop")


class BashClient:
    """第一轮 Bash，第二轮最终回答。"""

    def __init__(self):
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_1", name="Bash")
            yield ToolUseDelta(index=0, args_fragment='{"command":"echo hi"}')
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("命令执行完")
            yield StreamEnd("stop")


# ---------------------------------------------------------------------------
# /permissions 命令
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissions_command_shows_current_mode(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]), permission_mode="default")
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/permissions"))
    text = output.getvalue()
    assert "default" in text
    assert "本地规则" in text


@pytest.mark.asyncio
async def test_permissions_command_switches_mode(tmp_path):
    app, output = make_app(tmp_path, ScriptedClient([]), permission_mode="default")
    import mewcode.commands as commands

    app._handle_command(commands.parse_command("/permissions acceptEdits"))
    assert app.permission.mode == PermissionMode.ACCEPT_EDITS
    assert "acceptEdits" in output.getvalue()


# ---------------------------------------------------------------------------
# default 模式：写操作/Bash 需确认
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_mode_write_prompts_and_allows(tmp_path):
    """default 模式 WriteFile 弹确认，用户输入 y 放行。"""
    answers = iter(["y", "/exit"])
    app, output = make_app(tmp_path, WriteClient(), answers=answers, permission_mode="default")
    await app._ask("写个文件")
    text = output.getvalue()
    assert "[WriteFile]" in text
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_default_mode_write_prompt_deny_no_write(tmp_path):
    """default 模式 WriteFile 用户选 n 拒绝，文件不写入。"""
    answers = iter(["n", "/exit"])
    app, output = make_app(tmp_path, WriteClient(), answers=answers, permission_mode="default")
    await app._ask("写个文件")
    assert "[WriteFile]" in output.getvalue()
    assert not (tmp_path / "hello.txt").exists()
    assert "✗" in output.getvalue()


@pytest.mark.asyncio
async def test_default_mode_always_allow_writes_rule(tmp_path):
    """default 模式用户选 a（始终允许）：自动追加 allow 规则。"""
    answers = iter(["a", "/exit"])
    app, output = make_app(tmp_path, WriteClient(), answers=answers, permission_mode="default")
    await app._ask("写个文件")
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "1"
    local = tmp_path / ".mewcode" / "permissions.local.yaml"
    assert local.exists()
    assert "WriteFile(hello.txt)" in local.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_default_mode_bash_prompts(tmp_path):
    """default 模式 Bash 弹确认，用户用数字 1 放行。"""
    answers = iter(["1", "/exit"])
    app, output = make_app(tmp_path, BashClient(), answers=answers, permission_mode="default")
    await app._ask("跑个命令")
    text = output.getvalue()
    assert "[Bash]" in text
    assert "命令执行完" in text


# ---------------------------------------------------------------------------
# 只读工具自动放行 / 黑名单硬拦截
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_mode_read_no_prompt(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [
        tools_turn([("c1", "ReadFile", {"path": "a.txt"})]),
        text_turn("读到了"),
    ]
    app, output = make_app(tmp_path, ScriptedClient(script), permission_mode="default")
    await app._ask("读一下")
    text = output.getvalue()
    assert "✗" not in text
    assert "读到了" in text


@pytest.mark.asyncio
async def test_dangerous_command_denied_no_prompt(tmp_path):
    script = [
        tools_turn([("c1", "Bash", {"command": "rm -rf /"})]),
        text_turn("已停止"),
    ]
    app, output = make_app(tmp_path, ScriptedClient(script), permission_mode="bypassPermissions")
    await app._ask("清理")
    text = output.getvalue()
    assert "允许执行" not in text  # 黑名单硬拦截，不弹窗
    assert "危险命令" in text or "✗" in text


# ---------------------------------------------------------------------------
# acceptEdits 模式：文件编辑自动放行，Bash 仍确认
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_edits_write_auto_allowed(tmp_path):
    answers = iter(["写文件吧", "y", "/exit"])
    app, output = make_app(tmp_path, WriteClient(), answers=answers, permission_mode="acceptEdits")
    await app._ask("写个文件")
    text = output.getvalue()
    assert "允许执行" not in text  # 写操作被 acceptEdits 自动放行，没有弹窗
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_accept_edits_bash_still_prompts(tmp_path):
    answers = iter(["y", "/exit"])
    app, output = make_app(tmp_path, BashClient(), answers=answers, permission_mode="acceptEdits")
    await app._ask("跑个命令")
    assert "[Bash]" in output.getvalue()
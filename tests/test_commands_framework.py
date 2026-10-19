"""第 10 章：命令框架单元测试（解析、注册、别名、冲突、补全、handler）。"""

import asyncio

import pytest

from mewcode.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    CommandType,
    build_command_context,
    command_usage,
    dispatch_command,
    parse_command,
    parse_command_parts,
)
from mewcode.commands.handlers import ALL_COMMANDS, register_all_commands


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def test_parse_command_parts_basic():
    assert parse_command_parts("/compact 保留数据库") == ("compact", "保留数据库", True)
    assert parse_command_parts("/help") == ("help", "", True)
    assert parse_command_parts("/") == ("", "", True)
    assert parse_command_parts("普通输入") == ("", "", False)
    assert parse_command_parts("/Use abc") == ("use", "abc", True)  # 大小写不敏感


def test_parse_command_compat_api():
    cmd = parse_command("/use abc")
    assert cmd is not None
    assert cmd.name == "use"
    assert cmd.argument == "abc"
    assert parse_command("hello") is None
    assert parse_command("/exit").name == "exit"


# ---------------------------------------------------------------------------
# 注册与查找
# ---------------------------------------------------------------------------


def test_default_registry_has_ten_core_commands():
    registry = CommandRegistry()
    register_all_commands(registry)
    names = {cmd.name for cmd in registry.list_commands()}
    assert names == {
        "help", "status", "compact", "clear",
        "plan", "do", "session", "memory", "permission", "review",
    }


def test_find_by_name_and_alias():
    registry = CommandRegistry()
    register_all_commands(registry)
    assert registry.find("compact").name == "compact"
    assert registry.find("c").name == "compact"     # 别名
    assert registry.find("h").name == "help"        # 别名
    assert registry.find("?").name == "help"        # 别名
    assert registry.find("nope") is None


def test_register_conflict_name():
    registry = CommandRegistry()
    register_all_commands(registry)
    with pytest.raises(ValueError, match="conflicts"):
        registry.register_sync(
            Command(name="compact", description="重复", type=CommandType.LOCAL, handler=lambda ctx: None)
        )


def test_register_conflict_alias():
    registry = CommandRegistry()
    registry.register_sync(
        Command(name="a", description="a", type=CommandType.LOCAL, handler=lambda ctx: None, aliases=["x"])
    )
    # 别名 x 已被占：别名不能与另一个命令的主名字冲突
    with pytest.raises(ValueError, match="conflicts"):
        registry.register_sync(
            Command(name="x", description="b", type=CommandType.LOCAL, handler=lambda ctx: None)
        )
    # 双向：别名也不能与其他命令的别名冲突
    with pytest.raises(ValueError, match="conflicts"):
        registry.register_sync(
            Command(name="c", description="c", type=CommandType.LOCAL, handler=lambda ctx: None, aliases=["x"])
        )


def test_async_register_with_lock():
    async def run():
        registry = CommandRegistry()
        await registry.register(
            Command(name="tmp", description="t", type=CommandType.LOCAL, handler=lambda ctx: None)
        )
        return registry

    registry = asyncio.run(run())
    assert registry.find("tmp") is not None


def test_hidden_commands_excluded_from_list():
    registry = CommandRegistry()
    registry.register_sync(
        Command(name="debug", description="调试", type=CommandType.LOCAL, handler=lambda ctx: None, hidden=True)
    )
    assert registry.find("debug") is not None  # 可直接输入
    assert "debug" not in [c.name for c in registry.list_commands()]  # 不在 /help 列表


# ---------------------------------------------------------------------------
# 补全
# ---------------------------------------------------------------------------


def test_complete_prefix_matches_name_and_alias():
    registry = CommandRegistry()
    register_all_commands(registry)
    assert registry.complete("/com") == ["/compact"]
    assert registry.complete("/s") == ["/s", "/session", "/status"]
    assert registry.complete("com") == ["/compact"]  # 无斜杠前缀也可


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------


class FakeUI:
    def __init__(self):
        self.messages = []
        self.sent = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def send_user_message(self, text: str) -> None:
        self.sent.append(text)

    def set_plan_mode(self, enabled: bool) -> None:
        pass

    def get_token_count(self) -> tuple[int, int]:
        return (1000, 200_000)

    def refresh_status(self) -> None:
        pass


@pytest.mark.asyncio
async def test_dispatch_unknown_command_guides_to_help():
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = FakeUI()
    ctx = build_command_context(registry, "", ui=ui, config={"registry": registry})
    found = await dispatch_command(ctx, "not-a-command", registry)
    assert found is False
    assert "未知命令" in ui.messages[0]
    assert "/help" in ui.messages[0]


@pytest.mark.asyncio
async def test_dispatch_help_lists_commands():
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = FakeUI()
    ctx = build_command_context(registry, "", ui=ui, config={"registry": registry})
    found = await dispatch_command(ctx, "help", registry)
    assert found is True
    assert "可用命令" in ui.messages[0]
    assert "/compact" in ui.messages[0]


@pytest.mark.asyncio
async def test_dispatch_review_sends_prompt():
    """PROMPT 类型：/review 把审查 prompt 发给 Agent。"""
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = FakeUI()
    ctx = build_command_context(registry, "注意并发安全", ui=ui, config={"registry": registry})
    found = await dispatch_command(ctx, "review", registry)
    assert found is True
    assert len(ui.sent) == 1
    assert "审查当前 git diff" in ui.sent[0]
    assert "注意并发安全" in ui.sent[0]


@pytest.mark.asyncio
async def test_dispatch_help_with_arg_shows_single_command():
    registry = CommandRegistry()
    register_all_commands(registry)
    ui = FakeUI()
    ctx = build_command_context(registry, "session", ui=ui, config={"registry": registry})
    await dispatch_command(ctx, "help", registry)
    assert any("会话管理" in m for m in ui.messages)


# ---------------------------------------------------------------------------
# 内置命令元数据
# ---------------------------------------------------------------------------


def test_core_command_metadata():
    registry = CommandRegistry()
    register_all_commands(registry)
    compact = registry.find("compact")
    assert compact.type == CommandType.LOCAL
    assert "c" in compact.aliases
    assert compact.usage.startswith("/compact")
    review = registry.find("review")
    assert review.type == CommandType.PROMPT
    plan = registry.find("plan")
    assert plan.type == CommandType.LOCAL_UI
    assert "p" in plan.aliases
    status = registry.find("status")
    assert "s" in status.aliases


def test_command_usage_compat():
    assert "会话ID" in command_usage(parse_command("/use"))
    assert "不需要参数" in command_usage(parse_command("/init"))
    assert "未知命令" in command_usage(parse_command("/what"))


def test_all_commands_registered_in_known():
    from mewcode.commands import KNOWN_COMMANDS

    assert "compact" in KNOWN_COMMANDS
    assert "exit" in KNOWN_COMMANDS  # 兼容旧命令
    assert "permissions" in KNOWN_COMMANDS  # 别名

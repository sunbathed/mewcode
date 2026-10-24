"""第 11 章：Skill 系统集成测试（SkillHost / App 集成 / LoadSkill / 意图识别）。"""

import io
import json

import pytest

from mewcode.app import App
from mewcode.commands import parse_command
from mewcode.config import ProviderConfig
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.skills import SkillHost
from mewcode.skills.host import SkillDefinitionError, SYSTEM_TOOL_NAMES
from mewcode.skills.tools import LoadSkillTool, ScriptTool
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools import ToolRegistry

from conftest import ScriptedClient, text_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client=None):
    output = io.StringIO()
    app = App(
        PROVIDER,
        storage=SessionStorage(tmp_path),
        client=client or ScriptedClient([]),
        terminal=Terminal(input_fn=lambda _: "/exit", output=output),
        registry=ToolRegistry(workdir=str(tmp_path)),
        workdir=str(tmp_path),
        permission_mode="bypassPermissions",
    )
    return app, output


# ---------------------------------------------------------------------------
# SkillHost 集成
# ---------------------------------------------------------------------------


def test_load_skill_tool_registered(tmp_path):
    app, _ = make_app(tmp_path)
    assert "LoadSkill" in app.registry.names()
    assert app.skill_host.has_tool("LoadSkill") is False or app.skill_host.has_tool("ReadFile")


def test_skill_auto_registered_as_command(tmp_path):
    app, _ = make_app(tmp_path)
    commit = app.commands.find("commit")
    assert commit is not None
    assert "[skill]" in commit.description
    test_cmd = app.commands.find("test")
    assert test_cmd is not None
    assert "[skill]" in test_cmd.description


def test_skill_command_list(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/skill"))
    text = output.getvalue()
    assert "commit" in text
    assert "内置" in text
    assert "review" in text
    assert "test" in text


def test_skill_command_info(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/skill info commit"))
    text = output.getvalue()
    assert "名称：commit" in text
    assert "模式：inline" in text
    assert "允许工具" in text


def test_skill_command_reload(tmp_path):
    app, output = make_app(tmp_path)
    app._handle_command(parse_command("/skill reload"))
    assert "已重新扫描" in output.getvalue()


# ---------------------------------------------------------------------------
# allowedTools 过滤
# ---------------------------------------------------------------------------


def test_filter_registry_restricts_tools(tmp_path):
    app, _ = make_app(tmp_path)
    filtered = app.skill_host.filter_registry(("ReadFile", "Grep"))
    assert filtered is not None
    names = set(filtered.names())
    assert "ReadFile" in names and "Grep" in names
    assert "Bash" not in names
    assert "LoadSkill" in names  # 系统工具豁免


def test_filter_registry_empty_returns_none(tmp_path):
    app, _ = make_app(tmp_path)
    assert app.skill_host.filter_registry(()) is None


def test_filter_registry_fail_fast_missing_tool(tmp_path):
    app, _ = make_app(tmp_path)
    with pytest.raises(SkillDefinitionError, match="不存在的工具"):
        app.skill_host.filter_registry(("ReadFile", "NoSuchTool"))


def test_system_tool_names_contains_load_skill():
    assert "LoadSkill" in SYSTEM_TOOL_NAMES


# ---------------------------------------------------------------------------
# LoadSkill 激活
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_writes_env_context(tmp_path):
    app, _ = make_app(tmp_path)
    result = app.skill_host.activate("commit")
    assert "已激活" in result
    assert "commit" in app.skill_host.state.names()
    # SOP 已钉到 env context
    assert any("git commit" in sop for sop in app.skill_host.state.sop_texts())


@pytest.mark.asyncio
async def test_activate_unknown_skill(tmp_path):
    app, _ = make_app(tmp_path)
    result = app.skill_host.activate("no-such")
    assert "错误" in result
    assert app.skill_host.state.names() == []


@pytest.mark.asyncio
async def test_activate_bad_allowed_tools(tmp_path):
    """Skill 声明了不存在的工具：fail-fast 报错不激活。"""
    app, _ = make_app(tmp_path)
    # 动态加一个坏 Skill
    bad_dir = tmp_path / ".mewcode" / "skills" / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\nname: broken\ndescription: 坏skill\nallowedTools:\n  - NotExistTool\n---\nbody",
        encoding="utf-8",
    )
    app.skill_host.reload()
    result = app.skill_host.activate("broken")
    assert "不存在的工具" in result
    assert app.skill_host.state.names() == []


# ---------------------------------------------------------------------------
# clear 联动清空
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_clears_active_skills(tmp_path):
    app, _ = make_app(tmp_path)
    app.skill_host.activate("commit")
    assert app.skill_host.state.names() == ["commit"]
    app._handle_command(parse_command("/clear"))
    assert app.skill_host.state.names() == []


@pytest.mark.asyncio
async def test_new_clears_active_skills(tmp_path):
    app, _ = make_app(tmp_path)
    app.skill_host.activate("commit")
    app._handle_command(parse_command("/new"))
    assert app.skill_host.state.names() == []


# ---------------------------------------------------------------------------
# 意图识别（两阶段加载）
# ---------------------------------------------------------------------------


class IntentClient:
    """第一轮调 LoadSkill，第二轮纯文本。"""

    def __init__(self, skill_name="commit"):
        self.requests = []
        self.calls = 0
        self.skill_name = skill_name

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_ls", name="LoadSkill")
            raw = json.dumps({"name": self.skill_name})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("按 SOP 完成了任务。")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_skill_catalog_in_system_prompt(tmp_path):
    client = IntentClient()
    app, _ = make_app(tmp_path, client)
    await app._ask("帮我提交一下代码")
    system = client.requests[0][0]
    assert system["role"] == "system"
    assert "commit" in system["content"]
    assert "LoadSkill" in system["content"]  # 提示模型用 LoadSkill


@pytest.mark.asyncio
async def test_load_skill_activates_env_context(tmp_path):
    client = IntentClient()
    app, _ = make_app(tmp_path, client)
    await app._ask("帮我提交一下代码")

    assert app.skill_host.state.names() == ["commit"]
    # 第二轮请求的 env context 含 SOP
    req2 = client.requests[1]
    assert any("git commit" in str(m.get("content", "")) for m in req2)


@pytest.mark.asyncio
async def test_review_command_uses_skill(tmp_path):
    """/review 现在走 Skill（升级），不再用硬编码 prompt。"""
    client = ScriptedClient([])
    app, output = make_app(tmp_path, client)
    # run_async 里 /review 走 skill_host.execute_skill，"review" 是 fork 模式
    from mewcode.skills.executor import SkillResult

    result = await app.skill_host.execute_skill("review", "注意安全")
    assert result.mode == "fork"
    assert app.skill_host.loader.get("review").mode == "fork"


# ---------------------------------------------------------------------------
# LoadSkillTool 独立单元
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skill_tool_run(tmp_path):
    app, _ = make_app(tmp_path)
    tool = LoadSkillTool(app.skill_host.loader, app.skill_host.state, tool_host=app.skill_host)
    result = await tool.run({"name": "commit"})
    assert result.success
    assert "已激活" in result.output
    assert app.skill_host.state.is_active("commit")


@pytest.mark.asyncio
async def test_script_tool_executes(tmp_path):
    """ScriptTool 子进程执行 references/ 里的脚本。"""
    refs = tmp_path / "refs"
    refs.mkdir()
    script = refs / "echo_args.py"
    script.write_text(
        "import json, sys\nargs = json.loads(sys.argv[1])\nprint('got', args.get('x'))",
        encoding="utf-8",
    )
    tool = ScriptTool("my_tool", "测试工具", {"type": "object", "properties": {}}, script)
    result = await tool.run({"x": "hello"})
    assert result.success
    assert "got hello" in result.output
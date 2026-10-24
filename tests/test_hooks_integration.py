"""第 12 章：Hook 系统集成测试（App / Agent Loop / 配置加载 / 验收场景）。

验收场景（对应实战文档）：
  配置 block-json-write Hook（pre_tool_use + reject + 正则 .json 后缀），
  用户要求"创建 config.json"时 WriteFile 被拦截，工具不真正执行，
  Agent 收到拒绝原因后调整策略。
"""

import io
import json

import pytest

from mewcode.app import App
from mewcode.config import ProviderConfig
from mewcode.hooks import (
    Action,
    ConditionGroup,
    Hook,
    HookContext,
    HookEngine,
)
from mewcode.hooks.models import LifecycleEvent as LE
from mewcode.llm_client import StreamEnd, TextDelta, ToolUseDelta, ToolUseStart
from mewcode.storage import SessionStorage
from mewcode.terminal import Terminal
from mewcode.tools import ToolRegistry

from conftest import ScriptedClient

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def make_app(tmp_path, client=None, hooks=None):
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
    if hooks is not None:
        app.hooks = hooks
    return app, output


def write_json_hook() -> Hook:
    """文档验收场景的 Hook：拦截写 *.json 文件。"""
    return Hook(
        id="block-json-write",
        event="pre_tool_use",
        action=Action(type="command", command='echo "禁止直接写入 JSON 文件，请使用专用工具"'),
        reject=True,
        condition=ConditionGroup.parse('tool == "WriteFile" && args.file_path =~ /\\.json$/'),
    )


# ---------------------------------------------------------------------------
# 验收场景：WriteFile 写 JSON 被 Hook 拦截
# ---------------------------------------------------------------------------


class JsonWriteClient:
    """第一轮请求 WriteFile 写 config.json；第二轮文本收尾。"""

    def __init__(self):
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_write", name="WriteFile")
            raw = json.dumps({"file_path": "config.json", "content": "{}"})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("JSON 文件需要专用工具处理，我不再直接写入。")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_hook_rejects_json_write(tmp_path):
    """验收：写 config.json 的 WriteFile 调用被 reject Hook 拦截。

    工具不真正执行（config.json 未创建），模型收到拒绝原因后调整策略。
    """
    client = JsonWriteClient()
    engine = HookEngine([write_json_hook()])
    app, _ = make_app(tmp_path, client, hooks=engine)
    await app._ask("帮我创建 config.json，内容是 {}")

    # 第二轮请求（模型调整策略后的文本）历史里能看到拒绝原因
    assert client.calls == 2
    # config.json 不应被创建（工具被拦截，没有真正执行）
    assert not (tmp_path / "config.json").exists()
    # 模型收到了拒绝原因：tool_result 回填在会话里
    all_content = " ".join(str(m.get("content", "")) for m in client.requests[1])
    assert "禁止直接写入 JSON 文件" in all_content


@pytest.mark.asyncio
async def test_hook_reject_does_not_block_other_tools(tmp_path):
    """条件过滤：非 JSON 写入不受拦截。"""
    engine = HookEngine([write_json_hook()])
    ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile", tool_args={"path": "ok.txt"})
    assert await engine.run_pre_tool_hooks(ctx) is None
    ctx2 = HookContext(event_name="pre_tool_use", tool_name="WriteFile", tool_args={"path": "data.json"})
    rejection = await engine.run_pre_tool_hooks(ctx2)
    assert rejection is not None and rejection.hook_id == "block-json-write"


# ---------------------------------------------------------------------------
# 拦截反馈循环：Agent 调整策略
# ---------------------------------------------------------------------------


class AdaptClient:
    """第一轮尝试 WriteFile 写 json，被拦后第二轮改用 ReadFile。"""

    def __init__(self):
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call1", name="WriteFile")
            raw = json.dumps({"path": "config.json", "content": "{}"})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        elif self.calls == 2:
            # 看到拒绝原因后，Agent 不再硬写 JSON，改用安全方式
            yield ToolUseStart(index=0, id="call2", name="ReadFile")
            raw = json.dumps({"path": "README.md"})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("已改用安全方式处理。")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_rejection_feedback_loop(tmp_path):
    """拦截 → Agent 收到原因 → 调整策略：第二轮不再尝试写 JSON。"""
    client = AdaptClient()
    engine = HookEngine([write_json_hook()])
    app, _ = make_app(tmp_path, client, hooks=engine)
    await app._ask("创建 config.json")

    assert client.calls == 3  # 拦截轮 + 调整轮 + 收尾轮
    assert not (tmp_path / "config.json").exists()
    # 第二轮的请求历史里带上了拒绝原因
    history_text = " ".join(str(m.get("content", "")) for m in client.requests[1])
    assert "禁止直接写入 JSON 文件" in history_text


# ---------------------------------------------------------------------------
# 事件联动：prompt 注入 / turn 级事件
# ---------------------------------------------------------------------------


class TurnPromptClient:
    """首轮调 WriteFile（放行），第二轮文本收尾。"""

    def __init__(self):
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_write", name="WriteFile")
            raw = json.dumps({"path": "ok.txt", "content": "hi"})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("完成。")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_hook_prompt_injected_into_request(tmp_path):
    """session_start + turn_start 的 prompt 执行器消息注入到请求首部。"""
    engine = HookEngine(
        [
            Hook(
                id="ctx",
                event="session_start",
                action=Action(type="prompt", message="项目技术栈：Python 3.12 + FastAPI"),
                once=True,
            ),
            Hook(
                id="remind",
                event="turn_start",
                action=Action(type="prompt", message="请先阅读 ARCHITECTURE.md"),
            ),
        ]
    )
    client = TurnPromptClient()
    app, _ = make_app(tmp_path, client, hooks=engine)

    # 模拟 startup + session_start（run_async 里才触发）
    await engine.run_hooks(LE.STARTUP, HookContext(event_name=LE.STARTUP))
    await engine.run_hooks(LE.SESSION_START, HookContext(event_name=LE.SESSION_START))

    await app._ask("写个文件")
    # 两条 prompt 各占一条 system-reminder 消息，检查首轮全部消息
    first_turn_text = " ".join(str(m.get("content", "")) for m in client.requests[0])
    assert "项目技术栈：Python 3.12 + FastAPI" in first_turn_text
    assert "请先阅读 ARCHITECTURE.md" in first_turn_text


@pytest.mark.asyncio
async def test_turn_events_fire_around_ask(tmp_path):
    """turn_start / turn_end 事件在 _ask 前后触发（用追加脚本记录顺序）。"""
    marker = tmp_path / "turn_order.txt"
    script = tmp_path / "append.py"
    script.write_text(
        "import sys\nwith open(sys.argv[1], 'a', encoding='utf-8') as f:\n    f.write(sys.argv[2])\n",
        encoding="utf-8",
    )

    def make_cmd_hook(hook_id, event, text):
        return Hook(
            id=hook_id,
            event=event,
            action=Action(
                type="command",
                command=f'python {script} {marker} {text}',
            ),
        )

    # 标记避免 shell 特殊字符（cmd 里 | 是管道符）
    engine = HookEngine(
        [
            make_cmd_hook("start", "turn_start", "S"),
            make_cmd_hook("end", "turn_end", "E"),
        ]
    )
    app, _ = make_app(tmp_path, ScriptedClient([]), hooks=engine)
    await app._ask("你好")
    assert marker.read_text(encoding="utf-8") == "SE"


# ---------------------------------------------------------------------------
# post_tool_use 联动
# ---------------------------------------------------------------------------


class PostToolClient:
    """一轮 WriteFile（放行），二轮文本收尾。"""

    def __init__(self):
        self.requests = []
        self.calls = 0

    async def stream(self, messages):
        self.requests.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield ToolUseStart(index=0, id="call_write", name="WriteFile")
            raw = json.dumps({"path": "ok.txt", "content": "hi"})
            for i in range(0, len(raw), 2):
                yield ToolUseDelta(index=0, args_fragment=raw[i : i + 2])
            yield StreamEnd("tool_calls")
        else:
            yield TextDelta("完成。")
            yield StreamEnd("stop")


@pytest.mark.asyncio
async def test_post_tool_use_fires_with_file_path(tmp_path):
    """post_tool_use 事件触发并携带 file_path 上下文（$FILE_PATH 可替换）。"""
    marker = tmp_path / "post.txt"
    script = tmp_path / "write_marker.py"
    script.write_text(
        "import sys\nwith open(sys.argv[1], 'w', encoding='utf-8') as f:\n    f.write('got:' + sys.argv[2])\n",
        encoding="utf-8",
    )
    engine = HookEngine(
        [
            Hook(
                id="fmt",
                event="post_tool_use",
                action=Action(type="command", command=f"python {script} {marker} $FILE_PATH"),
            )
        ]
    )
    client = PostToolClient()
    app, _ = make_app(tmp_path, client, hooks=engine)
    await app._ask("写文件")
    assert marker.read_text(encoding="utf-8").startswith("got:")


# ---------------------------------------------------------------------------
# 配置加载接入 App
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_loads_hooks_from_config(tmp_path, monkeypatch):
    """App 从 .mewcode/config.yaml 自动加载 Hook 配置（无需显式注入）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    config_dir = tmp_path / ".mewcode"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "hooks:\n"
        "  - id: block-json-write\n"
        "    event: pre_tool_use\n"
        "    if: 'tool == \"WriteFile\" && args.file_path =~ /\\.json$/'\n"
        "    action:\n"
        "      type: command\n"
        "      command: 'echo \"禁止直接写入 JSON 文件，请使用专用工具\"'\n"
        "    reject: true\n",
        encoding="utf-8",
    )

    client = JsonWriteClient()
    app, output = make_app(tmp_path, client)
    # HookEngine 从配置文件加载（含验收 Hook）
    assert len(app.hooks.hooks) == 1
    assert app.hooks.hooks[0].id == "block-json-write"
    await app._ask("帮我创建 config.json，内容是 {}")

    assert not (tmp_path / "config.json").exists()
    all_content = " ".join(str(m.get("content", "")) for m in client.requests[1])
    assert "禁止直接写入 JSON 文件" in all_content


@pytest.mark.asyncio
async def test_app_bad_hook_config_falls_back(tmp_path, monkeypatch):
    """配置非法时 App 不崩溃，给出告警并退化为空 HookEngine。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    config_dir = tmp_path / ".mewcode"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "hooks:\n"
        "  - id: bad\n"
        "    event: no_such_event\n"
        "    action: {type: prompt, message: x}\n",
        encoding="utf-8",
    )
    app, output = make_app(tmp_path)
    assert app.hooks.hooks == []
    assert "Hook 配置加载失败" in output.getvalue()

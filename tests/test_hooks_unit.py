"""第 12 章：Hook 系统单元测试（数据结构 / 条件 / 执行器 / 加载器 / 引擎）。"""

import asyncio
import json

import pytest

from mewcode.hooks import (
    Action,
    Condition,
    ConditionGroup,
    ConditionParseError,
    Hook,
    HookConfigError,
    HookContext,
    HookEngine,
    LifecycleEvent,
    ToolRejectedError,
    execute_action,
    load_hooks,
)
from mewcode.hooks.executors import execute_command, execute_prompt


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def test_lifecycle_event_fifteen_values():
    """15 个生命周期事件常量，覆盖五层。"""
    values = [event.value for event in LifecycleEvent]
    assert len(values) == 15
    assert LifecycleEvent.SESSION_START == "session_start"
    assert LifecycleEvent.PRE_TOOL_USE == "pre_tool_use"
    assert LifecycleEvent.POST_TOOL_USE == "post_tool_use"
    assert LifecycleEvent.COMMAND_EXECUTE == "command_execute"


def test_hook_once_semantics():
    """once 标记：第一次触发后不再执行（运行时标记）。"""
    hook = Hook(id="h", event="session_start", action=Action(type="prompt", message="x"), once=True)
    assert hook.should_run() is True
    hook.mark_executed()
    assert hook.should_run() is False


def test_hook_non_once_always_runs():
    hook = Hook(id="h", event="turn_start", action=Action(type="prompt", message="x"))
    hook.mark_executed()
    assert hook.should_run() is True


def test_hook_context_get_field():
    ctx = HookContext(
        event_name="pre_tool_use",
        tool_name="WriteFile",
        tool_args={"path": "/tmp/a.json", "content": "{}"},
    )
    assert ctx.get_field("tool") == "WriteFile"
    assert ctx.get_field("event") == "pre_tool_use"
    assert ctx.get_field("args.path") == "/tmp/a.json"
    assert ctx.get_field("args.content") == "{}"
    assert ctx.get_field("args.missing") == ""


def test_hook_context_file_path_alias():
    """args.file_path 是配置文档常用写法，缺字段时回退取 args.path。"""
    ctx = HookContext(tool_name="WriteFile", tool_args={"path": "/tmp/a.json"})
    assert ctx.get_field("args.file_path") == "/tmp/a.json"
    # 反之亦然：显式 file_path 优先
    ctx2 = HookContext(tool_name="WriteFile", tool_args={"file_path": "/tmp/b.json"})
    assert ctx2.get_field("args.file_path") == "/tmp/b.json"


def test_hook_context_expand():
    ctx = HookContext(
        event_name="post_tool_use",
        tool_name="WriteFile",
        file_path="/tmp/a.py",
        message="hello",
        error="boom",
        tool_args={"path": "/tmp/a.py", "mode": "w"},
    )
    template = "$EVENT|$TOOL_NAME|$FILE_PATH|$MESSAGE|$ERROR|$TOOL_ARGS.path|$TOOL_ARGS.mode|$UNKNOWN"
    assert ctx.expand(template) == "post_tool_use|WriteFile|/tmp/a.py|hello|boom|/tmp/a.py|w|"


def test_tool_rejected_error_fields():
    err = ToolRejectedError(tool="WriteFile", reason="禁止写 JSON", hook_id="block-json")
    assert err.tool == "WriteFile"
    assert err.reason == "禁止写 JSON"
    assert err.hook_id == "block-json"


# ---------------------------------------------------------------------------
# 条件
# ---------------------------------------------------------------------------


def test_condition_equality_operators():
    ctx = HookContext(tool_name="Bash")
    assert Condition.parse('tool == "Bash"').evaluate(ctx)
    assert not Condition.parse('tool == "ReadFile"').evaluate(ctx)
    assert Condition.parse('tool != "ReadFile"').evaluate(ctx)
    assert not Condition.parse('tool != "Bash"').evaluate(ctx)


def test_condition_regex_operator():
    """=~ 正则匹配：支持 /pattern/ 写法。"""
    ctx = HookContext(tool_name="Bash", tool_args={"command": "rm -rf /tmp"})
    assert Condition.parse('args.command =~ /rm\\s+-rf/').evaluate(ctx)
    assert Condition.parse("args.command =~ rm").evaluate(ctx)
    assert not Condition.parse('args.command =~ "npm"').evaluate(ctx)


def test_condition_bad_regex_returns_false():
    """正则语法错误返回 False 而不是抛异常（防御性编程）。"""
    ctx = HookContext(tool_name="Bash", tool_args={"command": "x"})
    assert Condition.parse("args.command =~ /[unclosed/").evaluate(ctx) is False


def test_condition_glob_operator():
    """~= glob 匹配（fnmatch）。"""
    ctx = HookContext(tool_args={"path": "src/app.py"})
    assert Condition.parse('args.path ~= "*.py"').evaluate(ctx)
    assert not Condition.parse('args.path ~= "*.go"').evaluate(ctx)


def test_condition_group_and():
    ctx = HookContext(tool_name="WriteFile", tool_args={"path": "a.py"})
    group = ConditionGroup.parse('tool == "WriteFile" && args.path ~= "*.py"')
    assert group.logic == "and"
    assert group.evaluate(ctx)


def test_condition_group_or():
    ctx = HookContext(tool_name="Grep")
    group = ConditionGroup.parse('tool == "Bash" || tool == "Grep"')
    assert group.logic == "or"
    assert group.evaluate(ctx)


def test_condition_group_mixing_rejected():
    with pytest.raises(ConditionParseError, match="Cannot mix"):
        ConditionGroup.parse('tool == "Bash" && tool != "ReadFile" || tool == "Grep"')


def test_condition_parse_invalid():
    with pytest.raises(ConditionParseError):
        Condition.parse("justafield")
    with pytest.raises(ConditionParseError):
        Condition.parse("field ??? value")


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_command_success():
    ctx = HookContext(file_path="x.txt")
    action = Action(type="command", command="echo hello-$FILE_PATH")
    result = await execute_command(action, ctx)
    assert result.success
    assert "hello-x.txt" in result.output


@pytest.mark.asyncio
async def test_execute_command_failure():
    ctx = HookContext()
    action = Action(type="command", command="exit 3")
    result = await execute_command(action, ctx)
    assert not result.success


@pytest.mark.asyncio
async def test_execute_command_timeout():
    """超时 kill + wait：返回超时错误而不是挂死。"""
    ctx = HookContext()
    action = Action(type="command", command="sleep 10", timeout=1)
    result = await execute_command(action, ctx)
    assert not result.success
    assert "timed out" in result.output.lower()


@pytest.mark.asyncio
async def test_execute_prompt_expands_template():
    ctx = HookContext(file_path="a.py")
    action = Action(type="prompt", message="请检查 $FILE_PATH")
    result = await execute_prompt(action, ctx)
    assert result.success
    assert result.output == "请检查 a.py"


@pytest.mark.asyncio
async def test_execute_http_local_server():
    """http 执行器：对本地测试服务器发请求，2xx 视为成功。"""
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length).decode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    # 等待端口就绪（线程调度有延迟，轮询直到可连接）
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    try:
        ctx = HookContext(file_path="a.json")
        action = Action(type="http", url=f"http://127.0.0.1:{port}/hook", method="POST", body="file=$FILE_PATH")
        result = await execute_action(action, ctx)
        assert result.success
        assert "HTTP 200" in result.output
        assert received["body"] == "file=a.json"
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_execute_agent_stub():
    """agent 执行器：占位 stub，返回未实现提示但不报错。"""
    ctx = HookContext(file_path="a.py")
    action = Action(type="agent", prompt="请审查 $FILE_PATH")
    result = await execute_action(action, ctx)
    assert result.success
    assert "not yet implemented" in result.output


@pytest.mark.asyncio
async def test_execute_action_unknown_type():
    ctx = HookContext()
    action = Action(type="weird")
    result = await execute_action(action, ctx)
    assert not result.success
    assert "Unknown action type" in result.output


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def test_load_hooks_empty_when_no_config(tmp_path, monkeypatch):
    """没有配置文件时返回空列表（不报错）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    hooks = load_hooks(workdir=str(tmp_path / "nope"))
    assert hooks == []


def test_load_hooks_merges_three_files(tmp_path, monkeypatch):
    """三文件追加合并：用户级 + 项目级 + 本地级全部生效。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

    user_cfg = tmp_path / "home" / ".mewcode" / "config.yaml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text(
        "hooks:\n"
        "  - id: user-hook\n"
        "    event: session_start\n"
        "    action:\n"
        "      type: prompt\n"
        "      message: 用户级\n",
        encoding="utf-8",
    )
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: proj-hook\n"
        "    event: turn_start\n"
        "    action:\n"
        "      type: prompt\n"
        "      message: 项目级\n",
        encoding="utf-8",
    )
    local_cfg = tmp_path / ".mewcode" / "config.local.yaml"
    local_cfg.write_text(
        "hooks:\n"
        "  - id: local-hook\n"
        "    event: turn_end\n"
        "    action:\n"
        "      type: prompt\n"
        "      message: 本地级\n",
        encoding="utf-8",
    )

    hooks = load_hooks(workdir=str(tmp_path))
    assert [h.id for h in hooks] == ["user-hook", "proj-hook", "local-hook"]


def test_load_hooks_parse_condition_and_reject(tmp_path, monkeypatch):
    """加载含条件与 reject 标记的 Hook（文档验收场景的配置形态）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: block-json-write\n"
        "    event: pre_tool_use\n"
        "    if: 'tool == \"WriteFile\" && args.file_path =~ /\\.json$/'"
        "\n"
        "    action:\n"
        "      type: command\n"
        "      command: 'echo \"禁止直接写入 JSON 文件\"'\n"
        "    reject: true\n",
        encoding="utf-8",
    )
    hooks = load_hooks(workdir=str(tmp_path))
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook.reject is True
    assert hook.event == "pre_tool_use"
    assert hook.condition is not None
    assert hook.action.type == "command"


def test_load_hooks_unknown_event_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: no_such_event\n"
        "    action: {type: prompt, message: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="未知事件"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_unknown_action_type(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: turn_start\n"
        "    action: {type: weird}\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="未知 action"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_reject_not_on_pre_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: post_tool_use\n"
        "    action: {type: prompt, message: x}\n"
        "    reject: true\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="reject.*pre_tool_use"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_async_not_on_pre_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: pre_tool_use\n"
        "    action: {type: prompt, message: x}\n"
        "    async: true\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="async.*pre_tool_use"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_missing_required_fields(tmp_path, monkeypatch):
    """command 类型缺 command 字段、http 类型缺 url 字段都报错。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: turn_start\n"
        "    action: {type: command}\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="command.*缺少必填字段"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_missing_action(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: turn_start\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="action"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_missing_event(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    action: {type: prompt, message: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="event"):
        load_hooks(workdir=str(tmp_path))


def test_load_hooks_bad_condition(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    project_cfg = tmp_path / ".mewcode" / "config.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        "hooks:\n"
        "  - id: x\n"
        "    event: turn_start\n"
        "    if: 'a == \"1\" && b == \"2\" || c == \"3\"'\n"
        "    action: {type: prompt, message: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(HookConfigError, match="条件"):
        load_hooks(workdir=str(tmp_path))


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def _prompt_hook(hook_id: str, event: str, message: str, **kwargs) -> Hook:
    return Hook(id=hook_id, event=event, action=Action(type="prompt", message=message), **kwargs)


@pytest.mark.asyncio
async def test_engine_find_matching_hooks():
    engine = HookEngine(
        [
            _prompt_hook("a", "turn_start", "A"),
            _prompt_hook("b", "pre_tool_use", "B"),
            _prompt_hook("c", "turn_start", "C"),
        ]
    )
    ctx = HookContext(event_name="turn_start")
    matched = engine.find_matching_hooks("turn_start", ctx)
    assert [h.id for h in matched] == ["a", "c"]


@pytest.mark.asyncio
async def test_engine_run_hooks_collects_prompt_messages():
    engine = HookEngine([_prompt_hook("a", "turn_start", "提示 A")])
    await engine.run_hooks("turn_start", HookContext(event_name="turn_start"))
    assert engine.get_prompt_messages() == ["提示 A"]
    # 一次性消费：取走后清空
    assert engine.get_prompt_messages() == []


@pytest.mark.asyncio
async def test_engine_once_marked_executed():
    engine = HookEngine([_prompt_hook("a", "session_start", "A", once=True)])
    await engine.run_hooks("session_start", HookContext(event_name="session_start"))
    assert engine.get_prompt_messages() == ["A"]
    # 第二次触发被跳过（once 标记）
    await engine.run_hooks("session_start", HookContext(event_name="session_start"))
    assert engine.get_prompt_messages() == []


@pytest.mark.asyncio
async def test_engine_error_isolation():
    """Hook 执行出错只记日志，不中断主流程。"""
    engine = HookEngine(
        [
            Hook(id="bad", event="turn_start", action=Action(type="command", command="nonexistent_cmd_xyz")),
            _prompt_hook("good", "turn_start", "好的"),
        ]
    )
    await engine.run_hooks("turn_start", HookContext(event_name="turn_start"))
    # 坏命令执行失败被吞掉，后面的 Hook 照常执行
    assert engine.get_prompt_messages() == ["好的"]


@pytest.mark.asyncio
async def test_engine_async_hook_runs_in_background():
    """async 标记的 Hook 后台执行，主流程不等它。"""
    events: list[str] = []

    class SlowPromptHook(Hook):
        pass

    async def slow_exec(action, ctx):
        await asyncio.sleep(0.05)
        events.append("slow-done")
        from mewcode.hooks.executors import ActionResult

        return ActionResult(output="slow", success=True)

    import mewcode.hooks.executors as exec_mod

    original = exec_mod._EXECUTOR_MAP["prompt"]
    exec_mod._EXECUTOR_MAP["prompt"] = slow_exec
    try:
        engine = HookEngine(
            [Hook(id="slow", event="turn_start", action=Action(type="prompt", message="x"), async_exec=True)]
        )
        await engine.run_hooks("turn_start", HookContext(event_name="turn_start"))
        # 主流程不等后台任务
        assert events == []
        # 引擎持有任务句柄，跑完会触发回调
        await asyncio.sleep(0.15)
        assert events == ["slow-done"]
    finally:
        exec_mod._EXECUTOR_MAP["prompt"] = original


@pytest.mark.asyncio
async def test_engine_pre_tool_reject():
    """pre_tool_use + reject：返回 ToolRejectedError，reason 是 Hook 输出。"""
    ctx = HookContext(event_name="pre_tool_use", tool_name="WriteFile", tool_args={"path": "a.json"})
    hook = Hook(
        id="block-json",
        event="pre_tool_use",
        action=Action(type="command", command="echo 禁止写入 JSON"),
        reject=True,
    )
    engine = HookEngine([hook])
    err = await engine.run_pre_tool_hooks(ctx)
    assert isinstance(err, ToolRejectedError)
    assert err.tool == "WriteFile"
    assert "禁止写入 JSON" in err.reason
    assert err.hook_id == "block-json"


@pytest.mark.asyncio
async def test_engine_pre_tool_no_reject_returns_none():
    ctx = HookContext(event_name="pre_tool_use", tool_name="Bash", tool_args={"command": "ls"})
    engine = HookEngine([_prompt_hook("a", "pre_tool_use", "x")])
    err = await engine.run_pre_tool_hooks(ctx)
    assert err is None
    # prompt 消息同样被收集
    assert engine.get_prompt_messages() == ["x"]


@pytest.mark.asyncio
async def test_engine_pre_tool_condition_filters():
    """条件不匹配的 Hook 不触发。"""
    ctx = HookContext(event_name="pre_tool_use", tool_name="ReadFile", tool_args={"path": "a.json"})
    hook = Hook(
        id="block-json",
        event="pre_tool_use",
        action=Action(type="command", command="echo 禁止写入 JSON"),
        condition=ConditionGroup.parse('tool == "WriteFile"'),
        reject=True,
    )
    engine = HookEngine([hook])
    assert await engine.run_pre_tool_hooks(ctx) is None


@pytest.mark.asyncio
async def test_engine_reject_order_first_hook_wins():
    """多个匹配 Hook：先声明的先执行，第一个 reject 命中即返回，后续不跑。"""
    ctx = HookContext(event_name="pre_tool_use", tool_name="Bash", tool_args={"command": "rm -rf /"})
    engine = HookEngine(
        [
            Hook(
                id="first",
                event="pre_tool_use",
                action=Action(type="command", command="echo 第一个拒绝"),
                reject=True,
            ),
            Hook(
                id="second",
                event="pre_tool_use",
                action=Action(type="command", command="echo 第二个拒绝"),
                reject=True,
            ),
        ]
    )
    err = await engine.run_pre_tool_hooks(ctx)
    assert err.hook_id == "first"

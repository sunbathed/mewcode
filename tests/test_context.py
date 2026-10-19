"""第八章：上下文管理单元测试。

覆盖 Token 估算、第 1 层大结果存盘与决策冻结（含理论篇三轮场景）、
第 2 层全量摘要、PTL 重试与熔断，以及紧急压缩。
"""

import pytest

from mewcode.config import ProviderConfig
from mewcode.context import (
    ContentReplacementState,
    ContextConfig,
    ContextManager,
    CompactError,
    estimate_messages_tokens,
    estimate_tokens,
    is_prompt_too_long_error,
    parse_summary,
)
from mewcode.llm_client import LLMError, StreamEnd, TextDelta
from mewcode.session import Session, ToolResultBlock, ToolUseBlock
from conftest import ScriptedClient, make_registry, text_turn

PROVIDER = ProviderConfig("openai", "demo", "http://test/v1", "secret")


def summary_turn(text: str = "用户要求重构 auth 模块，修复了错误处理 bug，测试全部通过。") -> list:
    """构造一轮摘要客户端的两阶段输出（草稿 + 正式摘要）。"""
    return text_turn(f"<analysis>这是内部草稿，不会保留</analysis>\n<summary>{text}</summary>")


def build_rounds(session: Session, sizes: list[tuple[str, int]]) -> None:
    """往会话里追加若干轮「user + assistant(tool_use) + tool_result」消息。"""
    for index, (use_id, size) in enumerate(sizes):
        content = "计" * size  # 中文 1 字符 ≈ 1 token，便于精确控制规模
        session.add_message("user", f"任务{index}")
        session.add_message(
            "assistant",
            "好的",
            blocks=[ToolUseBlock(id=use_id, name="Bash", input={"command": "echo 1"})],
        )
        session.add_tool_result(use_id, content)


def make_manager(tmp_path, *, config=None, compact_client=None, state=None):
    return ContextManager(
        PROVIDER,
        workdir=str(tmp_path),
        config=config or ContextConfig(),
        state=state,
        compact_client=compact_client,
    )


def tool_result_blocks(session: Session) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for message in session.messages:
        for block in message.blocks or []:
            if isinstance(block, ToolResultBlock):
                blocks[block.tool_use_id] = block.content
    return blocks


# ---------------------------------------------------------------------------
# Token 估算
# ---------------------------------------------------------------------------


def test_estimate_tokens_mixed():
    # ASCII 4 字符 ≈ 1 token；中文 1 字符 ≈ 1 token
    assert estimate_tokens("hello") == 2  # 5 ASCII → 1 + 1
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_messages_tokens():
    messages = [
        {"role": "user", "content": "读一下文件"},
        {"role": "assistant", "content": "我来看看", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    total = estimate_messages_tokens(messages)
    assert total > 0
    assert estimate_messages_tokens([]) == 0


# ---------------------------------------------------------------------------
# 第 1 层：大结果存盘 + 决策冻结
# ---------------------------------------------------------------------------


def test_single_result_over_threshold_persists(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("big_1", 60_000)])  # 60K 字符 > 50K 阈值
    manager = make_manager(tmp_path)

    replaced = manager.apply_budget(session.messages)

    assert set(replaced) == {"big_1"}
    # 内容被替换为「预览 + 路径」模板
    content = tool_result_blocks(session)["big_1"]
    assert "<persisted-output>" in content
    assert "tool-results" in content
    assert "big_1" in content
    # 完整内容在磁盘上
    files = list((tmp_path / ".mewcode" / "session" / "tool-results").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "计" * 60_000


def test_replace_is_idempotent(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("big_1", 60_000)])
    manager = make_manager(tmp_path)
    manager.apply_budget(session.messages)
    first = tool_result_blocks(session)["big_1"]

    replaced = manager.apply_budget(session.messages)  # 每轮请求前都会重跑
    files = list((tmp_path / ".mewcode" / "session" / "tool-results").glob("*.txt"))
    assert replaced == {}  # 决策已冻结，不再重复写盘
    assert len(files) == 1  # 幂等：文件只写了一次
    assert tool_result_blocks(session)["big_1"] == first  # 预览字符串逐字节不变


def test_budget_decision_freeze_walkthrough(tmp_path):
    """理论篇三轮场景：X(8K)、Y(7K)、Z(9K)，预算 20K。

    前两轮全部保留原文并冻结；第三轮只有新出现的 Z 可替换，
    X、Y 因前缀冻结继续保留原文，即便合计超预算也不动。
    """
    session = Session(model="demo")
    manager = make_manager(tmp_path, config=ContextConfig(aggregate_budget_chars=20_000))

    # 第一轮：X 8K
    build_rounds(session, [("X", 8_000)])
    manager.apply_budget(session.messages)
    assert tool_result_blocks(session)["X"] == "计" * 8_000

    # 第二轮：加 Y 7K
    build_rounds(session, [("Y", 7_000)])
    manager.apply_budget(session.messages)
    assert tool_result_blocks(session)["X"] == "计" * 8_000
    assert tool_result_blocks(session)["Y"] == "计" * 7_000

    # 第三轮：加 Z 9K，合计 24K > 20K → 只有新候选 Z 可替换
    build_rounds(session, [("Z", 9_000)])
    replaced = manager.apply_budget(session.messages)
    assert set(replaced) == {"Z"}
    assert "persisted-output" in tool_result_blocks(session)["Z"]
    # X、Y 永不替换
    assert tool_result_blocks(session)["X"] == "计" * 8_000
    assert tool_result_blocks(session)["Y"] == "计" * 7_000
    # 冻结在 state 上：Z 每轮用同一预览原样重放
    manager.apply_budget(session.messages)
    assert tool_result_blocks(session)["Z"] == manager.state.replacements["Z"]


def test_aggregate_picks_largest_new_result(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("A", 40_000), ("B", 30_000), ("C", 20_000)])
    manager = make_manager(tmp_path, config=ContextConfig(aggregate_budget_chars=70_000))

    manager.apply_budget(session.messages)

    blocks = tool_result_blocks(session)
    # 合计 90K > 70K → 挑最大的 A 存盘；B、C 保留原文
    assert "persisted-output" in blocks["A"]
    assert blocks["B"] == "计" * 30_000
    assert blocks["C"] == "计" * 20_000
    files = list((tmp_path / ".mewcode" / "session" / "tool-results").glob("*.txt"))
    assert len(files) == 1


def test_apply_agent_action_persists_and_freezes(tmp_path):
    manager = make_manager(tmp_path)
    content, path = manager.apply_agent_action("act_1", "x" * 60_000)
    assert path
    assert "persisted-output" in content
    # 同一 id 再次进入：重放缓存预览，不重复写盘
    content2, path2 = manager.apply_agent_action("act_1", "anything")
    assert content2 == content
    assert path2 == ""
    files = list((tmp_path / ".mewcode" / "session" / "tool-results").glob("*.txt"))
    assert len(files) == 1


def test_apply_agent_action_small_result_kept(tmp_path):
    manager = make_manager(tmp_path)
    content, path = manager.apply_agent_action("small_1", "小结果")
    assert content == "小结果"
    assert path == ""
    assert "small_1" in manager.state.seen_ids  # 决定不替换 → 冻结


# ---------------------------------------------------------------------------
# 第 2 层：全量摘要
# ---------------------------------------------------------------------------


def test_parse_summary_extracts_only_summary_block():
    raw = "<analysis>草稿内容</analysis>\n<summary>正式摘要</summary>"
    assert parse_summary(raw) == "正式摘要"
    # 没有 summary 块时退回全文（至少丢掉 analysis）
    assert parse_summary("<analysis>a</analysis>剩下的") == "剩下的"
    assert parse_summary("") == ""


def test_prompt_too_long_detection():
    assert is_prompt_too_long_error(LLMError("Prompt is too long"))
    assert is_prompt_too_long_error(LLMError("HTTP 400 ... prompt_too_long"))
    assert not is_prompt_too_long_error(LLMError("网络连接失败"))


async def test_auto_compact_replaces_history(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000), ("a4", 3_000), ("a5", 3_000)])  # 约 15K+ token
    config = ContextConfig(context_window=10_000, summary_output_reserve=0, auto_safety_margin=0)
    summary_client = ScriptedClient([summary_turn()])
    manager = make_manager(tmp_path, config=config, compact_client=summary_client)

    outcome = await manager.maybe_auto_compact(session)

    assert outcome.performed
    assert outcome.reason == "auto"
    assert outcome.before_tokens > outcome.after_tokens
    # 摘要客户端没有工具定义：请求首条是摘要指令
    assert "上下文压缩器" in summary_client.requests[0][0]["content"]
    # 会话被替换为：摘要 + 边界消息 + 最近 5 条原文保留
    assert "重构 auth 模块" in session.messages[0].content
    assert any("不要根据摘要猜测" in m.content for m in session.messages)
    # 最近 5 条原样保留（最后一轮的 tool_result 还在）
    assert any(isinstance(b, ToolResultBlock) for b in (session.messages[-1].blocks or []))


async def test_auto_compact_below_threshold_is_noop(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 10)])  # token 极少
    manager = make_manager(tmp_path, compact_client=ScriptedClient([summary_turn()]))
    outcome = await manager.maybe_auto_compact(session)
    assert not outcome.performed
    assert len(session.messages) == 3  # 未被压缩


async def test_auto_compact_restores_recent_files(tmp_path):
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("def a(): pass", encoding="utf-8")
    file_b.write_text("def b(): pass", encoding="utf-8")
    session = Session(model="demo")
    # 三轮：先读 a.py，再读 b.py，最后跑一条命令（9 条消息，最近 5 条保留）
    session.add_message("user", "看看代码")
    session.add_message(
        "assistant",
        "",
        blocks=[ToolUseBlock(id="r1", name="ReadFile", input={"path": "a.py"})],
    )
    session.add_tool_result("r1", file_a.read_text(encoding="utf-8"))
    session.add_message("user", "再看 b")
    session.add_message(
        "assistant",
        "",
        blocks=[ToolUseBlock(id="r2", name="ReadFile", input={"path": "b.py"})],
    )
    session.add_tool_result("r2", file_b.read_text(encoding="utf-8"))
    session.add_message("user", "跑一下")
    session.add_message(
        "assistant",
        "",
        blocks=[ToolUseBlock(id="r3", name="Bash", input={"command": "echo hi"})],
    )
    session.add_tool_result("r3", "hi")
    config = ContextConfig(context_window=1000)  # 阈值 max(0, 1000-20K-13K)=0 → 必然触发
    manager = make_manager(tmp_path, config=config, compact_client=ScriptedClient([summary_turn()]))

    outcome = await manager.maybe_auto_compact(session)
    assert outcome.performed
    joined = "\n".join(m.content for m in session.messages)
    assert "[恢复文件]" in joined
    assert "def a(): pass" in joined
    assert "def b(): pass" in joined


async def test_circuit_breaker_stops_auto_after_three_failures(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000), ("a4", 3_000), ("a5", 3_000)])
    failing = ScriptedClient([LLMError("服务暂时不可用")] * 3)
    manager = make_manager(
        tmp_path,
        config=ContextConfig(context_window=10, summary_output_reserve=0, auto_safety_margin=0),
        compact_client=failing,
    )

    for _ in range(3):
        with pytest.raises(CompactError):
            await manager.maybe_auto_compact(session)
    assert manager.auto_enabled is False  # 熔断：停止自动触发

    # 熔断后不再触发，也不产生新的 API 调用
    outcome = await manager.maybe_auto_compact(session)
    assert not outcome.performed
    assert failing.calls == 3


async def test_manual_compact_not_blocked_by_breaker(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000), ("a4", 3_000), ("a5", 3_000)])
    manager = make_manager(
        tmp_path,
        config=ContextConfig(context_window=10, summary_output_reserve=0, auto_safety_margin=0),
        compact_client=ScriptedClient([LLMError("挂")] * 3 + [summary_turn()]),
    )
    # 自动压缩连续失败三次 → 熔断
    for _ in range(3):
        with pytest.raises(CompactError):
            await manager.maybe_auto_compact(session)
    assert manager.auto_enabled is False
    # 手动压缩（App 的 /compact）仍可直接执行，不受熔断限制
    outcome = await manager.compact(session)
    assert outcome.performed
    assert "重构 auth 模块" in session.messages[0].content


async def test_ptl_retry_drops_oldest_rounds(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000)])
    # 第一次调用撞上 Prompt Too Long，丢弃最旧的轮次组后重试成功
    summary_client = ScriptedClient([LLMError("prompt_too_long"), summary_turn()])
    manager = make_manager(
        tmp_path,
        config=ContextConfig(context_window=10, summary_output_reserve=0, auto_safety_margin=0),
        compact_client=summary_client,
    )

    outcome = await manager.maybe_auto_compact(session)
    assert outcome.performed
    assert summary_client.calls == 2  # 重试一次
    # 重试时消息明显变少（丢掉了最旧的轮次）
    assert len(summary_client.requests[1]) < len(summary_client.requests[0])


async def test_force_compact_bypasses_breaker_and_disabled(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000)])
    config = ContextConfig(context_window=10, summary_output_reserve=0, auto_safety_margin=0, auto_compact_enabled=False)
    manager = make_manager(tmp_path, config=config, compact_client=ScriptedClient([summary_turn()]))

    # 自动被关闭：不触发
    assert not (await manager.maybe_auto_compact(session)).performed
    # 紧急压缩不受影响
    outcome = await manager.maybe_auto_compact(session, force=True)
    assert outcome.performed
    assert outcome.reason == "force"


async def test_compact_failure_raises_compact_error(tmp_path):
    session = Session(model="demo")
    build_rounds(session, [("a1", 3_000), ("a2", 3_000), ("a3", 3_000)])
    manager = make_manager(
        tmp_path,
        config=ContextConfig(context_window=10, summary_output_reserve=0, auto_safety_margin=0),
        compact_client=ScriptedClient([LLMError("网络错误")]),
    )
    with pytest.raises(CompactError):
        await manager.maybe_auto_compact(session)
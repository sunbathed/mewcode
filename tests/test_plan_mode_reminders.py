from __future__ import annotations

from mewcode.engine.plan_mode import (
    PLAN_MODE_EXIT_REMINDER,
    build_plan_mode_reminder,
)


def test_plan_mode_reminder_turn_1_is_full():
    reminder = build_plan_mode_reminder(
        plan_file_path="/path/to/plan-001.plan.md",
        plan_exists=False,
        iteration=1,
    )
    assert "五阶段工作流" in reminder
    assert "探索（Exploration）" in reminder
    assert "/path/to/plan-001.plan.md" in reminder
    assert "使用 WriteFile 将完整计划写入计划文件" in reminder
    assert "计划文件尚未创建" in reminder


def test_plan_mode_reminder_sparse_turns():
    # 轮次 2, 3, 4, 5 返回精简版
    for turn in (2, 3, 4, 5):
        reminder = build_plan_mode_reminder(
            plan_file_path="/path/to/plan-001.plan.md",
            plan_exists=True,
            iteration=turn,
            reminder_interval=5,
        )
        assert "五阶段工作流" not in reminder
        assert "当前处于只读规划模式。请继续分析或更新计划文件" in reminder
        assert "/path/to/plan-001.plan.md" in reminder


def test_plan_mode_reminder_cadence_repeat_full():
    # 轮次 6（第 1 轮之后第 5 轮，即 iteration % 5 == 1）重复完整版
    reminder_6 = build_plan_mode_reminder(
        plan_file_path="/path/to/plan-001.plan.md",
        plan_exists=True,
        iteration=6,
        reminder_interval=5,
    )
    assert "五阶段工作流" in reminder_6


def test_plan_mode_exit_reminder():
    assert "规划模式已退出" in PLAN_MODE_EXIT_REMINDER
    assert "所有工具权限已恢复正常" in PLAN_MODE_EXIT_REMINDER
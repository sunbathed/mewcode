"""第 15 章：Pane 队员的 CLI 入口（tmux / iTerm2 后端）。

tmux / iTerm2 后端启动的队员是一个完整的 MewCode CLI 实例，通过环境
变量 MEWCODE_TEAM_NAME / MEWCODE_TEAMMATE_NAME 识别自己的团队身份。
启动后进入队员循环：

  1. 用初始 prompt 跑第一轮任务
  2. 存 transcript → 标记空闲 → 通知 Lead
  3. 每 0.5 秒轮询邮箱（消息会在下一轮循环开头被读到）
  4. 收到新消息 → 带着磁盘恢复的上下文继续工作

这对应理论篇的「共享文件邮箱加 500ms 轮询」：队员之间可以直接寻址，
通信本质上是异步的。
"""

from __future__ import annotations

import asyncio
import os
import sys

from .manager import TeamManager
from .teammate import TEAMMATE_APPENDIX, build_teammate_tools, format_mailbox_digest
from .transcript import save_transcript

POLL_INTERVAL_SECONDS = 0.5


def _parse_cli_args(argv: list[str]) -> tuple[str, str]:
    """解析 pane CLI 参数：返回 (work_dir, prompt)。

    build_cli_command 生成的形如：--teammate --work-dir /path 'prompt'。
    """
    work_dir = ""
    prompt_parts: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            work_dir = arg
            skip_next = False
            continue
        if arg == "--work-dir":
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        prompt_parts.append(arg)
    return work_dir, " ".join(prompt_parts).strip()


def run_teammate_cli() -> None:
    """pane 队员进程的同步入口（__main__ 检测环境变量后调用）。"""
    try:
        asyncio.run(_teammate_loop())
    except KeyboardInterrupt:
        pass


async def _teammate_loop() -> None:
    from ..config import load_provider_config
    from ..engine.agent import Agent, AgentConfig
    from ..llm_client import OpenAICompatibleClient
    from ..permission import PermissionChecker, PermissionMode
    from ..session import Session
    from ..tools import ToolRegistry

    team_name = os.environ.get("MEWCODE_TEAM_NAME", "").strip()
    teammate_name = os.environ.get("MEWCODE_TEAMMATE_NAME", "").strip()
    if not team_name or not teammate_name:
        print("Error: MEWCODE_TEAM_NAME / MEWCODE_TEAMMATE_NAME are required.", file=sys.stderr)
        return
    work_dir, initial_prompt = _parse_cli_args(sys.argv[1:])

    try:
        config = load_provider_config(dotenv_path=".env")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return

    manager = TeamManager()
    team = manager.get_team(team_name)
    if team is None:
        print(f"Error: team '{team_name}' not found.", file=sys.stderr)
        return
    member = team.get_member(teammate_name)
    if member is None:
        # Lead 忘了先注册（或进程重启恢复）：自动补注册
        from .models import TeammateInfo

        member = TeammateInfo(
            name=teammate_name,
            agent_id=os.urandom(6).hex(),
            agent_type="general-purpose",
            model=config.model,
            worktree_path=os.getcwd(),
            backend_type="tmux",
            is_active=True,
        )
        manager.register_member(team.name, member)
    agent_id = member.agent_id
    worktree_path = work_dir if work_dir and os.path.isdir(work_dir) else (
        member.worktree_path if os.path.isdir(member.worktree_path) else os.getcwd()
    )

    mailbox = manager.get_mailbox(team.name)
    task_store = manager.get_task_store(team.name)
    if mailbox is None or task_store is None:
        print(f"Error: team '{team_name}' storage missing.", file=sys.stderr)
        return

    registry = build_teammate_tools(
        ToolRegistry(workdir=worktree_path),
        worktree_path=worktree_path,
        task_store=task_store,
        mailbox=mailbox,
        manager=manager,
        team_name=team.name,
        teammate_name=teammate_name,
        self_agent_id=agent_id,
    )
    client = OpenAICompatibleClient(config, tools=registry.api_specs())

    def _drain(turn: int) -> list[str]:
        digest = format_mailbox_digest(mailbox.consume(agent_id))
        return [digest] if digest else []

    agent = Agent(
        config,
        session=Session(model=config.model),
        registry=registry,
        workdir=worktree_path,
        client=client,
        agent_config=AgentConfig(
            max_iterations=50,
            system_prompt=TEAMMATE_APPENDIX.strip(),
            custom_instructions="",
            memory_instructions="",
            skill_catalog="",
        ),
        permission=PermissionChecker(workdir=worktree_path, mode=PermissionMode.BYPASS_PERMISSIONS),
    )
    agent.pre_turn_inject = _drain

    async def _work_cycle(prompt: str) -> None:
        try:
            result = await agent.run_to_completion(prompt)
        except Exception as exc:  # noqa: BLE001 - 崩溃也要走空闲收尾
            result = f"（队员执行失败：{exc}）"
        save_transcript(team.name, agent_id, agent.session)
        manager.set_member_idle(team.name, teammate_name, last_output=result)

    # 第一轮：初始任务
    if initial_prompt:
        await _work_cycle(initial_prompt)
    # 队员循环：轮询邮箱，收到新指令就继续工作
    while True:
        messages = mailbox.consume(agent_id)
        if not messages:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        digest = format_mailbox_digest(messages)
        await _work_cycle(digest)


__all__ = ["run_teammate_cli"]

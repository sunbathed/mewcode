import os
import sys

from .config import ConfigurationError, load_provider_config
from .llm_client import OpenAICompatibleClient
from .memory import MemoryManager
from .storage import SessionStorage
from .terminal import Terminal
from .tools import ToolRegistry
from .tui import MewCodeTUI


def _is_pane_teammate() -> bool:
    """pane 队员身份：--teammate 标记或团队环境变量（tmux/iTerm2 后端启动）。"""
    if "--teammate" in sys.argv[1:]:
        return True
    return bool(os.environ.get("MEWCODE_TEAM_NAME") and os.environ.get("MEWCODE_TEAMMATE_NAME"))


def main() -> None:
    # 第 15 章：pane 队员入口——tmux/iTerm2 后端在窗格里启动的完整 CLI 实例，
    # 通过 MEWCODE_TEAM_NAME / MEWCODE_TEAMMATE_NAME 环境变量识别团队身份
    if _is_pane_teammate():
        from .teams.pane_entry import run_teammate_cli

        run_teammate_cli()
        return
    try:
        config = load_provider_config(dotenv_path=".env")
    except ConfigurationError as exc:
        Terminal().error(str(exc))
        return
    registry = ToolRegistry()
    client = OpenAICompatibleClient(config, tools=registry.api_specs())
    # 第 9 章：跨会话记忆——记忆提取用独立客户端（不带工具定义）
    workdir = os.getcwd()
    memory = MemoryManager(workdir)
    memory_client = OpenAICompatibleClient(config, tools=None)
    MewCodeTUI(
        config,
        storage=SessionStorage(),
        client=client,
        registry=registry,
        workdir=workdir,
        memory=memory,
        memory_client=memory_client,
    ).run()


if __name__ == "__main__":
    main()

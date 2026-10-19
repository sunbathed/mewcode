import os

from .config import ConfigurationError, load_provider_config
from .llm_client import OpenAICompatibleClient
from .memory import MemoryManager
from .storage import SessionStorage
from .terminal import Terminal
from .tools import ToolRegistry
from .tui import MewCodeTUI


def main() -> None:
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
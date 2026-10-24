"""第 15 章：Agent Teams——多 Agent 团队协作。

从 SubAgent 到 Agent Team：SubAgent 是「星型」拓扑，所有通信经过主
Agent；Team 是「网状」结构，队员有自己的上下文窗口，可以直接给其他
队员发消息。协调能力以工具的形式注入到每个队员的工具集，谁做什么由
LLM 自己看共享任务列表和消息判断——框架不裁判，只提供基础设施。

模块划分（对应文档「Python 源码解析」）：
  models.py        核心类型：BackendType / TeammateInfo / AgentTeam
  registry.py      AgentNameRegistry 单例：名字到 agent_id 的映射
  mailbox.py       基于文件的收件箱：读 / 写 / 消费 / 广播
  shared_task.py   共享任务存储：CRUD + 依赖关系（blocks/blocked_by）
  transcript.py    对话记录持久化：按 agent_id 存储，续写的根基
  backend_detect.py 后端自动检测：tmux → iTerm2 → in-process
  spawn_inprocess.py In-process 后端：asyncio.Task 包装
  spawn_tmux.py    Tmux 后端：split-window 三级降级 + CLI 命令构建
  spawn_iterm2.py  iTerm2 后端：it2 CLI 分屏
  manager.py       TeamManager：团队和成员的全部生命周期管理
  coordinator.py   协调者模式：双锁、工具收窄、四阶段工作流提示词
  teammate.py      队员构造 / 工具注入 / spawn / 续写（接合部）
  tools.py         TeamCreate / TeamDelete / SendMessage 顶层工具
  team_task_tools.py 共享任务工具（TaskCreate/TaskGet/TaskList/TaskUpdate）
  pane_entry.py    pane 队员的 CLI 循环（tmux / iTerm2 后端入口）
"""

from .coordinator import (
    COORDINATOR_MODE_ALLOWED_TOOLS,
    build_coordinator_registry,
    coordinator_env_enabled,
    coordinator_feature_enabled,
    get_coordinator_system_prompt,
    get_coordinator_user_context,
    is_coordinator_mode,
)
from .mailbox import Mailbox, MailboxMessage, create_message
from .manager import LEAD_AGENT_ID, TeamManager
from .models import AgentTeam, BackendType, TeamError, TeammateInfo, resolve_team_dir, unique_team_name
from .registry import AgentNameRegistry
from .shared_task import SharedTask, SharedTaskStore
from .spawn_inprocess import InProcessTeammateHandle, spawn_inprocess_teammate
from .team_task_tools import build_team_task_tools
from .teammate import (
    IN_PROCESS_TEAMMATE_ALLOWED_TOOLS,
    TEAMMATE_APPENDIX,
    TeammateSpawner,
    build_teammate_tools,
    format_mailbox_digest,
)
from .tools import SendMessageTool, TeamCreateTool, TeamDeleteTool
from .transcript import load_transcript, save_transcript

__all__ = [
    "COORDINATOR_MODE_ALLOWED_TOOLS",
    "IN_PROCESS_TEAMMATE_ALLOWED_TOOLS",
    "LEAD_AGENT_ID",
    "AgentNameRegistry",
    "AgentTeam",
    "BackendType",
    "InProcessTeammateHandle",
    "Mailbox",
    "MailboxMessage",
    "SendMessageTool",
    "SharedTask",
    "SharedTaskStore",
    "TEAMMATE_APPENDIX",
    "TeamCreateTool",
    "TeamDeleteTool",
    "TeamError",
    "TeamManager",
    "TeammateInfo",
    "TeammateSpawner",
    "build_coordinator_registry",
    "build_team_task_tools",
    "build_teammate_tools",
    "coordinator_env_enabled",
    "coordinator_feature_enabled",
    "create_message",
    "format_mailbox_digest",
    "get_coordinator_system_prompt",
    "get_coordinator_user_context",
    "is_coordinator_mode",
    "load_transcript",
    "resolve_team_dir",
    "save_transcript",
    "spawn_inprocess_teammate",
    "unique_team_name",
]

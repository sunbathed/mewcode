"""第 13 章：SubAgent 子任务分发系统。

把 Agent 包装成工具，让引擎层和工具层递归组合。主 Agent 调用统一的
Agent 工具，通过 subagent_type 分流「定义式」（预定义角色、空白对话）
与「Fork 式」（继承父对话、无条件后台）两条路径。子 Agent 在独立
上下文中完成任务，把结果交回主 Agent，主 Agent 的上下文不会被
子任务的中间过程污染。

模块划分（对应文档「Python 源码解析」）：
  - types.py       核心数据结构（AgentDef / AgentToolParams / ForkError）
  - parser.py      Agent 定义文件解析（frontmatter + body + 校验）
  - loader.py      多来源加载器（项目 > 用户 > 内置，同名先到先得，热重载）
  - tool_filter.py 工具过滤多层防线（MCP 直通 → 全局黑名单 → 自定义限制
                   → 后台白名单 → 定义级 tools/disallowedTools）
  - fork.py        Fork 路径（继承父对话 + Boilerplate 约束 + 防嵌套）
  - task_manager.py 后台任务生命周期（launch / poll / cancel）
  - trace.py       父子链路追踪（调用链 / token / 状态）
  - agent_tool.py  统一 Agent 工具（定义式 & Fork 式分发）
  - builtin/       三个内置 Agent（explore / plan / general-purpose）
"""

from .agent_tool import AgentTool
from .fork import FORK_BOILERPLATE, FORK_BOILERPLATE_TAG, ForkError, build_forked_messages
from .loader import AgentLoader
from .parser import AgentParseError, parse_agent_file
from .task_manager import BackgroundTask, ProgressInfo, TaskManager
from .tool_filter import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    CUSTOM_AGENT_DISALLOWED_TOOLS,
    resolve_agent_tools,
)
from .trace import TraceNode, TraceRegistry
from .types import (
    AgentDef,
    AgentToolParams,
    SOURCE_BUILTIN,
    SOURCE_PLUGIN,
    SOURCE_PROJECT,
    SOURCE_USER,
)

__all__ = [
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "AgentDef",
    "AgentLoader",
    "AgentParseError",
    "AgentTool",
    "AgentToolParams",
    "BackgroundTask",
    "CUSTOM_AGENT_DISALLOWED_TOOLS",
    "FORK_BOILERPLATE",
    "FORK_BOILERPLATE_TAG",
    "ForkError",
    "ProgressInfo",
    "SOURCE_BUILTIN",
    "SOURCE_PLUGIN",
    "SOURCE_PROJECT",
    "SOURCE_USER",
    "TaskManager",
    "TraceNode",
    "TraceRegistry",
    "build_forked_messages",
    "parse_agent_file",
    "resolve_agent_tools",
]
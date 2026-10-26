"""第 12 章：Hook 系统的核心数据结构。

一个 Hook 由三要素组成：事件（什么时候触发）、条件（什么情况下执行，
可省略表示无条件）、动作（执行什么）。事件与动作必须有，条件可选。

- Hook          一条 Hook 规则（含 once 运行时标记）
- Action        动作定义（command/prompt/http/agent 四大类型的统一结构）
- HookContext   事件触发时的上下文，条件求值与模板展开都从这里取值
- LifecycleEvent 15 个生命周期事件常量（Session/Turn/Message/Tool/System 五层）
- ToolRejectedError  pre_tool_use 拦截工具调用时抛出的唯一异常
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与 conditions.py 循环导入
    from .conditions import ConditionGroup


class LifecycleEvent(enum.StrEnum):
    """Agent 生命周期事件常量（覆盖会话、轮次、消息、工具、系统五层）。"""

    # 会话级：一个会话从开到关，各自只发生一次
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    # 轮次级：一轮对话的开始与结束，一个会话里反复出现
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    # 消息级：消息发送给 LLM 之前 / 收到 LLM 响应之后
    PRE_SEND = "pre_send"
    POST_RECEIVE = "post_receive"
    # 工具级：工具执行前（唯一可拦截）/ 工具执行后
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    # 系统级
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    COMPACT = "compact"
    # 场景化事件
    PERMISSION_REQUEST = "permission_request"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTE = "command_execute"


# 会修改文件系统的内置工具名集合：file_change 事件只在这些工具
# 执行成功时触发（写失败属于 error 场景，不构成「文件被修改」）。
WRITE_TOOL_NAMES = frozenset({"WriteFile", "EditFile", "ReplaceInFile"})


@dataclass(slots=True)
class Hook:
    """一条 Hook 规则：事件 + 条件 + 动作，外加三个执行控制标记。"""

    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False

    def should_run(self) -> bool:
        """once 语义：标记过已执行就不再触发（运行时标记，重启即重置）。"""
        if self.once and self.executed:
            return False
        return True

    def mark_executed(self) -> None:
        self.executed = True


@dataclass(slots=True)
class Action:
    """Hook 的动作定义。

    四种 type 各用一部分字段（大 union 结构，配置驱动系统常用）：
      - command：command + timeout
      - prompt：  message
      - http：    url + method + body + headers
      - agent：   prompt
    """

    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30


@dataclass(slots=True)
class HookContext:
    """事件触发时的所有上下文信息。

    get_field 供条件表达式取值（tool / event / args.xxx），
    expand 供动作模板做 $VAR 变量替换。未知字段返回空串不报错，
    让 Hook 配置对特定事件缺失字段的情况保持容错。
    """

    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    # 条件取值
    # ------------------------------------------------------------------

    def get_field(self, name: str) -> str:
        """按字段名取条件匹配用的值：tool / event / args.xxx。

        args.file_path 是配置文档里的常用写法，而 WriteFile 等工具
        的参数名是 path，缺字段时回退取 args.path，保证配置可命中。
        """
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            if not value and key in ("file_path", "path"):
                value = self.tool_args.get("path", self.tool_args.get("file_path", ""))
            return str(value) if value else ""
        return ""

    # ------------------------------------------------------------------
    # 模板变量替换
    # ------------------------------------------------------------------

    def expand(self, template: str) -> str:
        """替换命令/消息/body 模板里的上下文变量。

        支持 $EVENT / $TOOL_NAME / $FILE_PATH / $MESSAGE / $ERROR /
        $TOOL_ARGS.xxx；未定义的变量替换成空字符串，不报错。
        """
        import re

        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        # 剩余 $VAR 形式（含 $TOOL_ARGS.xxx 的残缺写法）视为未定义变量，清空
        result = re.sub(r"\$[A-Z][A-Z0-9_.]*", "", result)
        return result


class ToolRejectedError(Exception):
    """pre_tool_use Hook 拒绝工具调用时向外抛出的唯一异常。

    Agent Loop 收到它后，把拒绝原因作为工具执行结果返回给 LLM，
    形成「拦截 → Agent 收到原因 → 调整策略」的反馈循环。
    """

    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Hook '{hook_id}' rejected tool '{tool}': {reason}")


__all__ = [
    "Action",
    "Hook",
    "HookContext",
    "LifecycleEvent",
    "ToolRejectedError",
    "WRITE_TOOL_NAMES",
]

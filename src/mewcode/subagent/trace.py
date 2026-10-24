"""第 13 章：父子链路追踪（简化版）。

TraceRegistry 记录每个子 Agent 的调用链信息：agent_id、父 agent_id、
agent 类型、工具调用次数、Token 消耗、执行状态。用于诊断子 Agent
的执行情况，不跨会话持久化（本章明确不做）。

主 Agent 的 token 统计挂载在 Agent 的 total_input_tokens /
total_output_tokens 上（Agent Loop 每轮累加）；工具调用次数由
TraceRegistry 在 AgentTool 执行过程中累计。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TraceNode:
    """一次子 Agent 调用的追踪记录。"""

    agent_id: str
    parent_id: str = ""
    agent_type: str = ""
    name: str = ""
    status: str = "running"  # running / completed / failed / cancelled
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def duration(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


class TraceRegistry:
    """父子 Agent 调用链的追踪注册中心。"""

    def __init__(self) -> None:
        self._nodes: dict[str, TraceNode] = {}

    def start_agent(
        self,
        *,
        parent_id: str = "",
        agent_type: str = "",
        name: str = "",
    ) -> str:
        """登记一次子 Agent 启动，返回 agent_id。"""
        agent_id = uuid.uuid4().hex[:8]
        self._nodes[agent_id] = TraceNode(
            agent_id=agent_id,
            parent_id=parent_id,
            agent_type=agent_type,
            name=name,
        )
        return agent_id

    def complete_agent(
        self,
        agent_id: str,
        *,
        status: str = "completed",
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """标记一次子 Agent 调用结束，记录消耗与状态。"""
        node = self._nodes.get(agent_id)
        if node is None:
            return
        node.status = status
        node.tool_calls = tool_calls
        node.input_tokens = input_tokens
        node.output_tokens = output_tokens
        node.finished_at = time.monotonic()

    def get(self, agent_id: str) -> TraceNode | None:
        return self._nodes.get(agent_id)

    def list_nodes(self) -> list[TraceNode]:
        return list(self._nodes.values())

    def tree_text(self) -> str:
        """渲染父子调用链的文本视图（用于 /tasks 命令的链路部分）。"""
        if not self._nodes:
            return "（暂无子 Agent 调用记录）"
        lines = ["子 Agent 调用链："]
        by_parent: dict[str, list[TraceNode]] = {}
        for node in self._nodes.values():
            by_parent.setdefault(node.parent_id, []).append(node)
        root_ids = [n.agent_id for n in by_parent.get("", [])]
        if not root_ids:
            root_ids = sorted(self._nodes, key=lambda i: self._nodes[i].started_at)

        def render(node: TraceNode, depth: int) -> None:
            indent = "  " * depth
            lines.append(
                f"{indent}• {node.name or node.agent_id} "
                f"[{node.agent_type or 'fork'}] {node.status} "
                f"({node.tool_calls} 次工具调用，"
                f"{node.input_tokens + node.output_tokens} token)"
            )
            for child in sorted(by_parent.get(node.agent_id, []), key=lambda n: n.started_at):
                render(child, depth + 1)

        for root_id in sorted(root_ids, key=lambda i: self._nodes[i].started_at):
            render(self._nodes[root_id], 0)
        return "\n".join(lines)

    def clear(self) -> None:
        self._nodes.clear()


__all__ = [
    "TraceNode",
    "TraceRegistry",
]
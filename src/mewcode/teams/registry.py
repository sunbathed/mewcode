"""第 15 章：AgentNameRegistry——名字到 agent_id 的全局注册表。

SendMessage 的 to 参数支持「队友名称」或「agent_id」两种寻址方式：
名称可读（alice），ID 稳定（<task-notification> 返回的就是 ID，续写时
直接拿来用）。注册表让消息发送工具不需要关心调用方传的是哪种标识符。

单例 + 双重检查锁：整个进程共享一张表，多个协程/线程并发注册时不重复创建。
"""

from __future__ import annotations

import threading


class AgentNameRegistry:
    """name → agent_id 映射；resolve 接受名字或 ID 两种输入。"""

    _instance: "AgentNameRegistry | None" = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> "AgentNameRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def register(self, name: str, agent_id: str) -> None:
        if name and agent_id:
            self._names[name] = agent_id

    def unregister(self, name: str) -> None:
        self._names.pop(name, None)

    def resolve(self, name_or_id: str) -> str | None:
        """名字 → agent_id；本身就是 agent_id（能在 values 里找到）则原样返回。"""
        if name_or_id in self._names:
            return self._names[name_or_id]
        if name_or_id in self._names.values():
            return name_or_id
        return None

    def reset(self) -> None:
        """清空注册表（测试隔离用）。"""
        self._names.clear()


__all__ = ["AgentNameRegistry"]

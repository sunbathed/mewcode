"""第 15 章：Agent Teams 核心数据结构。

一个 AgentTeam 对应一个长期协作的团队：名称、负责人（Lead）、队员花名册
和持久化位置。TeamCreate 创建团队后，配置落盘到团队目录的 config.json，
tmux 后端启动的队友进程可以通过 load 读回配置，知道自己属于哪个团队。

数据层设计要点：
  - BackendType 继承 str：JSON 序列化时直接输出字符串值
  - TeammateInfo.backend_type 用 str 而不是枚举：反序列化不需要额外转换
  - is_active 三态：True 工作中 / False 空闲 / None 刚注册还没开始；
    没有 "terminated" 状态——终止的队员直接从 members 移除，不留墓碑
  - 团队目录在 ~/.mewcode/teams/ 而不是项目目录：一个团队可能跨多个项目
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TeamError(RuntimeError):
    """团队操作失败（不存在 / 校验不过 / 状态不符）。"""


class BackendType(str, Enum):
    """队员执行后端：tmux / iTerm2 独立进程隔离，in-process 同进程轻量运行。"""

    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


def _sanitize_name(name: str) -> str:
    """团队名转文件系统安全的 slug（小写、非法字符替换为 -、限长 48）。"""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower())
    slug = slug.strip("-")
    return (slug or "team")[:48]


def teams_base_dir() -> Path:
    """团队根目录：默认 ~/.mewcode/teams/，测试可用 MEWCODE_TEAMS_DIR 覆盖。"""
    override = os.environ.get("MEWCODE_TEAMS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mewcode" / "teams"


def resolve_team_dir(team_name: str) -> Path:
    """团队目录：{base}/{slug}。config.json / tasks.json / mailbox/ 都在里面。"""
    return teams_base_dir() / _sanitize_name(team_name)


def unique_team_name(team_name: str) -> str:
    """同名团队自动加数字后缀：my-team、my-team-2、my-team-3。"""
    slug = _sanitize_name(team_name)
    base_dir = teams_base_dir()
    if not (base_dir / slug).exists():
        return slug
    counter = 2
    while (base_dir / f"{slug}-{counter}").exists():
        counter += 1
    return f"{slug}-{counter}"


@dataclass(slots=True)
class TeammateInfo:
    """一个队友的完整信息（花名册中的一行）。"""

    name: str           # 队员名称，由 lead 分配（SendMessage 的寻址名）
    agent_id: str       # 对应的 Agent 实例 ID
    agent_type: str     # 使用的 Agent 定义
    model: str          # 模型，可覆盖
    worktree_path: str  # 所在的 Worktree 路径
    backend_type: str   # "tmux" | "iterm2" | "in-process"（str 便于序列化）
    is_active: bool | None = None  # True 工作中 / False 空闲 / None 未开始
    plan_mode_required: bool = False  # 修改操作前是否需要 Lead 审批

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "backend_type": self.backend_type,
            "is_active": self.is_active,
            "plan_mode_required": self.plan_mode_required,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TeammateInfo":
        return cls(
            name=str(data.get("name", "")),
            agent_id=str(data.get("agent_id", "")),
            agent_type=str(data.get("agent_type", "")),
            model=str(data.get("model", "")),
            worktree_path=str(data.get("worktree_path", "")),
            backend_type=str(data.get("backend_type", "")),
            is_active=data.get("is_active"),
            plan_mode_required=bool(data.get("plan_mode_required", False)),
        )


@dataclass(slots=True)
class AgentTeam:
    """团队：名称、负责人、花名册与持久化位置（自包含的工作区）。"""

    name: str
    lead_agent_id: str
    members: list[TeammateInfo] = field(default_factory=list)
    config_path: str = ""
    description: str = ""

    # ------------------------------------------------------------------
    # 序列化：save / load 成对出现，让 Team 状态可以跨进程共享
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lead_agent_id": self.lead_agent_id,
            "description": self.description,
            "members": [m.to_dict() for m in self.members],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentTeam":
        members = [TeammateInfo.from_dict(m) for m in data.get("members", []) if isinstance(m, dict)]
        return cls(
            name=str(data.get("name", "")),
            lead_agent_id=str(data.get("lead_agent_id", "")),
            description=str(data.get("description", "")),
            members=members,
        )

    def save(self) -> None:
        if not self.config_path:
            self.config_path = str(resolve_team_dir(self.name) / "config.json")
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, config_path: str) -> "AgentTeam":
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        team = cls.from_dict(data)
        team.config_path = config_path
        return team

    # ------------------------------------------------------------------
    # 成员管理
    # ------------------------------------------------------------------

    def add_member(self, member: TeammateInfo) -> None:
        """同名队员直接覆盖（重复 spawn 同名队员时更新而非累积）。"""
        for index, existing in enumerate(self.members):
            if existing.name == member.name:
                self.members[index] = member
                return
        self.members.append(member)

    def get_member(self, name: str) -> TeammateInfo | None:
        for member in self.members:
            if member.name == name:
                return member
        return None

    def set_member_active(self, name: str, active: bool) -> bool:
        """更新队员活跃状态；返回是否找到该队员。"""
        member = self.get_member(name)
        if member is None:
            return False
        member.is_active = active
        return True

    def all_idle(self) -> bool:
        """所有成员都明确标记为不活跃——Lead 判断是否可以收工。"""
        return all(m.is_active is False for m in self.members)

    def active_members(self) -> list[TeammateInfo]:
        """没有明确标记为不活跃的成员（含 is_active 为 None 的）。"""
        return [m for m in self.members if m.is_active is not False]


__all__ = [
    "AgentTeam",
    "BackendType",
    "TeammateInfo",
    "TeamError",
    "resolve_team_dir",
    "unique_team_name",
]

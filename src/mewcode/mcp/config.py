"""MCP 配置：让用户告诉 MewCode 该连哪些 Server。

配置遵循多级作用域模式（与 Git 配置一致）：
  - 项目级：{workdir}/.mewcode/config.yaml —— 只在当前项目生效，项目级优先
  - 用户级：~/.mewcode/config.yaml —— 在所有项目生效

合并策略：项目级优先，同名 Server 项目级覆盖用户级。

两种传输方式的配置：
  - stdio：有 command 字段 -> 启动子进程，走管道
  - Streamable HTTP：有 url 字段 -> 发 HTTP 请求

env 中的 ${VAR} 语法引用当前环境变量的值，而不是把 Token 明文写在配置里。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境未装 PyYAML 时使用内置兜底
    yaml = None

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(slots=True)
class MCPServerConfig:
    """一个 MCP Server 的配置。"""

    name: str
    # stdio 传输
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Streamable HTTP 传输
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # 公共
    transport: str = "stdio"  # stdio | http（由 command/url 推断，也可显式指定）
    timeout: float = 60.0
    disabled: bool = False

    @property
    def transport_type(self) -> str:
        """按配置推断实际 transport 类型：有 command -> stdio，有 url -> http。"""
        if self.url:
            return "http"
        return "stdio"

    def expanded_env(self) -> dict[str, str]:
        """把 env 中的 ${VAR} 展开为当前进程环境变量的值。"""
        return {key: expand_env_vars(value) for key, value in self.env.items()}

    def expanded_headers(self) -> dict[str, str]:
        return {key: expand_env_vars(value) for key, value in self.headers.items()}


class MCPConfigError(RuntimeError):
    """MCP 配置解析错误。"""


def expand_env_vars(value: str, environ: dict[str, str] | None = None) -> str:
    """展开字符串里的 ${VAR} 为环境变量值；未定义的变量保留原样。"""
    env = environ if environ is not None else os.environ

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env.get(name, match.group(0))

    return ENV_VAR_PATTERN.sub(_replace, value)


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """加载 YAML 文件；文件不存在返回空 dict。"""
    if not path.is_file():
        return {}
    if yaml is None:
        raise MCPConfigError(f"缺少 PyYAML 依赖，无法解析配置文件 {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MCPConfigError(f"配置文件 {path} 格式错误：{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MCPConfigError(f"配置文件 {path} 必须是键值对结构")
    return raw


def _parse_server_entry(name: str, entry: Any) -> MCPServerConfig:
    """解析单个 server 配置条目（dict）。"""
    if not isinstance(entry, dict):
        raise MCPConfigError(f"MCP Server {name} 配置必须是键值对结构")
    return MCPServerConfig(
        name=name,
        command=_optional_str(entry, "command"),
        args=_optional_list(entry, "args"),
        env=_optional_str_dict(entry, "env"),
        url=_optional_str(entry, "url"),
        headers=_optional_str_dict(entry, "headers"),
        transport=str(entry.get("transport", "")).strip() or "auto",
        timeout=float(entry.get("timeout", 60.0) or 60.0),
        disabled=bool(entry.get("disabled", False)),
    )


def _optional_str(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _optional_list(entry: dict[str, Any], key: str) -> list[str]:
    value = entry.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        return [str(value)]
    return [str(v) for v in value]


def _optional_str_dict(entry: dict[str, Any], key: str) -> dict[str, str]:
    value = entry.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MCPConfigError(f"配置项 {key} 必须是键值对结构")
    return {str(k): str(v) for k, v in value.items()}


def _extract_servers(config: dict[str, Any]) -> dict[str, MCPServerConfig]:
    """从配置 dict 中提取 mcp_servers 段。"""
    raw = config.get("mcp_servers")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MCPConfigError("mcp_servers 必须是键值对结构")
    servers: dict[str, MCPServerConfig] = {}
    for name, entry in raw.items():
        try:
            server = _parse_server_entry(str(name), entry)
        except MCPConfigError:
            raise
        if not server.disabled:
            servers[server.name] = server
    return servers


def load_mcp_config(
    *,
    workdir: str | None = None,
    user_path: str | os.PathLike[str] | None = None,
    project_path: str | os.PathLike[str] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, MCPServerConfig]:
    """加载并合并 MCP Server 配置：项目级优先于用户级。"""
    # 默认路径
    if user_path is None:
        user_path = Path.home() / ".mewcode" / "config.yaml"
    if project_path is None:
        base = Path(workdir).expanduser() if workdir else Path.cwd()
        project_path = base / ".mewcode" / "config.yaml"

    user_config = _load_yaml_file(Path(user_path).expanduser())
    project_config = _load_yaml_file(Path(project_path).expanduser())

    merged = _extract_servers(user_config)
    for name, server in _extract_servers(project_config).items():
        merged[name] = server  # 项目级覆盖用户级
    return merged


__all__ = [
    "MCPServerConfig",
    "MCPConfigError",
    "expand_env_vars",
    "load_mcp_config",
]
from __future__ import annotations

import asyncio
import platform
import subprocess
from typing import Any

from .base import BaseTool, ToolError, ToolTimeoutError

BASH_TIMEOUT = 10.0
MAX_OUTPUT_LENGTH = 30000


def _truncate(text: str, limit: int = MAX_OUTPUT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（输出过长，已截断）"


def _decode_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import locale

            enc = locale.getpreferredencoding(False) or "gbk"
            return raw.decode(enc, errors="replace")
        except Exception:
            return raw.decode("gbk", errors="replace")


class Bash(BaseTool):
    name = "Bash"
    description = (
        "在项目工作目录执行 shell 命令。受超时约束，超时即终止进程。"
        "Bash 只用于系统管理命令、构建和运行测试等必须由 shell 执行的操作。"
        "读文件、编辑文件、创建文件、查找或搜索文件绝不要用 Bash，而必须优先调用对应的专用工具。"
        + ("（Windows 环境下为 cmd.exe，查看文件请优先使用 Glob 或 dir，避免使用 ls）" if platform.system() == "Windows" else "")
    )
    safety = "command"
    timeout = BASH_TIMEOUT
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "可选的超时（秒），默认 10 秒"},
        },
        "required": ["command"],
    }

    def __init__(self, *, workdir: str | None = None) -> None:
        self.workdir = workdir

    async def _run(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("缺少 command 参数")
        timeout = float(args.get("timeout") or self.timeout)
        if timeout <= 0:
            raise ToolError("timeout 必须为正数")

        def _execute() -> tuple[str, str, int]:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=False,
                timeout=timeout,
                cwd=self.workdir,
            )
            return _decode_bytes(proc.stdout), _decode_bytes(proc.stderr), proc.returncode

        loop = asyncio.get_running_loop()
        try:
            stdout, stderr, returncode = await asyncio.wait_for(loop.run_in_executor(None, _execute), timeout=timeout + 2)
        except asyncio.TimeoutError:
            raise ToolTimeoutError(f"命令执行超时（超过 {timeout:g} 秒），已终止进程") from None
        except subprocess.TimeoutExpired:
            raise ToolTimeoutError(f"命令执行超时（超过 {timeout:g} 秒），已终止进程") from None
        except FileNotFoundError:
            raise ToolError("命令不可执行：找不到 shell 或命令") from None
        except OSError as exc:
            raise ToolError(f"命令执行失败：{exc}") from exc

        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        combined = _truncate("\n".join(parts)) if parts else "（命令执行成功，无输出）"
        if returncode != 0:
            raise ToolError(f"命令退出码 {returncode}：\n{combined}")
        return combined


__all__ = ["Bash", "BASH_TIMEOUT", "MAX_OUTPUT_LENGTH"]
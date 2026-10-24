"""第 12 章：Hook 动作执行器（四种类型通过分发表路由）。

  - command：启动 shell 子进程执行命令，捕获输出与退出码，支持超时
  - prompt： 只展开消息模板，返回文本（由引擎收集后注入对话）
  - http：    用 urllib 发 HTTP 请求（阻塞调用丢线程池），响应截断 500 字符
  - agent：   启动子 Agent 处理事件（占位 stub，真实实现留给第 13 章 SubAgent）

设计原则：执行器本身不抛异常——异常由引擎捕获记日志，
Hook 作为辅助机制，故障绝不中断 Agent 主流程。
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .models import Action, HookContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActionResult:
    """一次动作执行的结果：输出文本 + 是否成功。"""

    output: str
    success: bool


async def execute_command(action: Action, ctx: HookContext) -> ActionResult:
    """command 执行器：模板替换后启动子进程，stderr 合并 stdout。

    超时先 kill 再 wait，确保子进程真正退出，不留下僵尸进程。
    """
    command = ctx.expand(action.command)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=action.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ActionResult(output="Command timed out", success=False)
    output = _decode_output(stdout)
    return ActionResult(output=output, success=proc.returncode == 0)


def _decode_output(raw: bytes) -> str:
    """解码子进程输出：按候选编码依次尝试，避免中文输出乱码。

    Windows 上子进程（cmd）默认按本地代码页（GBK）编码输出，
    而 Python 进程 locale 偏好可能是 UTF-8，单用 locale 回退
    依然会乱码；因此按 utf-8 → gbk → locale 的顺序尝试解码。
    """
    if not raw:
        return ""
    import locale

    candidates = ["utf-8", "gbk"]
    preferred = locale.getpreferredencoding(False) or "utf-8"
    if preferred not in candidates:
        candidates.append(preferred)
    for encoding in candidates:
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


async def execute_prompt(action: Action, ctx: HookContext) -> ActionResult:
    """prompt 执行器：不执行任何外部操作，只把模板展开后的消息返回。"""
    message = ctx.expand(action.message)
    return ActionResult(output=message, success=True)


async def execute_http(action: Action, ctx: HookContext) -> ActionResult:
    """http 执行器：发送 HTTP 请求，状态码 2xx 视为成功。

    urlopen 是阻塞调用，用 run_in_executor 丢到线程池执行；
    响应体截断到 500 字符，防止超大响应撑爆内存。
    """
    url = ctx.expand(action.url)
    body = ctx.expand(action.body) if action.body else None
    headers = {key: ctx.expand(value) for key, value in action.headers.items()}
    method = action.method.upper()
    data = body.encode("utf-8") if body is not None else None

    def _do_request() -> ActionResult:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # 回环地址请求绕过环境代理：全局 http_proxy 不应劫持发往本机的通知
        opener = _build_http_opener(url)
        with opener.open(req, timeout=action.timeout) as resp:
            resp_body = resp.read().decode(errors="replace")[:500]
            return ActionResult(
                output=f"HTTP {resp.status}: {resp_body}",
                success=200 <= resp.status < 300,
            )

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_request)


def _build_http_opener(url: str):
    """构造 urllib opener：回环地址禁用代理，其余走默认（环境代理）。"""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


async def execute_agent(action: Action, ctx: HookContext) -> ActionResult:
    """agent 执行器（占位 stub）：真实子 Agent 运行时留到第 13 章。

    目前只记录日志并返回提示，保证配置加载与执行链路完整可跑。
    """
    prompt = ctx.expand(action.prompt)
    logger.info("Agent executor stub called with prompt: %s", prompt[:100])
    return ActionResult(output="agent executor not yet implemented", success=True)


# 动作类型 -> 执行器分发表
_EXECUTOR_MAP: dict[str, Callable[[Action, HookContext], Awaitable[ActionResult]]] = {
    "command": execute_command,
    "prompt": execute_prompt,
    "http": execute_http,
    "agent": execute_agent,
}


async def execute_action(action: Action, ctx: HookContext) -> ActionResult:
    """按 action.type 路由到对应执行器；未知类型返回失败结果而非抛错。"""
    executor = _EXECUTOR_MAP.get(action.type)
    if executor is None:
        return ActionResult(output=f"Unknown action type: {action.type}", success=False)
    return await executor(action, ctx)


__all__ = [
    "ActionResult",
    "execute_action",
    "execute_agent",
    "execute_command",
    "execute_http",
    "execute_prompt",
]
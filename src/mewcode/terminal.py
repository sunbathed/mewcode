from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GRAY = "\x1b[90m"
GREEN = "\x1b[92m"
RED = "\x1b[91m"
CYAN = "\x1b[96m"

# 工具执行中的 spinner 帧
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _enable_windows_vt() -> None:
    """Windows 10+ 控制台启用 ANSI 转义序列支持。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


class Terminal:
    """Claude Code 风格的终端渲染：`> ` 提示符、`⎿ ⎿` 工具卡片、`✓/✗` 状态。"""

    def __init__(self, *, input_fn: Callable[[str], str] = input, output=None, color: bool | None = None) -> None:
        self.input_fn = input_fn
        self.output = output or sys.stdout
        self.color = self._detect_color(color)
        self.interactive = bool(getattr(self.output, "isatty", lambda: False)())
        _enable_windows_vt()

    def _detect_color(self, override: bool | None) -> bool:
        if override is not None:
            return override
        if os.environ.get("NO_COLOR"):
            return False
        return bool(getattr(self.output, "isatty", lambda: False)())

    def _style(self, text: str, codes: str) -> str:
        if not self.color:
            return text
        return codes + text + RESET

    def write(self, text: str = "") -> None:
        print(text, file=self.output, flush=True)

    def stream_text(self, text: str) -> None:
        print(text, end="", file=self.output, flush=True)

    def raw(self, text: str) -> None:
        """无换行、无装饰的直接输出（用于 spinner 的 \r 行重写）。"""
        print(text, end="", file=self.output, flush=True)

    def prompt(self) -> str:
        return self.input_fn(self._style("> ", BOLD + CYAN))

    def error(self, text: str) -> None:
        self.write(self._style(f"✗ {text}", RED))

    @staticmethod
    def _summary_for(name: str, args: dict[str, Any]) -> str:
        """从工具入参提取一行参数摘要。"""
        for key in ("path", "command", "pattern"):
            if key in args:
                value = str(args[key]).replace("\n", " ")
                if len(value) > 120:
                    value = value[:120] + "…"
                return value
        import json

        text = json.dumps(args, ensure_ascii=False)
        if len(text) > 120:
            text = text[:120] + "…"
        return text

    def tool_call(self, name: str, args: dict[str, Any]) -> str:
        """组装工具卡片行：`⎿ ⎿ ReadFile greeting.txt`。"""
        summary = self._summary_for(name, args)
        label = f"⎿ ⎿ {name}"
        if summary:
            label += f" {summary}"
        return label

    def tool_running(self, label: str, frame: str) -> None:
        # 非交互输出（管道/测试/重定向）不输出 spinner，避免污染文本流
        if not self.interactive:
            return
        self.raw(f"\r{label} {frame}")

    def tool_finish(self, label: str, result) -> None:
        if self.interactive:
            self.raw("\r" + " " * (len(label) + 4) + "\r")
        if result.success:
            status = self._style("✓", GREEN)
            tail = self._style(f"（{result.duration:.2f}s）", DIM)
            self.write(f"{status} {label} {tail}")
        else:
            reason = "超时" if result.timed_out else "失败"
            status = self._style("✗", RED)
            detail = self._style(f"：{result.error}", RED)
            self.write(f"{status} {label} {detail}（{reason}）")


__all__ = ["Terminal"]
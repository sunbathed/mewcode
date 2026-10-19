"""第 10 章：命令解析器。

把用户输入拆成 (命令名, 参数, 是否是命令) 三元组：
  - 以 / 开头的输入才是命令，否则原样交给 Agent Loop
  - 第一个空格之前是命令名，之后是参数（参数保持原样，不再拆分）
  - 命令名转小写，/Help 与 /help 等价
"""

from __future__ import annotations


def parse_command(text: str) -> tuple[str, str, bool]:
    """解析一行用户输入。

    返回 (name, args, is_command)：
      - name: 小写命令名（无命令名时为 ""）
      - args: 参数原文（无参数时为 ""）
      - is_command: 是否以 / 开头
    """
    text = text.strip()
    if not text.startswith("/"):
        return "", "", False
    text = text[1:]
    if not text:
        return "", "", True
    parts = text.split(None, 1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args, True


__all__ = ["parse_command"]

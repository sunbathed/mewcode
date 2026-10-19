"""第 10 章：TUI Tab 补全弹窗。

用户在输入框输入 / 后按 Tab，显示所有可用命令；输入 /com 后按 Tab
补全为 /compact。单匹配直接补全，多匹配列出候选供用户选择。
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class CompletionPopup(Vertical):
    """dock 在底部的命令补全弹窗：候选列表选中后回传父组件。"""

    class Selected(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    DEFAULT_CSS = """
    CompletionPopup {
        height: 8;
        dock: bottom;
        display: none;
        background: $surface;
        border-top: round $primary;
    }
    #completion-list {
        height: 1fr;
    }
    """

    def __init__(self, registry) -> None:
        super().__init__(id="completion-popup")
        self.registry = registry
        self._matches: list[str] = []

    def compose(self):
        yield OptionList(id="completion-list")

    def show(self, prefix: str) -> None:
        matches = self.registry.complete(prefix)
        if not matches:
            self.hide()
            return
        self._matches = matches
        ol = self.query_one("#completion-list", OptionList)
        ol.clear_options()
        for item in matches:
            ol.add_option(Option(item, id=item))
        self.display = True
        ol.focus()

    def hide(self) -> None:
        self.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        value = event.option.id or ""
        self.hide()
        self.post_message(self.Selected(value))

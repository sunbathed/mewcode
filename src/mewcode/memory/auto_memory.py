"""第 9 章：自动记忆——Agent 在对话中自动积累的长期知识。

项目指令文件（MEWCODE.md）是你手动维护的；自动记忆是 Agent 在对话过程中
自动识别值得记住的信息，按四个类别分类存储，在后续会话中自动加载。

四类记忆天然分成两组：
  - 用户偏好（user）与纠正反馈（feedback）：关于「人」，跟你走 → 用户级目录
  - 项目知识（project）与参考资料（reference）：关于「项目」→ 项目级目录

存储结构（与理论篇一致）：.mewcode/memory/ 目录下每条记忆一个 .md 文件
（YAML frontmatter 描述元信息），再用 MEMORY.md 索引文件把指针串起来，
索引注入到 messages 上下文（上限 200 行 / 25KB，超了截断并附加警告）。
另外维护 user_role.md 记录用户画像（如「Go 工程师」），供 /memory 展示。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..session import Message

# 记忆目录结构
MEMORY_DIR_NAME = "memory"
INDEX_FILE_NAME = "MEMORY.md"
USER_ROLE_FILE_NAME = "user_role.md"

# 索引注入上限（支持环境变量覆盖，防止记忆太多撑爆上下文）
INDEX_MAX_LINES = int(os.getenv("MEWCODE_MEMORY_INDEX_MAX_LINES", "200"))
INDEX_MAX_BYTES = int(os.getenv("MEWCODE_MEMORY_INDEX_MAX_BYTES", "25000"))

# 自动提取的消息数阈值：间隔固定轮次或对话较长时触发
EXTRACTION_EVERY_N_MESSAGES = int(os.getenv("MEWCODE_MEMORY_EVERY_N_MESSAGES", "5"))

# LLM 输出分类节标题
_USER_LEVEL_HEADERS = ("用户偏好", "纠正反馈")
_PROJECT_LEVEL_HEADERS = ("项目知识", "参考资料")

# LLM 可能输出的占位符（空分类下的幻觉文字，必须过滤）
_PLACEHOLDERS = ("...", "暂无", "N/A", "无", "无值得记忆的内容")


class MemoryManagerError(RuntimeError):
    """记忆读写失败。"""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """一条记忆：frontmatter 元信息 + 正文。"""

    name: str
    description: str
    type: str
    content: str


# 显式「记住 X」的分类规则：按关键词把一句话快速归入四类
_USER_KEYWORDS = ("我是", "我是一名", "我喜欢", "我偏好", "我习惯", "我通常", "我的")
_FEEDBACK_KEYWORDS = ("不要", "别用", "请用", "应该用", "不要用", "要使用", "不能用")
_PROJECT_KEYWORDS = ("项目", "技术栈", "部署", "数据库", "目录", "路径", "仓库", "代码库", "服务", "模块", "CI", "构建")
_REFERENCE_KEYWORDS = ("http://", "https://", "文档", "链接", "网址", "文档站")


def classify_memory(text: str) -> str:
    """按关键词把一句话归入四类（user / feedback / project / reference）。"""
    lowered = text.lower()
    if any(k in lowered for k in _REFERENCE_KEYWORDS):
        return "reference"
    if any(k in lowered for k in _PROJECT_KEYWORDS):
        return "project"
    if any(k in lowered for k in _FEEDBACK_KEYWORDS):
        return "feedback"
    if any(k in lowered for k in _USER_KEYWORDS):
        return "user"
    return "user"


def _is_placeholder(line: str) -> bool:
    stripped = line.strip().lstrip("-").strip()
    return stripped in _PLACEHOLDERS or len(stripped) < 2


def _slug(name: str) -> str:
    """记忆文件名：小写 + 短横线，去掉特殊字符（保留中文）。"""
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name.strip(), flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    return text[:60] or "memory"


class MemoryManager:
    """自动记忆的管理入口：加载、显式记住、LLM 提取、展示、清空。

    两条路径：
      - 用户级 ~/.mewcode/memory/（用户偏好、纠正反馈跟人走）
      - 项目级 <项目根>/.mewcode/memory/（项目知识、参考资料跟项目走）
    """

    def __init__(self, project_root: str | Path) -> None:
        root = Path(project_root).expanduser()
        self.user_dir = Path.home() / ".mewcode" / MEMORY_DIR_NAME
        self.project_dir = root / ".mewcode" / MEMORY_DIR_NAME
        # 目录惰性创建：读操作（load_index / view）不需要目录存在
        # 上次提取处理到第几条消息：下次只处理新消息，避免重复提取
        self._last_extraction_msg_count = 0

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def _ensure_user_dir(self) -> Path:
        self.user_dir.mkdir(parents=True, exist_ok=True)
        return self.user_dir

    def _ensure_project_dir(self) -> Path:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        return self.project_dir

    def dir_for(self, memory_type: str) -> Path:
        return self.project_dir if memory_type in _PROJECT_LEVEL_HEADERS or memory_type == "project" else self.user_dir

    def index_path(self, directory: Path) -> Path:
        return directory / INDEX_FILE_NAME

    def user_role_path(self) -> Path:
        return self.user_dir / USER_ROLE_FILE_NAME

    # ------------------------------------------------------------------
    # 加载（注入 messages）
    # ------------------------------------------------------------------

    def load_index(self) -> str:
        """加载记忆索引文本：用户级 + 项目级 MEMORY.md 拼接，按注入上限截断。

        Agent 在处理用户请求之前就读过所有长期记忆的指针；某条记忆与当前
        任务相关时可以用 ReadFile 读对应的记忆文件。
        """
        sections: list[str] = []
        for directory in (self.user_dir, self.project_dir):
            index = self.index_path(directory)
            if index.exists():
                try:
                    content = index.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if content:
                    sections.append(content)
        joined = "\n\n".join(sections)
        if not joined:
            return ""
        lines = joined.splitlines()
        if len(lines) > INDEX_MAX_LINES or len(joined.encode("utf-8")) > INDEX_MAX_BYTES:
            truncated = "\n".join(lines[:INDEX_MAX_LINES])
            truncated = truncated.encode("utf-8")[:INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
            warning = "\n\n> ⚠️ 记忆索引超出注入上限，已截断。完整内容在记忆目录中。"
            return truncated + warning
        return joined

    def view(self) -> str:
        """完整展示记忆（/memory 命令）：用户画像 + 用户级/项目级索引 + 记忆文件。"""
        parts: list[str] = []
        user_role = self.user_role_path()
        if user_role.exists():
            try:
                role = user_role.read_text(encoding="utf-8").strip()
            except OSError:
                role = ""
            if role:
                parts.append(f"## 用户画像\n{role}")
        index = self.load_index()
        if index:
            parts.append(f"## 记忆索引\n{index}")
        for directory in (self.user_dir, self.project_dir):
            memories = self.list_memory_files(directory)
            if memories:
                blocks = ["", f"## {directory} 中的记忆文件"]
                for entry in memories:
                    blocks.append(
                        f"### {entry.name}（{entry.type}）\n{entry.description}\n\n{entry.content}"
                    )
                parts.append("\n".join(blocks))
        if not parts:
            return "暂无记忆。"
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 显式记忆：用户说「记住 X」
    # ------------------------------------------------------------------

    def remember(self, text: str) -> list[Path]:
        """显式记忆：把用户要求记住的内容写入记忆文件并更新索引。

        「记住我是 Go 工程师」这类用户画像会额外写入 user_role.md；
        分类用关键词规则快速判定（显式记忆要求即时生效，不走 LLM）。
        """
        content = _strip_remember_prefix(text)
        if not content:
            raise MemoryManagerError("没有可记住的内容")

        memory_type = classify_memory(content)
        directory = self.dir_for(memory_type)
        if memory_type in _PROJECT_LEVEL_HEADERS or memory_type == "project":
            directory = self._ensure_project_dir()
        else:
            directory = self._ensure_user_dir()
        name = _slug(content)
        description = content
        entry = MemoryEntry(name=name, description=description, type=memory_type, content=content)
        path = self._write_entry(directory, entry)
        self._rebuild_index(directory)

        written = [path]
        if _is_user_role_text(content):
            role_path = self._append_user_role(content)
            written.append(role_path)
        return written

    # ------------------------------------------------------------------
    # 自动提取（LLM 分析最近对话）
    # ------------------------------------------------------------------

    async def extract(self, client: Any, messages: list[Message]) -> str:
        """每轮 Agent Loop 结束后调用：让 LLM 分析新增对话，更新记忆。

        只处理上次提取之后的新消息（工具调用与结果被过滤，只留纯文本对话）。
        返回写入了哪些记忆文件的摘要文本。
        """
        recent = messages[self._last_extraction_msg_count :]
        self._last_extraction_msg_count = len(messages)
        if not recent:
            return ""

        conv_lines: list[str] = []
        for message in recent:
            if message.role == "user" and message.content:
                conv_lines.append(f"用户: {message.content}")
            elif message.role == "assistant" and message.content:
                conv_lines.append(f"助手: {message.content}")
        if not conv_lines:
            return ""

        current_index = self.load_index() or "（暂无记忆）"
        prompt = (
            MEMORY_EXTRACTION_PROMPT
            + f"\n\n## 当前记忆索引\n{current_index}\n\n"
            + "## 最近对话\n" + "\n".join(conv_lines)
        )
        try:
            response = await _collect_text(client, prompt)
        except Exception as exc:  # noqa: BLE001 - 提取失败不影响对话
            return f"记忆提取失败：{exc}"

        written = self._write_extracted(response)
        return "、".join(written) if written else "（没有值得记住的新内容）"

    def should_extract(self, message_count: int) -> bool:
        """提取频率控制：每 EXTRACTION_EVERY_N_MESSAGES 条新消息触发一次。"""
        return message_count - self._last_extraction_msg_count >= EXTRACTION_EVERY_N_MESSAGES

    def _write_extracted(self, content: str) -> list[str]:
        """解析 LLM 输出（### 分类节 + - 条目），按节分拣写入用户级/项目级。

        返回写入的文件名列表（占位符与重复内容被过滤）。
        """
        sections = _parse_extraction_sections(content)
        written: list[str] = []
        for header, lines in sections:
            real_lines = [line for line in lines if not _is_placeholder(line)]
            if not real_lines:
                continue
            memory_type = "user"
            if any(k in header for k in _USER_LEVEL_HEADERS):
                memory_type = "user" if "偏好" in header else "feedback"
            elif any(k in header for k in _PROJECT_LEVEL_HEADERS):
                memory_type = "project" if "知识" in header else "reference"
            directory = self.dir_for(memory_type)
            if memory_type in _PROJECT_LEVEL_HEADERS or memory_type == "project":
                directory = self._ensure_project_dir()
            else:
                directory = self._ensure_user_dir()
            for line in real_lines:
                entry = MemoryEntry(
                    name=_slug(line),
                    description=line,
                    type=memory_type,
                    content=line,
                )
                self._write_entry(directory, entry)
            self._rebuild_index(directory)
            written.append(header)
        return written

    # ------------------------------------------------------------------
    # 记忆文件读写
    # ------------------------------------------------------------------

    def _write_entry(self, directory: Path, entry: MemoryEntry) -> Path:
        """写一条记忆为独立 .md 文件（frontmatter + 正文），返回文件路径。"""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{entry.name}.md"
        counter = 1
        while path.exists() and _entry_from_file(path) is not None:
            # 同名记忆已存在：视为同一条记忆的更新，直接覆盖
            break
        header = (
            f"---\n"
            f"name: {entry.name}\n"
            f"description: {entry.description}\n"
            f"type: {entry.type}\n"
            f"---\n"
        )
        body = f"\n{entry.content}\n"
        path.write_text(header + body, encoding="utf-8")
        return path

    def _rebuild_index(self, directory: Path) -> None:
        """从目录下的记忆文件重建 MEMORY.md 索引（每行一个指针）。"""
        lines: list[str] = []
        for entry in self.list_memory_files(directory):
            lines.append(f"- [{entry.name}]({entry.name}.md) — {entry.description}")
        content = "\n".join(lines)
        if content:
            content += "\n"
        self.index_path(directory).write_text(content, encoding="utf-8")

    def _append_user_role(self, text: str) -> Path:
        """把用户画像（如「我是 Go 工程师」）追加到 user_role.md（按条去重）。"""
        path = self._ensure_user_dir() / USER_ROLE_FILE_NAME
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if text in existing:
            return path
        line = f"- {text}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return path

    # ------------------------------------------------------------------
    # 列表 / 清空
    # ------------------------------------------------------------------

    def list_memory_files(self, directory: Path) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        for path in sorted(directory.glob("*.md")):
            if path.name in (INDEX_FILE_NAME, USER_ROLE_FILE_NAME):
                continue
            entry = _entry_from_file(path)
            if entry is not None:
                entries.append(entry)
        return entries

    def clear(self) -> int:
        """清空所有记忆（删除记忆文件，重建空索引；用户画像一并清空）。"""
        removed = 0
        for directory in (self.user_dir, self.project_dir):
            if not directory.exists():
                continue
            for entry in self.list_memory_files(directory):
                (directory / f"{entry.name}.md").unlink(missing_ok=True)
                removed += 1
            self.index_path(directory).write_text("", encoding="utf-8")
        user_role = self.user_dir / USER_ROLE_FILE_NAME
        user_role.unlink(missing_ok=True)
        return removed


def _strip_remember_prefix(text: str) -> str:
    for prefix in ("请记住", "帮我记住", "记住"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text.strip()


def _is_user_role_text(text: str) -> bool:
    """用户画像特征：描述「我是什么样的人 / 我具备什么」的自我陈述。"""
    return bool(re.match(r"^(我是|我是一名|我是一个|我具备|我擅长|我是做|我从事)", text.strip()))


def _parse_extraction_sections(content: str) -> list[tuple[str, list[str]]]:
    """解析 LLM 提取输出：按 ### 标题分节，收集节下的 - 条目。"""
    sections: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("###"):
            if current_header:
                sections.append((current_header, current_lines))
            current_header = stripped[3:].strip()
            current_lines = []
        elif stripped.startswith("- ") and current_header:
            current_lines.append(stripped[2:].strip())
    if current_header:
        sections.append((current_header, current_lines))
    return sections


def _entry_from_file(path: Path) -> MemoryEntry | None:
    """从记忆文件解析 frontmatter（name/description/type）+ 正文。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.startswith("---"):
        return None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return None
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    content = parts[2].strip()
    if not meta.get("name"):
        return None
    return MemoryEntry(
        name=meta["name"],
        description=meta.get("description", ""),
        type=meta.get("type", "user"),
        content=content,
    )


async def _collect_text(client: Any, messages: list[dict[str, Any]] | str) -> str:
    """流式收集提取调用的文本输出（摘要/记忆提取请求不传 tools）。"""
    from ..llm_client import TextDelta

    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    parts: list[str] = []
    async for event in client.stream(messages):
        if isinstance(event, TextDelta):
            parts.append(event.text)
    return "".join(parts)


MEMORY_EXTRACTION_PROMPT = """\
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息。

分类规则（输出时用 ### 作为小节标题）：
- ### 用户偏好：用户的编码习惯和风格要求（如「不要用 tab」「回复用中文」）
- ### 纠正反馈：用户明确指出错误并给出正确做法的内容（如「这里应该用互斥锁」）
- ### 项目知识：当前项目的技术信息（技术栈、目录结构、部署方式）
- ### 参考资料：外部链接和文档地址

规则：
- 已有相同含义的记忆不要重复创建（参考当前记忆索引）。
- 每条以 `- ` 开头，输出完整内容。
- 没有值得记忆的内容就什么都不输出。
"""


__all__ = [
    "EXTRACTION_EVERY_N_MESSAGES",
    "INDEX_MAX_BYTES",
    "INDEX_MAX_LINES",
    "MEMORY_EXTRACTION_PROMPT",
    "MemoryEntry",
    "MemoryManager",
    "MemoryManagerError",
    "classify_memory",
]

"""第 14 章：WorktreeManager——Worktree 完整生命周期的中枢。

所有操作都通过 WorktreeManager 发起：Create（快速恢复 + 全新创建）、
Enter（保存现场）、Exit（安全检查 + 可选清除）、Remove、List、
AutoCleanup 配合（变更判断在 cleanup.py）、RestoreSession（崩溃恢复）。

设计要点：
  - asyncio.Lock() 保护：Python 版用 asyncio 做并发，所有操作都是 async
  - 快速恢复：目录已存在时不调 git 子进程，纯文件系统读 HEAD SHA
  - 缓存一致性：进入/退出时清除 FileCache + 注册的清理回调
    （工作目录变了，之前缓存的文件内容全部失效）
  - 安全退出：remove 前检查未提交修改 + 新增提交，须显式 discard_changes
  - git 命令统一注入 GIT_TERMINAL_PROMPT=0，需要凭证时失败而不是挂起
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .changes import GIT_ENV, has_worktree_changes, run_git
from .models import Worktree, WorktreeSession
from .session import load_worktree_session, save_worktree_session
from .setup import perform_post_creation_setup
from .slug import flatten_slug, validate_slug

logger = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    """Worktree 操作失败（校验不过 / git 命令失败 / 状态不符）。"""


# ---------------------------------------------------------------------------
# 配置加载（.mewcode/config.yaml 的 worktree: 段）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorktreeSettings:
    """Worktree 的可配置项（全部有默认值，配置文件可省略整个段）。"""

    symlink_directories: list[str] = field(default_factory=list)
    worktree_dir: str = ""            # 空 = 默认 .mewcode/worktrees
    cleanup_interval_seconds: int = 3600   # 后台清理轮询间隔
    stale_cutoff_hours: int = 24      # 超过多久视为过期


def load_worktree_settings(workdir: str) -> WorktreeSettings:
    """从 {workdir}/.mewcode/config.yaml 的 worktree: 段读配置；缺省用默认值。"""
    settings = WorktreeSettings()
    config_path = Path(workdir).expanduser() / ".mewcode" / "config.yaml"
    if not config_path.is_file():
        return settings
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置读不出来就用默认值，不阻断启动
        return settings
    if not isinstance(raw, dict):
        return settings
    section = raw.get("worktree")
    if not isinstance(section, dict):
        return settings
    dirs = section.get("symlink_directories") or section.get("symlinkDirectories") or []
    if isinstance(dirs, str):
        dirs = [item.strip() for item in dirs.split(",") if item.strip()]
    if isinstance(dirs, list):
        settings.symlink_directories = [str(item).strip() for item in dirs if str(item).strip()]
    if isinstance(section.get("worktree_dir"), str) and section["worktree_dir"].strip():
        settings.worktree_dir = str(section["worktree_dir"]).strip()
    for key, attr in (
        ("cleanup_interval_seconds", "cleanup_interval_seconds"),
        ("cleanupIntervalSeconds", "cleanup_interval_seconds"),
        ("stale_cutoff_hours", "stale_cutoff_hours"),
        ("staleCutoffHours", "stale_cutoff_hours"),
    ):
        value = section.get(key)
        if value is not None:
            try:
                setattr(settings, attr, int(value))
            except (TypeError, ValueError):
                continue
    return settings


# ---------------------------------------------------------------------------


class WorktreeManager:
    """Worktree 生命周期管理：创建 / 进入 / 退出 / 删除 / 列举 / 恢复。"""

    def __init__(
        self,
        repo_root: str,
        file_cache: Any | None = None,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.file_cache = file_cache
        self.symlink_directories = symlink_directories or []
        self.worktree_dir = worktree_dir or str(Path(repo_root) / ".mewcode" / "worktrees")
        self._mewcode_dir = Path(repo_root) / ".mewcode"
        self._lock = asyncio.Lock()
        self.active: dict[str, Worktree] = {}
        self.current_session: WorktreeSession | None = None
        # 外部模块注册的缓存清理回调（进入/退出 worktree 时工作目录变了，
        # 之前缓存的文件内容、提示词、记忆索引全部失效）
        self._clear_caches_callback: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # 缓存清理
    # ------------------------------------------------------------------

    def register_cache_clear_callback(self, callback: Callable[[], None]) -> None:
        """注册缓存清理回调；进入/退出 worktree 时统一触发。"""
        self._clear_caches_callback.append(callback)

    def _clear_all_caches(self) -> None:
        """一次性清掉所有缓存。每个回调独立 try/except：一个失败不影响其他。"""
        if self.file_cache is not None:
            try:
                self.file_cache.clear()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worktree file cache clear failed: %s", exc)
        for cb in self._clear_caches_callback:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cache clear callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Create：快速恢复 + 全新创建
    # ------------------------------------------------------------------

    async def create(self, name: str, base_branch: str = "HEAD") -> Worktree:
        """创建（或恢复）一个 worktree，返回 Worktree 描述。"""
        async with self._lock:
            err = validate_slug(name)
            if err:
                raise WorktreeError(f"invalid worktree name: {err}")

            if name in self.active:
                raise WorktreeError(f"worktree already exists: {name}")

            flat_slug = flatten_slug(name)
            wt_path = os.path.join(self.worktree_dir, flat_slug)
            branch_name = f"worktree-{flat_slug}"

            # 快速恢复：目录已存在且是合法 worktree（上次进程崩溃遗留），
            # 纯文件系统读 HEAD SHA，不启动 git 子进程，直接复用
            head_sha = self.read_worktree_head_sha(wt_path)
            if head_sha is None and (Path(wt_path) / ".git").exists():
                # 文件系统解析失败但目录确实是个 worktree：git 子进程兜底
                head_sha = self._resolve_head_by_git(wt_path)
            if head_sha is not None:
                logger.info("Fast recovery: reusing existing worktree at %s", wt_path)
                wt = Worktree(
                    name=name,
                    path=wt_path,
                    branch=branch_name,
                    based_on=base_branch,
                    head_commit=head_sha,
                )
                self.active[name] = wt
                return wt

            # 全新创建。-B（不是 -b）：分支已存在时强制重置，容忍上次
            # 没清理干净的孤儿分支；-b 遇到同名分支会直接报错
            Path(self.worktree_dir).mkdir(parents=True, exist_ok=True)
            result = self._run_git(["worktree", "add", "-B", branch_name, wt_path, base_branch])
            if result.returncode != 0:
                raise WorktreeError(f"git worktree add failed: {result.stderr.strip()}")

            perform_post_creation_setup(
                self.repo_root,
                wt_path,
                symlink_directories=self.symlink_directories,
            )
            wt = Worktree(
                name=name,
                path=wt_path,
                branch=branch_name,
                based_on=base_branch,
                head_commit=self._resolve_head_by_git(wt_path),
            )
            self.active[name] = wt
            return wt

    @staticmethod
    def read_worktree_head_sha(wt_path: str) -> str | None:
        """纯文件系统读取 worktree 的 HEAD SHA（不启动子进程，约 3ms）。

        worktree 目录里的 .git 不是目录而是文件，内容形如
        gitdir: /path/to/main/.git/worktrees/xxx。解析它找到真正的
        gitdir，读 HEAD：符号引用继续读 ref 文件（先私有目录再公共
        目录，最后 packed-refs），分离头指针本身就是 SHA。
        解析不出返回 None（调用方走全新创建路径）。
        """
        wt = Path(wt_path)
        git_file = wt / ".git"
        if not git_file.exists():
            return None
        try:
            content = git_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        gitdir = Path(content.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = wt / gitdir
        try:
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not head.startswith("ref:"):
            return head or None
        ref = head[4:].strip()

        def _read_ref(base: Path) -> str | None:
            ref_file = base / ref
            if ref_file.is_file():
                try:
                    return ref_file.read_text(encoding="utf-8").strip() or None
                except OSError:
                    return None
            return None

        # 1) worktree 私有 ref（gitdir/refs/...）
        sha = _read_ref(gitdir)
        if sha:
            return sha
        # 2) 公共 ref：从 commondir 文件找到主 .git 目录
        try:
            common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
            common_dir = Path(common)
            if not common_dir.is_absolute():
                common_dir = (gitdir / common).resolve()
        except OSError:
            return None
        sha = _read_ref(common_dir)
        if sha:
            return sha
        # 3) packed-refs 兜底
        packed = common_dir / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    sha_part, _, ref_part = line.partition(" ")
                    if ref_part.strip() == ref:
                        return sha_part.strip()
            except OSError:
                return None
        return None

    @staticmethod
    def _resolve_head_by_git(wt_path: str) -> str:
        """git rev-parse HEAD 兜底解析（纯文件系统解析失败时用）。"""
        result = run_git(["rev-parse", "HEAD"], cwd=wt_path, timeout=15)
        return result.stdout.strip() if result.returncode == 0 else ""

    # ------------------------------------------------------------------
    # Enter：保存现场
    # ------------------------------------------------------------------

    async def enter(self, name: str, *, session_id: str = "", hook_based: bool = False) -> WorktreeSession:
        """进入 worktree：清缓存 → 记录现场 → 持久化 session。

        注意没有 chdir：本实现是显式 cwd 模式——worktree 路径记进
        session，由上层把工具的工作目录切到 worktree（每次工具调用
        显式声明「在 worktree 里跑」），进程级 cwd 保持原位不动。
        """
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")
        self._clear_all_caches()
        session = WorktreeSession(
            original_cwd=os.getcwd(),
            worktree_path=wt.path,
            worktree_name=name,
            original_branch=self._get_current_branch(),
            original_head_commit=self._get_head_commit(),
            session_id=session_id,
            hook_based=hook_based,
        )
        self.current_session = session
        save_worktree_session(self._mewcode_dir, session)
        return session

    # ------------------------------------------------------------------
    # Exit / Remove：安全检查 + 可选清除
    # ------------------------------------------------------------------

    async def exit(
        self, name: str, action: str = "keep", discard_changes: bool = False
    ) -> None:
        """退出 worktree。action: keep 保留目录（下次可复用）/ remove 删除。

        变更保护：删除时若存在未提交修改或未推送 commit，必须显式传
        discard_changes=True——一道安全阀，防止 LLM 误删有价值的工作成果。
        """
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")

        if action == "remove" and not discard_changes:
            if has_worktree_changes(wt.path, wt.head_commit):
                raise WorktreeError(
                    f"worktree '{name}' has uncommitted changes or new commits; "
                    "set discard_changes=True to force removal."
                )

        # 退出语义与进入对称：清缓存（切回原目录后旧缓存全部失效）、
        # 清 session 并持久化为空对象（--resume 正确工作的前提）
        self._clear_all_caches()
        if self.current_session is not None and self.current_session.worktree_name == name:
            self.current_session = None
        save_worktree_session(self._mewcode_dir, None)

        if action == "remove":
            await self._remove_worktree(wt)
            self.active.pop(name, None)

    async def remove(self, name: str, discard_changes: bool = False) -> None:
        """独立删除一个 worktree（不必先进入过）。变更保护同 exit。"""
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")
        if not discard_changes and has_worktree_changes(wt.path, wt.head_commit):
            raise WorktreeError(
                f"worktree '{name}' has uncommitted changes or new commits; "
                "set discard_changes=True to force removal."
            )
        await self._remove_worktree(wt)
        self.active.pop(name, None)
        if self.current_session is not None and self.current_session.worktree_name == name:
            self.current_session = None
            save_worktree_session(self._mewcode_dir, None)

    async def _remove_worktree(self, wt: Worktree) -> None:
        """git worktree remove --force + git branch -D（强制删除路径）。

        两条命令之间 sleep(100ms)：间隔太短可能因 git lockfile 未释放
        而失败，100ms 是经验值。
        """
        result = self._run_git(["worktree", "remove", "--force", wt.path])
        if result.returncode != 0:
            raise WorktreeError(f"git worktree remove failed: {result.stderr.strip()}")
        await asyncio.sleep(0.1)
        branch_result = self._run_git(["branch", "-D", wt.branch])
        if branch_result.returncode != 0:
            logger.warning("git branch -D %s failed: %s", wt.branch, branch_result.stderr.strip())

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_worktrees(self) -> list[Worktree]:
        """所有已知的 worktree：活跃注册表 + 磁盘扫描（恢复态也可见）。"""
        known: dict[str, Worktree] = dict(self.active)
        worktree_dir = Path(self.worktree_dir)
        if worktree_dir.is_dir():
            for entry in sorted(worktree_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if any(wt.path == str(entry) for wt in known.values()):
                    continue
                flat = entry.name
                # 磁盘上有但未注册：反推 slug（+ 还原为 / 的尝试从 active 匹配，
                # 这里直接用平铺名当名字展示）
                sha = self.read_worktree_head_sha(str(entry))
                known[flat] = Worktree(
                    name=flat,
                    path=str(entry),
                    branch=f"worktree-{flat}",
                    based_on="",
                    head_commit=sha or "",
                )
        return list(known.values())

    # ------------------------------------------------------------------
    # Restore：进程重启后恢复
    # ------------------------------------------------------------------

    def restore_session(self) -> WorktreeSession | None:
        """恢复持久化的 worktree session（--resume / 崩溃恢复）。

        「先加载，再验证」：从磁盘读 session，再验证 worktree 目录是否
        还在（能读出 HEAD SHA）。目录已被手动删除就清掉持久化数据。
        """
        session = load_worktree_session(self._mewcode_dir)
        if session is None:
            return None
        head_sha = self.read_worktree_head_sha(session.worktree_path)
        if head_sha is None:
            save_worktree_session(self._mewcode_dir, None)
            return None
        wt = Worktree(
            name=session.worktree_name,
            path=session.worktree_path,
            branch=f"worktree-{flatten_slug(session.worktree_name)}",
            based_on="",
            head_commit=head_sha,
        )
        self.active[session.worktree_name] = wt
        self.current_session = session
        logger.info("Restored worktree session: %s at %s", session.worktree_name, session.worktree_path)
        return session

    # ------------------------------------------------------------------
    # git 辅助
    # ------------------------------------------------------------------

    def _run_git(self, args: list[str], *, timeout: int = 60) -> Any:
        """在主仓库根目录跑 git 命令（统一注入 GIT_TERMINAL_PROMPT=0）。"""
        return run_git(args, cwd=self.repo_root, timeout=timeout)

    def _get_current_branch(self) -> str:
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _get_head_commit(self) -> str:
        result = self._run_git(["rev-parse", "HEAD"], timeout=15)
        return result.stdout.strip() if result.returncode == 0 else ""


__all__ = ["WorktreeError", "WorktreeManager", "WorktreeSettings", "load_worktree_settings"]

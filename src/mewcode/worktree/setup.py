"""第 14 章：Worktree 创建后的环境初始化。

git worktree add 创建的是一个干净的工作目录，缺少主仓库里已有的一些
运行时依赖，直接让 Agent 进去工作会报错或行为不一致。四步初始化：

  A. 复制本地配置    settings.local.json / .env 被 gitignore，不会跟着
                     worktree 走，但子 Agent 运行需要它们
  B. Git Hooks 配置  worktree 不继承 core.hooksPath；优先 .husky/，
                     回退 .git/hooks/，显式 git config 指回主仓库
  C. 软链接大目录    node_modules 这类依赖目录不该每个 worktree 复制一份，
                     软链接共享主仓库的同一份（目录列表来自配置）
  D. 复制被忽略文件  .worktreeinclude 用 gitignore 语法声明哪些被忽略
                     但运行时必需的文件（如编译产物）要复制过来

整个 setup 是 best-effort 的：任何一步失败只记警告，不中断创建流程。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .changes import run_git

logger = logging.getLogger(__name__)

# 被 gitignore 但子 Agent 运行需要的本地配置文件
LOCAL_CONFIG_FILES = ("settings.local.json", ".env")


def perform_post_creation_setup(
    repo_root: str, wt_path: str, symlink_directories: list[str] | None = None
) -> None:
    """创建后的四步初始化（只对新建执行，快速恢复跳过）。"""
    root = Path(repo_root)
    wt = Path(wt_path)
    _copy_local_configs(root, wt)
    _setup_git_hooks(root, wt)
    _create_symlinks(root, wt, symlink_directories or [])
    _copy_ignored_files(root, wt)


def _copy_local_configs(root: Path, wt: Path) -> None:
    """复制本地配置文件到 worktree（存在才复制，失败只警告）。"""
    for name in LOCAL_CONFIG_FILES:
        src = root / name
        dst = wt / name
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            logger.warning("Worktree setup: copy %s failed: %s", name, exc)


def _setup_git_hooks(root: Path, wt: Path) -> None:
    """让 worktree 的 git hooks 指向主仓库的 hooks 目录。

    优先用 Husky 的 .husky 目录，没有就用 .git/hooks；通过
    git config core.hooksPath 保持提交规范（pre-commit/lint）一致。
    """
    hooks_path = ""
    husky_dir = root / ".husky"
    if husky_dir.is_dir():
        hooks_path = str(husky_dir)
    else:
        git_hooks = root / ".git" / "hooks"
        if git_hooks.is_dir():
            hooks_path = str(git_hooks)
    if not hooks_path:
        return
    result = run_git(["config", "core.hooksPath", hooks_path], cwd=str(wt), timeout=10)
    if result.returncode != 0:
        logger.warning("Worktree setup: core.hooksPath config failed: %s", result.stderr.strip())


def _create_symlinks(root: Path, wt: Path, directories: list[str]) -> None:
    """软链接大型依赖目录（node_modules / .venv 等）到主仓库的对应目录。

    两个跳过条件让这段代码可以重复调用而不出问题：
    源不存在跳过；目标已存在（含已是符号链接）也跳过。
    """
    for dirname in directories:
        src = root / dirname
        dst = wt / dirname
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Windows 上 symlink 需要开发者模式/管理员权限；失败只警告
            import os

            os.symlink(str(src), str(dst), target_is_directory=src.is_dir())
        except OSError as exc:
            logger.warning("Worktree setup: symlink %s failed: %s", dirname, exc)


def _copy_ignored_files(root: Path, wt: Path) -> None:
    """按 .worktreeinclude 的规则复制被 gitignore 但运行需要的文件。

    用 git ls-files --others --ignored --exclude-standard --directory 列出
    所有被忽略的文件，再用 .worktreeinclude 的模式（gitignore 语法）
    过滤出需要的那些。best-effort：模式匹配失败或文件不存在只警告。
    """
    include_file = root / ".worktreeinclude"
    if not include_file.is_file():
        return
    patterns = [
        line.strip()
        for line in include_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not patterns:
        return
    listing = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=str(root),
    )
    if listing.returncode != 0:
        logger.warning("Worktree setup: list ignored files failed: %s", listing.stderr.strip())
        return

    from fnmatch import fnmatch

    for rel_path in listing.stdout.splitlines():
        rel = rel_path.strip().rstrip("/")
        if not rel:
            continue
        if not any(fnmatch(rel, pat) or fnmatch(Path(rel).name, pat) for pat in patterns):
            continue
        src = root / rel
        dst = wt / rel
        if not src.is_file() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            logger.warning("Worktree setup: copy ignored file %s failed: %s", rel, exc)


__all__ = ["LOCAL_CONFIG_FILES", "perform_post_creation_setup"]

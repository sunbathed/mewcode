"""第二道防线：路径沙箱。

文件操作是 Coding Agent 最常用的能力，但文件系统是扁平的，
从项目目录到 /etc/passwd 只差一个路径。路径沙箱的规则很简单：
所有文件操作都必须限制在允许的目录（项目根目录 + 系统临时目录 + 配置白名单）内。

实现要点（对应理论篇伪代码）：
  1. 解析为绝对路径；
  2. 解析符号链接（防止通过 symlink 逃逸）——如果失败（文件还不存在），
     退而求其次解析父目录，父目录真实路径在沙箱内则新文件创建出来也在沙箱内；
  3. 检查真实路径的前缀是否落在任一允许根目录内。

沙箱默认允许两个目录：项目根目录和系统临时目录。可通过 extra_dirs 扩展白名单。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class PathSandbox:
    """验证文件路径是否落在允许目录范围内，防止符号链接逃逸。"""

    def __init__(
        self,
        workdir: str | None = None,
        *,
        extra_dirs: list[str] | None = None,
        allow_temp: bool = True,
    ) -> None:
        base = Path(workdir).expanduser().absolute() if workdir else Path.cwd()
        self.workdir = base
        self.allowed_roots: list[Path] = [base]
        if allow_temp:
            self.allowed_roots.append(Path(tempfile.gettempdir()).expanduser().absolute())
        for d in extra_dirs or []:
            if d:
                self.allowed_roots.append(Path(d).expanduser().absolute())

    def _norm(self, path: Path) -> str:
        """归一化用于比较：解析后再大小写归一（Windows 文件系统不区分大小写）。"""
        return os.path.normcase(str(path))

    def _resolve(self, path: Path) -> Path | None:
        """解析符号链接；解析失败返回 None（交由调用方检查父目录）。"""
        try:
            return Path(os.path.realpath(path))
        except OSError:
            return None

    def validate(self, requested_path: str) -> tuple[bool, str]:
        """校验路径是否在沙箱内。返回 (是否允许, 描述信息)。

        允许时第二项为解析后的真实路径；拒绝时第二项为拒绝原因。
        """
        if not requested_path:
            return False, "路径为空"
        raw = Path(requested_path).expanduser()
        if not raw.is_absolute():
            raw = self.workdir / raw

        # 1) 先解析符号链接（完整路径）
        real = self._resolve(raw)

        # 2) 文件可能还不存在（WriteFile 创建新文件），检查父目录
        if real is None:
            parent = self._resolve(raw.parent)
            if parent is None:
                return False, f"无法解析路径：{requested_path}"
            real = parent / raw.name

        # 3) 前缀检查：真实路径是否落在某个允许根目录内
        for root in self.allowed_roots:
            if self._within(real, root):
                return True, str(real)
        return False, f"路径 {requested_path} 超出沙箱范围"

    def _within(self, child: Path, root: Path) -> bool:
        """child 是否等于或位于 root 之下（大小写不敏感）。"""
        root_resolved = self._resolve(root) or root
        child_norm = self._norm(child)
        root_norm = self._norm(root_resolved)
        return child_norm == root_norm or child_norm.startswith(root_norm.rstrip("/\\") + os.sep)


__all__ = ["PathSandbox"]
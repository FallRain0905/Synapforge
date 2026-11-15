"""在系统文件管理器里打开一个目录（"在资源管理器里显示"）。

**这个能力只在 studio 跑在你自己的机器上时有意义**，而且它是一个能把任意
路径交给 `explorer.exe` / `open` / `xdg-open` 的自由度，所以必须收两道口子：

1. **白名单根目录**：只允许打开 studio 自己管得到的目录（状态目录、Agent 工作区、
   集群工作区、成果所在目录）。用户传来别处的路径一律拒绝——不是缩小权限，
   而是这个功能本来就没有"打开任意路径"的需求。
2. **不做路径拼接**：传进来的是路径本身就是路径，不做 `..` 展开后二次拼接，
   解析后必须仍在某个白名单根之内。

另外：**只有本机运行才启用**。studio 部署到服务器后（反代 / `/studio/` /
非回环绑定），这个接口一律返回 `reveal_unavailable`——服务器上没有"用户的
文件管理器"，硬开只会给服务器起一个无头进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .commands import StudioError


def _roots(store: Any) -> list[Path]:
    """允许打开的根目录集合。"""

    from ..paths import state_dir

    roots: list[Path] = []
    for raw in (state_dir(), state_dir() / "workspace", state_dir() / "artifacts"):
        roots.append(Path(raw))
    try:
        from . import store as studio_store

        for agent in studio_store.list_agents(store):
            workspace = str(agent.get("workspace") or "").strip()
            if workspace:
                roots.append(Path(workspace))
        for cluster in studio_store.list_clusters(store):
            workspace = str(cluster.get("workspace_root") or "").strip()
            if workspace:
                roots.append(Path(workspace))
    except Exception:  # noqa: BLE001 — 取不到数据库也不该让整个接口 500
        pass
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.expanduser().resolve())
        except OSError:
            continue
    return resolved


def _inside(target: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def reveal_enabled(host: str = "", base_path: str = "") -> bool:
    """这个进程是否应该提供"打开文件夹"能力。

    部署形态（反代挂在子路径下、或绑到非回环地址）下没有意义，直接关掉。
    """

    if base_path and base_path.strip("/"):
        return False
    if host and host.strip() not in ("127.0.0.1", "localhost", "::1"):
        return False
    # 无图形会话的 Linux（服务器）上 explorer/xdg-open 只会报错
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def reveal(store: Any, path: str, *, host: str = "", base_path: str = "") -> dict[str, Any]:
    """在文件管理器里定位到 `path`（目录则直接打开，文件则选中它）。"""

    if not reveal_enabled(host=host, base_path=base_path):
        raise StudioError("reveal_unavailable",
                          ["当前是服务器/反代部署，没有本地文件管理器可打开"])

    raw = str(path or "").strip().strip('"')
    if not raw:
        raise StudioError("path_required", ["要打开的路径不能为空"])
    try:
        target = Path(raw).expanduser().resolve()
    except OSError as error:
        raise StudioError("path_invalid", [str(error)]) from error
    if not target.exists():
        raise StudioError("path_not_found", [str(target)])
    if not _inside(target, _roots(store)):
        # 这条是硬边界：不做"用户可能只是想看看"的例外
        raise StudioError("path_not_allowed",
                          ["只能打开 studio 自己的工作区/成果目录"])
    _spawn(target)
    return {"path": str(target), "directory": str(target if target.is_dir() else target.parent)}


def _spawn(target: Path) -> None:
    try:
        if sys.platform == "win32":
            if target.is_dir():
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                # /select 需要逗号紧贴路径，且路径用反斜杠
                subprocess.Popen(["explorer", f"/select,{target}"],
                                 close_fds=True)
        elif sys.platform == "darwin":
            args = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
            subprocess.Popen(args, close_fds=True)
        else:
            directory = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(directory)], close_fds=True)
    except Exception as error:  # noqa: BLE001 — 打不开就如实报，不静默成功
        raise StudioError("reveal_failed", [f"{type(error).__name__}: {error}"]) from error

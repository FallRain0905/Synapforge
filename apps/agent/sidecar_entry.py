"""桌面端内核的打包入口（PyInstaller 用）。

壳只认这个可执行文件：它启动"常驻体"——本地回环 HTTP 契约服务（`docs/SIDECAR_CONTRACT.md`）
+ 平台连接监督 + 任务循环（DP-2-02）。不依赖仓库、不依赖系统 Python。

用法：
    math-agent-sidecar.exe [--port N] [--state-dir PATH] [--workspace PATH] [--contract-only]
选项：
    --workspace       Agent 工作目录（用户选的那个）；不给就用 platform.json 里记住的，再退回当前目录
    --contract-only  只提供契约服务，不连接平台（排障用；见 agentd sidecar-run）
环境变量：
    MAP_STATE_DIR  覆盖状态目录（默认 %LOCALAPPDATA%\\MathAgentPlatform）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path



def _force_utf8_stdio() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    打包成 exe 后，Windows 上标准输出默认走 cp1252（除非父进程设了 PYTHONIOENCODING）：
    一旦某条日志里有中文（我们的日志**到处是中文**，例如 `no_project_grant` 的提示），
    `print()` 就抛 UnicodeEncodeError → 进程直接退出。表现是"内核反复崩溃重启"，
    首装未配对时尤其容易踩到（见 COST/桌面端 0.2.0 的首次运行验证）。
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

# 打包后 sys.path 里没有仓库结构，这里把冻结目录与源码目录都补上
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent.parent) not in sys.path:  # 开发态：仓库根
    sys.path.insert(0, str(_HERE.parent.parent))

try:
    from agentd import daemon_run, sidecar_run
    from sidecar_api import default_state_dir, read_platform_info
except ImportError:  # 打包为包结构时的相对导入
    from .agentd import daemon_run, sidecar_run  # type: ignore
    from .sidecar_api import default_state_dir, read_platform_info  # type: ignore


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Math Agent Platform desktop sidecar (kernel)")
    parser.add_argument("--port", type=int, help="fixed port (default: pick a free one)")
    parser.add_argument("--state-dir", default=None, help="writable state directory")
    parser.add_argument(
        "--workspace",
        default=None,
        # 以前这里硬写 `Path.cwd()`：打包后 cwd 是安装目录（`resources\\sidecar`），
        # 于是"工作区"落在程序安装目录里——只读、且用户根本不知道文件写去了哪（FM-0）。
        help="Agent workspace directory (default: the value remembered in platform.json, else the process cwd)",
    )
    parser.add_argument("--contract-only", action="store_true", help="serve the local contract without connecting to the platform")
    parser.add_argument("--device-token", help="debug only; production reads the token from the OS credential store")
    parser.add_argument("--url", default=None, help="platform base URL override (default: taken from the sidecar state)")
    args = parser.parse_args()

    if args.state_dir is None:
        args.state_dir = str(default_state_dir())
    if args.contract_only:
        sidecar_run(args)
        return 0
    daemon_run(_daemon_args(args))
    return 0


def _daemon_args(args: argparse.Namespace) -> argparse.Namespace:
    """内核启动参数 → `daemon_run` 需要的完整参数集（其余走默认值）。

    平台地址与工作目录来自状态目录的 `platform.json`（配对/上次选择时写入），可用 `--url` / `--workspace` 覆盖。
    """

    import argparse as _argparse

    defaults = _daemon_defaults(args.state_dir)
    return _argparse.Namespace(
        url=args.url or defaults.get("url") or "http://127.0.0.1:8010",
        state_dir=args.state_dir,
        state_path=None,
        device_token=args.device_token,
        credential_target=None,
        grant=None,
        project_id=None,
        project_token=None,
        agent_id=defaults.get("agent_id"),
        device_id=defaults.get("device_id"),
        session_id=None,
        workspace=args.workspace or defaults.get("workspace"),
        stages=[],
        lease_seconds=900,
        idle_seconds=5.0,
        max_idle_seconds=60.0,
        task_timeout=300.0,
        executor_command=None,
        codex_path=None,
        codex_sandbox="read-only",
        inventory_ttl=300.0,
        heartbeat_interval=15.0,
        send_poll_interval=0.25,
        retry_after_seconds=5,
        reconnect_base=1.0,
        reconnect_max=60.0,
        control_poll_interval=0.25,
        process_stop_timeout=5.0,
        port=args.port,
        skip_credential=False,
        start_paused=False,
    )


def _daemon_defaults(state_dir: str | None = None) -> dict:
    """落盘下来的平台地址、设备标识与工作目录（配对或上次选择时写入；没有就退回本地默认）。

    读的是**显式给的那个 state 目录**（而不是永远读默认目录）：壳启动时会带 `--state-dir`，
    两边读不同的文件就会出现"配对记录在 A、默认值从 B 读"这种对不上的怪状态。
    """

    directory = Path(state_dir).expanduser() if state_dir else None
    return read_platform_info(directory)


if __name__ == "__main__":
    raise SystemExit(main())
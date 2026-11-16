# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：synapforge studio 桌面内核（ONEDIR + 无控制台）。

入口是 `apps/local/studio_entry.py`；`--noconsole` 下 sys.stdout 为 None，
内核启动时会自己把 stdout/stderr 接到 `--log-file`（见 studio/server.py）。

用法（脚本会带好参数）：
    python -m PyInstaller --noconfirm --clean scripts/studio-core.spec
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # SPECPATH = <repo>/scripts
# 排障用：SF_CORE_CONSOLE=1 打一个带控制台的版本，能看到 import 失败的 traceback。
CONSOLE = os.environ.get("SF_CORE_CONSOLE") == "1"
LOCAL = ROOT / "apps" / "local"
API = ROOT / "apps" / "api"
# synapforge/__init__.py 会把 apps/agent 挂到 sys.path 上（receipts 等复用模块），
# 冻结时这些路径不存在，必须在构建期就放进 pathex，否则运行时 ModuleNotFoundError。
AGENT = ROOT / "apps" / "agent"

a = Analysis(
    [str(LOCAL / "studio_entry.py")],
    # `app`（协议实现）和 `packages` 都在仓库里，必须显式进搜索路径，
    # 否则冻结后 import app 会失败。
    pathex=[str(ROOT), str(LOCAL), str(API), str(AGENT)],
    binaries=[],
    datas=[
        (str(LOCAL / "synapforge" / "studio" / "static"), "synapforge/studio/static"),
    ],
    hiddenimports=[
        "pydantic",
        "sqlite3",
        "packages.agent_protocol",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除可选项，别让 PyInstaller 顺着 `if TYPE_CHECKING` 把整个科学生态拖进来：
    # pydantic 的类型检查分支写了 `from rich.console import Console`，而本机装了
    # IPython/matplotlib/pandas/scipy/torch/PySide6，实测会打到 699MB。
    excludes=[
        "fastapi", "uvicorn", "starlette",  # studio 不走 HTTP 服务端的 FastAPI 栈
        "rich", "IPython", "ipykernel", "jupyter_client", "matplotlib",
        "matplotlib_inline", "pandas", "scipy", "numpy", "torch", "functorch",
        "safetensors", "PySide6", "shiboken6", "jedi", "parso",
        # 下面这些同样只出现在 TYPE_CHECKING / 懒加载分支里：
        # boto3 只在 S3ObjectStore 里用（studio 用 LocalObjectStore）；
        # email_validator 是 pydantic EmailStr 的可选件（契约里没有 EmailStr）；
        # PIL/pygments/httpx/dns 是这条可选链的尾巴。
        "boto3", "botocore", "s3transfer", "jmespath",
        "email_validator", "dns", "httpx", "pygments", "PIL",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="synapforge-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=CONSOLE,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# MCP 是 stdio 协议，GUI 子系统没有 stdout，必须单独给一个控制台 exe。
# 入口脚本按 exe 名分流（见 apps/local/studio_entry.py）。
mcp_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="synapforge-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    mcp_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="synapforge-core",
)

"""验收专用启动器（多智能体 e2e）：把平台数据目录指到临时目录，再跑 uvicorn。

与 scripts/_llm_e2e_launcher.py 同一手法：`app/main.py` 的 BASE_DIR 是模块级写死的，
这里在导入 app.main **之前**把 store 换到临时库——开发库自始至终不被读写。
只用于本机验收；不参与生产路径，也不改任何业务代码。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

data_dir = Path(os.environ["MULTI_AGENT_E2E_DATA_DIR"]).resolve()
data_dir.mkdir(parents=True, exist_ok=True)

import app.main as main_module  # noqa: E402

from app.store import Store  # noqa: E402
from app.object_store import create_object_store  # noqa: E402

main_module.store = Store(data_dir / "platform.db", object_store=create_object_store(data_dir / "objects"))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(main_module.app, host="127.0.0.1", port=int(os.environ["MULTI_AGENT_E2E_PORT"]), log_level="warning")

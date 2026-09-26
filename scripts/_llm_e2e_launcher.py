"""验收专用启动器：把平台数据目录指到临时目录，再跑 uvicorn。

为什么需要它：`app/main.py` 里 `BASE_DIR = Path(__file__).parents[1]` 是**写死**的，
没有环境变量可覆盖。端到端验收要的是「干净的库」（首个注册账号才会成为管理员），
所以这里在导入 app.main **之前**把模块级的 BASE_DIR 改掉。

只用于本机验收；不参与生产路径，也不改任何业务代码。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

data_dir = Path(os.environ["LLM_E2E_DATA_DIR"]).resolve()
data_dir.mkdir(parents=True, exist_ok=True)

import app.main as main_module  # noqa: E402

# 关键一步：store 已在导入期建好（指向真实 apps/api/data），这里换掉它指向临时库。
from app.store import Store  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app import llm_channels  # noqa: E402

main_module.store = Store(data_dir / "platform.db", object_store=create_object_store(data_dir / "objects"))
llm_channels.ensure_schema(main_module.store)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(main_module.app, host="127.0.0.1", port=int(os.environ["LLM_E2E_PORT"]), log_level="warning")

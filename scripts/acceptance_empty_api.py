"""验收用临时 API：真实应用 + 独立空数据库（不触碰开发库）。

用途：前端验收需要"项目列表为空"的真实起点，而开发库初始化会播种样例项目
（`apps/api/app/store.py` 的 `_seed`）。本脚本用一份独立的临时 SQLite 库启动同一个
FastAPI 应用，并清掉项目域数据，从而得到真实的空列表响应。

用法（在仓库根目录执行）：

    python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010

前端侧需要一个指向它的 Web 服务（NEXT_PUBLIC_API_URL 在构建期内联，dev 模式可在启动时指定）：

    cd apps/web
    NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next dev -p 3014

注意：产品代码把 CORS 白名单硬编码为 localhost:3000 / 127.0.0.1:3000
（`apps/api/app/main.py`），所以若前端不在 3000 端口，需要在**本脚本内**为临时实例放开
来源（见下方 `EXTRA_CORS_ORIGINS`），不要去改产品代码。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT))

import app.main as main  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app.store import Store  # noqa: E402

# 临时库位置：默认放在系统临时目录，可用 UX_ACCEPTANCE_DIR 覆盖。
TEST_DIR = pathlib.Path(os.environ.get("UX_ACCEPTANCE_DIR") or pathlib.Path(os.environ.get("TEMP", "/tmp")) / "ux-acceptance")
# 允许的前端来源：默认补上常见的验收端口，可用 UX_ACCEPTANCE_ORIGINS 覆盖（逗号分隔）。
EXTRA_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "UX_ACCEPTANCE_ORIGINS",
        "http://127.0.0.1:3014,http://localhost:3014",
    ).split(",")
    if origin.strip()
]

# 每次启动都从零建库：残留旧库会因重复播种（agents 唯一约束）启动失败。
shutil.rmtree(TEST_DIR, ignore_errors=True)
TEST_DIR.mkdir(parents=True, exist_ok=True)

store = Store(TEST_DIR / "platform.db", object_store=create_object_store(TEST_DIR / "objects"))

# 只清项目域数据并保留身份种子（member-001 等）；否则 list_projects_for_member 会抛 member_not_found。
store.db.execute("PRAGMA foreign_keys = OFF")
tables = [
    row[0]
    for row in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    if not row[0].startswith("sqlite_")
]
for table in tables:
    columns = {row[1] for row in store.db.execute(f"PRAGMA table_info({table})").fetchall()}
    if "project_id" in columns:
        store.db.execute(f"DELETE FROM {table}")
for table in ("project_memberships", "projects"):
    if table in tables:
        store.db.execute(f"DELETE FROM {table}")
store.db.commit()
store.db.execute("PRAGMA foreign_keys = ON")

main.store = store
# 模块级单例同样绑定了创建时的 store：只替换 main.store 不够，
# 否则 HTTP 走临时库而 WebSocket 网关仍查真实开发库（设备会 403）。
main.importer = main.CumcmImporter(store)
main.handoff_importer = main.CumcmHandoffImporter(store)
main.gateway = main.GatewayService(store)
app = main.app

if EXTRA_CORS_ORIGINS:
    from starlette.middleware.cors import CORSMiddleware  # noqa: E402

    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            origins = list(middleware.kwargs.get("allow_origins", []))
            for extra in EXTRA_CORS_ORIGINS:
                if extra not in origins:
                    origins.append(extra)
            middleware.kwargs["allow_origins"] = origins
    app.middleware_stack = app.build_middleware_stack()
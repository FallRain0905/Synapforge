"""FM-0 端到端验收：带 `input_artifacts` 的任务，输入**真的**落到 `<workspace>/inputs/`。

只做单元测试不够：真 bug（`agentd._execute_task` 里引用从未定义的 `loop_identity`）就藏在
"取不到输入也照常跑"的兜底分支里，函数照样返回、任务照样上报成功——只有把**真内核**跑在
**真平台**上、再去磁盘与项目里对账，才能证明这条链路是活的。

跑的东西与线上同构，只是换了库与端口：
- 真 `uvicorn` 起同一个 FastAPI 应用（临时 SQLite + 临时对象目录，**不碰开发库**）；
- 真内核子进程 `agentd.py register` / `worker-run --once`（真 HTTP、真能力令牌、真 LocalRunner）；
- 真下载路由 `/api/agent/artifacts/{id}/content`（响应头形状与线上一致）。

对账四件事：
1. `inputs/` 里的字节与 `sha256` 等于平台侧的成果物内容；
2. `.math-agent-platform/inputs-manifest.json` 里 `artifact id → 相对路径 → sha256 → 字节数` 齐全；
3. 提示词里列出了这些文件（执行体知道去哪儿看），且**没有**"处理失败"字样；
4. 平台注册的 `local_workspace` == Runner 真正的 cwd == `--workspace`；并且 `inputs/` 里的文件
   **没有**被产出采集器当成"本轮产出"再上传一份。

用法（仓库根目录）：

    python -X utf8 scripts/deploy/_fm0_verify.py [--keep]

退出码 0 = 全过；非 0 = 有对账项不成立（脚本会把差异打印出来）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "apps" / "api"), str(ROOT / "apps" / "agent")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import app.main as api_main  # noqa: E402
from app import agent_chat  # noqa: E402
from app.contracts import (  # noqa: E402
    AgentRegister,
    ArtifactCreate,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    ProjectCreate,
    ReviewCreate,
    TaskCreate,
)
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402
from device_test_support import registration_request  # noqa: E402

MEMBER = "member-001"
AGENT_ID = "agent-fm0"
DEVICE_ID = "device-fm0"
ARTIFACT_NAME = "题目原文.txt"
ARTIFACT_CONTENT = "问题一：请给出模型假设与变量说明。\n".encode("utf-8")
INPUT_PATH = f"inputs/{ARTIFACT_NAME}"
GRANT_CAPABILITIES = [
    "task.claim",
    "task.progress",
    "artifact.read",
    "artifact.write",
    "run.create",
    "run.event",
]
# 执行体命令：把提示词与 cwd 落盘（我们靠它证明"执行体真的看到了输入文件"，而不是只看函数返回值）。
# 写成**脚本文件**而不是 `python -c`：`--executor-command` 是 nargs="*"，argparse 遇到 `-X` 就停止收值，
# 用脚本文件可以让命令行里不出现以 `-` 开头的参数（更接近线上任务声明 worker_command 的样子）。
ECHO_SCRIPT_SOURCE = (
    "import os, pathlib, sys\n"
    "pathlib.Path('prompt.txt').write_text(sys.argv[1], encoding='utf-8')\n"
    "pathlib.Path('cwd.txt').write_text(os.getcwd(), encoding='utf-8')\n"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_api(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/organizations", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise SystemExit("临时 API 没起来：检查端口占用或依赖")


def kernel(*args: str) -> subprocess.CompletedProcess:
    command = [sys.executable, "-X", "utf8", str(ROOT / "apps" / "agent" / "agentd.py"), *args]
    return subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-0 输入物化端到端验收")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排障用）")
    options = parser.parse_args()

    root = Path(os.environ.get("FM0_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm0-verify-"))
    workspace = root / "ws"
    state_dir = root / "state"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    agent_chat.ensure_schema(store)
    echo_script = root / "echo_prompt.py"
    echo_script.write_text(ECHO_SCRIPT_SOURCE, encoding="utf-8")
    worker_command = [sys.executable, str(echo_script), "{prompt}"]
    port = free_port()
    api = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(api_main.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_for_api(api)

    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    # ---- 平台侧准备 ----------------------------------------------------------
    # 单独建一个项目：播种项目里本来就有一批 READY 任务，内核会先领到它们（那些任务没有 worker_command，
    # 会以 executor_not_configured 失败），验收就变成在测别的东西了
    project = store.create_project(ProjectCreate(name="FM-0 输入物化验收", competition_pack="cumcm-2026"))
    registration = kernel(
        # `--url` 是**全局**参数，必须写在子命令前面（子命令里只有 daemon-run 自带 --url）
        "--url", api,
        "register",
        "--agent-id", AGENT_ID,
        "--display-name", "FM-0 验证机",
        "--owner", MEMBER,
        "--workspace", str(workspace),
    )
    registered = store.db.execute("SELECT local_workspace FROM agents WHERE agent_id = ?", (AGENT_ID,)).fetchone()
    expected_workspace = str(workspace.expanduser().resolve())
    check(
        "内核 register 上报的工作区就是 --workspace 解析后的绝对路径",
        bool(registered) and registered["local_workspace"] == expected_workspace,
        f"注册={registered['local_workspace'] if registered else None} 期望={expected_workspace}",
    )
    if registration.returncode != 0:
        print(registration.stdout[-2000:], registration.stderr[-2000:])

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=uuid.UUID(DEV_ORG_ID)), MEMBER)
    store.register_device(
        registration_request(
            pairing,
            Ed25519PrivateKey.generate(),
            AGENT_ID,
            DEVICE_ID,
            device_name="FM-0 验证机",
            capabilities=GRANT_CAPABILITIES,
        )
    )
    grant = store.create_device_project_grant(
        project.id,
        # **不挑能力**：用平台默认集合（设备页授权走的就是它）。手工挑会漏掉 task.result / run.complete
        # 之类分散在各路由上的能力，跑起来就是"能领活、交不了活"的 403（这里踩过一次）。
        DeviceProjectGrantCreate(device_id=DEVICE_ID, expires_in_seconds=3600),
        MEMBER,
    )
    artifact = store.create_artifact(
        project.id,
        ArtifactCreate(
            name=ARTIFACT_NAME,
            artifact_type="problem_source",
            description="FM-0 验收输入",
            source_path=str(workspace / ARTIFACT_NAME),
            data_policy={"future_data": "deny"},
        ),
    )
    store.store_artifact_content(artifact.id, ARTIFACT_CONTENT, "text/plain")
    # 输入成果物必须先**审核通过**：`_task_dependencies_ready` 要求输入是 APPROVED 且 downstream_allowed，
    # 否则任务领不走（平台有意如此——执行体只能吃已审核的输入）。审核会让成果物变不可变，所以内容要在审核前传。
    store.create_review(
        project.id,
        ReviewCreate(
            target_type="artifact",
            target_id=artifact.id,
            verdict="APPROVED",
            summary="FM-0 验收输入确认",
            reviewer=MEMBER,
            reviewer_kind="member",
        ),
    )
    task = store.create_task(
        project.id,
        TaskCreate(
            title="FM-0 输入物化验收",
            description="把 inputs/ 里的文件念一遍即可。",
            stage="modeling",
            assignee=AGENT_ID,
            input_artifacts=[str(artifact.id)],
            acceptance_criteria=["inputs/ 里有输入文件"],
            resource_policy={
                "worker_executor": "cli",
                "worker_command": worker_command,
            },
        ),
    )
    artifacts_before = {str(item.id) for item in store.list_artifacts(project.id)}

    # ---- 真内核跑一轮 --------------------------------------------------------
    result = kernel(
        "--url", api,
        "worker-run",
        "--once",
        # worker-run **没有** --state-dir（那是 daemon-run/sidecar-run 的）：状态一律用 --state-path 指定，
        # 否则它会落到用户主目录的 ~/.math-agent-platform 里，污染开发机
        "--state-path", str(state_dir / "agentd.db"),
        "--workspace", str(workspace),
        "--project-id", str(project.id),
        "--project-token", grant.project_token,
        "--agent-id", AGENT_ID,
        "--device-id", DEVICE_ID,
        "--skip-credential",
        "--credential-backend", "file",
        "--collect-outputs",
    )
    print("---- worker-run stdout ----")
    print(result.stdout.strip()[-3000:])
    if result.returncode != 0:
        print(result.stderr.strip()[-2000:])

    # ---- 对账 ----------------------------------------------------------------
    landed = workspace / INPUT_PATH
    check("输入文件落到 <workspace>/inputs/", landed.is_file(), str(landed))
    if landed.is_file():
        check(
            "落地字节与平台侧成果物一致（含 hash）",
            landed.read_bytes() == ARTIFACT_CONTENT
            and hashlib.sha256(landed.read_bytes()).hexdigest() == hashlib.sha256(ARTIFACT_CONTENT).hexdigest(),
            f"{len(landed.read_bytes())} 字节",
        )

    manifest_path = workspace / ".math-agent-platform" / "inputs-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    entries = {entry.get("artifact_id"): entry for entry in manifest.get("entries") or []}
    entry = entries.get(str(artifact.id))
    check(
        "清单记录 artifact id → 相对路径 → sha256 → 字节数",
        bool(entry)
        and entry.get("relative_path") == INPUT_PATH
        and entry.get("sha256") == hashlib.sha256(ARTIFACT_CONTENT).hexdigest()
        and entry.get("size_bytes") == len(ARTIFACT_CONTENT),
        json.dumps(entry, ensure_ascii=False) if entry else f"清单里没有 {artifact.id}",
    )

    prompt_file = workspace / "prompt.txt"
    prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else ""
    check(
        "提示词列出输入文件且没有失败字样",
        bool(prompt) and ARTIFACT_NAME in prompt and "inputs/" in prompt and "处理失败" not in prompt,
        prompt.splitlines()[0] if prompt else "没写出提示词",
    )

    cwd_file = workspace / "cwd.txt"
    runner_cwd = cwd_file.read_text(encoding="utf-8").strip() if cwd_file.is_file() else ""
    check(
        "Runner 真正的 cwd == 注册上报的工作区",
        bool(runner_cwd) and Path(runner_cwd).resolve() == Path(expected_workspace),
        f"cwd={runner_cwd}",
    )

    uploaded = [item for item in store.list_artifacts(project.id) if str(item.id) not in artifacts_before]
    inputs_reuploaded = [
        item
        for item in uploaded
        if ARTIFACT_NAME in str(item.name) or INPUT_PATH.replace("/", os.sep) in str(item.source_path or "")
    ]
    check(
        "平台下发的输入没有被当成产出再传一份",
        not inputs_reuploaded,
        f"新增成果物 {[item.name for item in uploaded]}",
    )

    run_rows = store.db.execute("SELECT id, status FROM runs WHERE task_id = ?", (str(task.id),)).fetchall()
    check("任务在平台侧留下了运行记录", bool(run_rows), f"{[(row['id'], row['status']) for row in run_rows]}")

    completed = store.get_task(task.id)
    check("任务终态不是失败", str(completed.status) not in {"FAILED", "BLOCKED"}, f"status={completed.status}")

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
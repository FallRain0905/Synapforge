"""FM-0：带 `input_artifacts` 的任务必须**真的**把输入下到 `<workspace>/inputs/`。

为什么单独一个文件、而且要从 `_execute_task` 一路测到磁盘：这里修的是一个真 bug——
`_execute_task` 里引用了从未定义的 `loop_identity`（参数其实叫 `identity`），`NameError` 被
"输入取不到也要照常跑"的兜底 `except` 吃掉，于是**每一次**带输入的任务都在提示词里写
"输入文件处理失败"、`inputs/` 永远是空的。兜底不能掩盖 bug，所以这里不接受"函数返回了"
当证据，只认三样东西：磁盘上的字节与 hash、Runner 子进程真正的 cwd、子进程收到的提示词。

平台侧用一台真的本机 HTTP 服务当替身（不是 mock 掉 `materialize_inputs`）：下载链路里的
鉴权头、`Content-Disposition` 取名、失败清单都得跟着一起验。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentd  # noqa: E402
from input_fetcher import manifest_path, read_inputs_manifest  # noqa: E402

# 任务侧提示词里出现什么就算"执行体知道有这个文件"——这里故意用一个只回显提示词的执行体命令
ECHO_PROMPT_SCRIPT = (
    "import json, os, pathlib, sys;"
    "pathlib.Path('prompt.txt').write_text(sys.argv[1], encoding='utf-8');"
    "pathlib.Path('cwd.txt').write_text(os.getcwd(), encoding='utf-8')"
)


class _ArtifactServer:
    """替身平台：`GET /api/agent/artifacts/{id}/content`，带 Content-Disposition 与鉴权回显。"""

    def __init__(self, artifacts: dict[str, tuple[str, bytes]]) -> None:
        self.artifacts = artifacts
        self.seen_headers: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
                outer.seen_headers.append({key: value for key, value in self.headers.items()})
                artifact_id = self.path.rsplit("/", 2)[-2] if self.path.endswith("/content") else ""
                entry = outer.artifacts.get(artifact_id)
                if entry is None:
                    body = b'{"detail":"artifact_not_found"}'
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name, content = entry
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                # 与平台真实响应同形：中文名走 RFC 5987 的 `filename*`，前面那个 ASCII 兜底是**假名**。
                # 谁要是按顺序取第一个 `filename=`，中文名就会被换成兜底名——这条正是那个回归的哨兵。
                fallback = "artifact" + Path(name).suffix
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{urllib.parse.quote(name, safe="")}',
                )
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_: object) -> None:  # 测试里不要往 stderr 刷访问日志
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _args(workspace: Path, url: str, command: list[str]) -> object:
    return agentd.argparse.Namespace(
        url=url,
        workspace=str(workspace),
        state_path=str(workspace / "agentd.db"),
        executor_command=None,
        codex_path=None,
        task_timeout=60.0,
        process_stop_timeout=5.0,
    )


def _task(artifact_ids: list[str], command: list[str]) -> dict:
    return {
        "id": "task-inputs-1",
        "title": "改这份文档",
        "description": "",
        "input_artifacts": artifact_ids,
        "resource_policy": {"worker_executor": "cli", "worker_command": command},
    }


class InputMaterializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Windows 上 SQLite 会占住 agentd.db：清理时忽略占用，免得测试因"临时目录删不掉"变红
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.temp_dir.name) / "ws"
        self.workspace.mkdir(parents=True)
        self.command = [sys.executable, "-X", "utf8", "-c", ECHO_PROMPT_SCRIPT, "{prompt}"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _run(self, server: _ArtifactServer, artifact_ids: list[str]) -> tuple[bool, str, str, str]:
        args = _args(self.workspace, server.url, self.command)
        identity = {
            "project_id": "project-1",
            "agent_id": "agent-fm0",
            "device_id": "device-fm0",
            "project_token": "project-token-fm0",
            "capabilities": [],
        }
        return await agentd._execute_task(_task(artifact_ids, self.command), identity, args)

    async def test_inputs_land_on_disk_with_matching_hash_and_prompt(self) -> None:
        content = "问题一：请给出模型假设。\n".encode("utf-8")
        server = _ArtifactServer({"artifact-aaaa1111": ("题目原文.txt", content)})
        try:
            success, summary, _, _ = await self._run(server, ["artifact-aaaa1111"])
        finally:
            server.close()

        self.assertTrue(success, summary)
        landed = self.workspace / "inputs" / "题目原文.txt"
        self.assertTrue(landed.is_file(), f"输入没落到 inputs/：{list((self.workspace / 'inputs').glob('*'))}")
        self.assertEqual(landed.read_bytes(), content)
        # 文件名必须是真名而不是响应头里的 ASCII 兜底（artifact.txt）
        self.assertFalse((self.workspace / "inputs" / "artifact.txt").exists())
        # 鉴权：下载必须带项目能力令牌与 Agent 标识（否则平台侧会 401/403，线上就是"取不到文件"）
        self.assertTrue(server.seen_headers)
        self.assertEqual(server.seen_headers[0].get("X-Project-Capability-Token"), "project-token-fm0")
        self.assertEqual(server.seen_headers[0].get("X-Agent-Id"), "agent-fm0")

        # 提示词：执行体得知道去哪儿看（这条在 bug 期间一直是"处理失败"）
        prompt = (self.workspace / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("inputs/", prompt)
        self.assertIn("题目原文.txt", prompt)
        self.assertNotIn("处理失败", prompt)

        # 清单对账：artifact id → 相对路径 → sha256 → 字节数
        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual(manifest["schema"], "inputs-manifest/1")
        self.assertEqual(len(manifest["entries"]), 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["artifact_id"], "artifact-aaaa1111")
        self.assertEqual(entry["relative_path"], "inputs/题目原文.txt")
        self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["size_bytes"], len(content))
        self.assertEqual(manifest["failures"], [])

    async def test_runner_cwd_is_the_registered_workspace(self) -> None:
        """注册上报的 `local_workspace` 必须就是 Runner 真正用的 cwd（否则平台对的是另一个目录）。"""

        server = _ArtifactServer({})
        args = _args(self.workspace, server.url, self.command)
        registered: dict = {}
        original_request = agentd.request
        agentd.request = lambda url, method, path, payload=None, headers=None: registered.update(payload or {}) or {}
        try:
            agentd.register(
                agentd.argparse.Namespace(
                    url=server.url,
                    agent_id="agent-fm0",
                    display_name="FM0 Agent",
                    owner="member-001",
                    provider="local",
                    model="unspecified",
                    tools=[],
                    languages=["python"],
                    capability_card=None,
                    executor_kind="",
                    executor_version="",
                    workspace=str(self.workspace),
                )
            )
            await self._run(server, [])
        finally:
            agentd.request = original_request
            server.close()

        runner_cwd = (self.workspace / "cwd.txt").read_text(encoding="utf-8")
        self.assertEqual(Path(runner_cwd).resolve(), Path(registered["local_workspace"]).resolve())
        self.assertEqual(Path(registered["local_workspace"]).resolve(), self.workspace.resolve())

    async def test_missing_input_is_reported_not_faked(self) -> None:
        content = b"ok"
        server = _ArtifactServer({"artifact-bbbb2222": ("good.csv", content)})
        try:
            success, summary, _, _ = await self._run(server, ["artifact-bbbb2222", "artifact-gone9999"])
        finally:
            server.close()

        self.assertTrue(success, summary)
        self.assertTrue((self.workspace / "inputs" / "good.csv").is_file())
        # 失败的那份**不许**凭空出现，也不许安静跳过：提示词与清单都要如实说
        self.assertFalse((self.workspace / "inputs" / "artifact-gone9999").exists())
        prompt = (self.workspace / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("没有取到", prompt)
        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertTrue(manifest["failures"])
        self.assertIn("404", manifest["failures"][0])

    async def test_manifest_keeps_previous_entries_and_drops_deleted_files(self) -> None:
        """清单是台账：上一轮的还在（文件也还在），被手工删掉的不留幽灵条目。"""

        first = _ArtifactServer({"artifact-cccc3333": ("a.txt", b"a")})
        try:
            await self._run(first, ["artifact-cccc3333"])
        finally:
            first.close()
        second = _ArtifactServer({"artifact-dddd4444": ("b.txt", b"b")})
        try:
            await self._run(second, ["artifact-dddd4444"])
        finally:
            second.close()

        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual({entry["name"] for entry in manifest["entries"]}, {"a.txt", "b.txt"})

        (self.workspace / "inputs" / "a.txt").unlink()
        third = _ArtifactServer({"artifact-eeee5555": ("c.txt", b"c")})
        try:
            await self._run(third, ["artifact-eeee5555"])
        finally:
            third.close()
        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual({entry["name"] for entry in manifest["entries"]}, {"b.txt", "c.txt"})

    async def test_manifest_is_written_atomically_next_to_run_metadata(self) -> None:
        """清单落在平台自己的元数据目录里，两个扫描器都不把它当产出（也不会污染 inputs/）。"""

        server = _ArtifactServer({"artifact-ffff6666": ("d.txt", b"d")})
        try:
            await self._run(server, ["artifact-ffff6666"])
        finally:
            server.close()

        path = manifest_path(self.workspace)
        self.assertEqual(path.parent.name, ".math-agent-platform")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["directory"], "inputs")
        self.assertFalse(list((self.workspace / "inputs").glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
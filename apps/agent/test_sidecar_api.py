"""Sidecar 契约（v1）的契约测试。

不打真实平台：用注入的 `agents_probe` / `pair_handler` 替换外部依赖，
断言的是**契约本身**——绑定地址、鉴权、状态形状、错误码与幂等语义。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "apps" / "agent"))

import sidecar_api  # noqa: E402
from sidecar_api import (  # noqa: E402
    CONTRACT_VERSION,
    decode_pairing_blob,
    default_pair_handler,
    SidecarError,
    SidecarService,
    SidecarState,
    default_state_dir,
    host_slug,
    sidecar_info_path,
)


def _fake_probe() -> list[dict]:
    return [
        {"adapter_id": "codex-cli", "state": "AVAILABLE", "version": "0.154.0-alpha.6.2",
         "executable": r"C:\x\codex.exe", "checked_at": "2026-09-16T00:00:00+00:00"},
        {"adapter_id": "claude-code", "state": "NOT_INSTALLED", "version": None,
         "executable": "claude", "checked_at": "2026-09-16T00:00:00+00:00"},
    ]


def _fake_pair(payload: dict) -> dict:
    return {
        "paired": True,
        "device_id": payload.get("device_id") or "device-test",
        "agent_id": payload.get("agent_id") or "agent-test",
        "public_key_fingerprint": "f" * 64,
        "credential_stored": True,
        "codex_path": None,
        "message": None,
    }


class SidecarHttpTests(unittest.TestCase):
    """真起 HTTP 服务（回环）验证传输层契约。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.service = SidecarService(
            state_dir=Path(self.temp.name),
            agents_probe=_fake_probe,
            pair_handler=_fake_pair,
        )
        self.service.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.token = self.service.token

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def _get(self, path: str, token: str | None = None):
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=5)

    def _post(self, path: str, payload: dict | None = None, token: str | None = None):
        data = json.dumps(payload or {}).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=5)

    def _expect_error(self, fn, status: int) -> dict:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            fn()
        self.assertEqual(caught.exception.code, status)
        return json.loads(caught.exception.read().decode())

    # ---- 绑定与握手 -----------------------------------------------------

    def test_listens_on_loopback_only(self) -> None:
        import socket

        # 能用回环连上
        with socket.create_connection(("127.0.0.1", self.service.port), timeout=3):
            pass
        # 本机非回环地址上不应监听（拿本机内网 IP 试；拿不到就跳过）
        hostname_ip = socket.gethostbyname(socket.gethostname())
        if hostname_ip.startswith("127."):
            self.skipTest("本机无独立内网地址，跳过非回环监听检查")
        with self.assertRaises(OSError):
            socket.create_connection((hostname_ip, self.service.port), timeout=2)

    def test_health_needs_no_token_and_leaks_nothing(self) -> None:
        payload = json.loads(self._get("/health").read())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["contract"], CONTRACT_VERSION)
        # 不得泄露令牌、身份或平台地址
        self.assertNotIn("token", json.dumps(payload))
        self.assertNotIn("device_id", payload)

    def test_info_file_written_with_contract_and_port(self) -> None:
        info = json.loads(sidecar_info_path(Path(self.temp.name)).read_text(encoding="utf-8"))
        self.assertEqual(info["port"], self.service.port)
        self.assertEqual(info["contract"], CONTRACT_VERSION)
        self.assertEqual(info["pid"], os.getpid())
        if os.name != "nt":
            self.assertEqual(Path(sidecar_info_path(Path(self.temp.name))).stat().st_mode & 0o777, 0o600)

    # ---- 鉴权 -----------------------------------------------------------

    def test_protected_endpoints_require_bearer(self) -> None:
        for path in ("/status", "/logs"):
            with self.subTest(path=path):
                payload = self._expect_error(lambda p=path: self._get(p), 401)
                self.assertEqual(payload["error"], "sidecar_unauthorized")

    def test_wrong_token_is_rejected(self) -> None:
        payload = self._expect_error(lambda: self._get("/status", token="wrong"), 401)
        self.assertEqual(payload["error"], "sidecar_unauthorized")

    # ---- 状态形状 -------------------------------------------------------

    def test_status_shape_matches_contract(self) -> None:
        payload = json.loads(self._get("/status", token=self.token).read())
        for key in ("connection", "identity", "tasks", "queue", "local_agents", "emergency_stop", "contract"):
            self.assertIn(key, payload)
        self.assertEqual(payload["contract"], CONTRACT_VERSION)
        self.assertEqual(payload["connection"]["state"], "disconnected")
        self.assertEqual(payload["tasks"]["paused"], False)
        self.assertEqual(payload["local_agents"][0]["adapter_id"], "codex-cli")
        self.assertIn(payload["local_agents"][1]["state"], {"AVAILABLE", "NOT_INSTALLED", "UNSUPPORTED", "ERROR"})

    def test_status_reflects_state_updates(self) -> None:
        self.service.state.update(connection_state="connected", device_id="device-x", agent_id="agent-x")
        payload = json.loads(self._get("/status", token=self.token).read())
        self.assertEqual(payload["connection"]["state"], "connected")
        self.assertEqual(payload["identity"]["device_id"], "device-x")

    # ---- 配对 -----------------------------------------------------------

    def test_pair_validates_platform_url_and_blob(self) -> None:
        bad_url = self._expect_error(
            lambda: self._post("/pair", {"platform_url": "ftp://x", "pairing_blob": "abc"}, self.token), 400
        )
        self.assertEqual(bad_url["error"], "sidecar_platform_url_invalid")

        bad_blob = self._expect_error(
            lambda: self._post("/pair", {"platform_url": "http://127.0.0.1:8000"}, self.token), 400
        )
        self.assertEqual(bad_blob["error"], "sidecar_pairing_blob_invalid")

    def test_pair_success_updates_identity(self) -> None:
        payload = json.loads(
            self._post("/pair", {"platform_url": "http://127.0.0.1:8000", "pairing_blob": "blob",
                                 "device_id": "device-p", "agent_id": "agent-p"}, self.token).read()
        )
        self.assertTrue(payload["paired"])
        status = json.loads(self._get("/status", token=self.token).read())
        self.assertEqual(status["identity"]["device_id"], "device-p")
        self.assertEqual(status["identity"]["platform_url"], "http://127.0.0.1:8000")
        self.assertIsNotNone(status["identity"]["paired_at"])

    def test_pair_does_not_expose_token_in_logs(self) -> None:
        self._post("/pair", {"platform_url": "http://127.0.0.1:8000", "pairing_blob": "secret-blob-value"}, self.token)
        logs = json.loads(self._get("/logs", token=self.token).read())["lines"]
        joined = "\n".join(logs)
        self.assertNotIn("secret-blob-value", joined)   # 配对串不进日志
        self.assertNotIn(self.token, joined)            # 启动令牌不进日志

    # ---- 暂停 / 紧急停止 / 日志 / 关闭 -----------------------------------

    def test_pause_resume_toggles_state(self) -> None:
        self.assertTrue(json.loads(self._post("/tasks/pause", {}, self.token).read())["paused"])
        self.assertTrue(json.loads(self._get("/status", token=self.token).read())["tasks"]["paused"])
        self.assertFalse(json.loads(self._post("/tasks/resume", {}, self.token).read())["paused"])

    def test_emergency_stop_toggles_state(self) -> None:
        self.assertTrue(json.loads(self._post("/emergency-stop", {}, self.token).read())["emergency_stop"])
        self.assertTrue(json.loads(self._get("/status", token=self.token).read())["emergency_stop"])
        self.assertFalse(json.loads(self._post("/clear-emergency-stop", {}, self.token).read())["emergency_stop"])

    def test_logs_tail_is_clamped(self) -> None:
        for index in range(10):
            self.service.logs.append(f"line-{index}")
        payload = json.loads(self._get("/logs?tail=3", token=self.token).read())
        self.assertEqual(payload["lines"], ["line-7", "line-8", "line-9"])
        huge = json.loads(self._get("/logs?tail=99999", token=self.token).read())
        self.assertEqual(len(huge["lines"]), 10)

    def test_rescan_returns_agent_inventory(self) -> None:
        payload = json.loads(self._post("/agents/rescan", {}, self.token).read())
        self.assertEqual(len(payload["local_agents"]), 2)

    def test_unknown_path_is_404_with_stable_code(self) -> None:
        payload = self._expect_error(lambda: self._get("/nope", token=self.token), 404)
        self.assertEqual(payload["error"], "sidecar_not_found")

    def test_shutdown_endpoint_responds_then_stops_server(self) -> None:
        payload = json.loads(self._post("/shutdown", {}, self.token).read())
        self.assertTrue(payload["shutting_down"])
        # 给停止线程一点时间
        import time

        for _ in range(50):
            if self.service._server is None:  # noqa: SLF001 - 测试观察生命周期
                break
            time.sleep(0.1)
        self.assertIsNone(self.service._server)


class SidecarHookTests(unittest.TestCase):
    """DP-2-04：托盘动作必须真的传到常驻体（契约端只改状态是不够的）。"""

    def _service(self, rescan_calls: list[int]) -> SidecarService:
        def rescan() -> list[dict]:
            rescan_calls.append(1)
            return _fake_probe()

        return SidecarService(
            state_dir=Path(tempfile.gettempdir()) / "map-sidecar-hooks",
            agents_probe=_fake_probe,
            agents_rescan=rescan,
        )

    def test_rescan_forces_a_real_probe_while_status_uses_the_cache(self) -> None:
        rescan_calls: list[int] = []
        service = self._service(rescan_calls)
        self.assertEqual(len(service.status()["local_agents"]), 2)
        self.assertEqual(rescan_calls, [])
        service.rescan_agents()
        self.assertEqual(len(rescan_calls), 1)

    def test_pause_and_emergency_stop_notify_the_resident_body(self) -> None:
        service = self._service([])
        seen: list[tuple[str, bool]] = []
        service.on_pause = lambda paused: seen.append(("pause", paused))
        service.on_emergency_stop = lambda active: seen.append(("emergency", active))

        service.set_paused(True)
        service.set_emergency_stop(True)
        service.set_emergency_stop(False)
        service.set_paused(False)

        self.assertEqual(seen, [("pause", True), ("emergency", True), ("emergency", False), ("pause", False)])
        self.assertFalse(service.state.paused)
        self.assertFalse(service.state.emergency_stop)

    def test_hooks_are_optional(self) -> None:
        """没有常驻体时（sidecar-run），暂停/紧急停止只改契约快照，不能抛异常。"""

        service = self._service([])
        self.assertIsNone(service.on_pause)
        self.assertEqual(service.set_paused(True), {"paused": True})
        self.assertEqual(service.set_emergency_stop(True), {"emergency_stop": True})

    def test_running_flag_reflects_the_server_lifecycle(self) -> None:
        service = SidecarService(state_dir=Path(tempfile.gettempdir()) / "map-sidecar-running", agents_probe=_fake_probe)
        self.assertFalse(service.running)
        service.start()
        try:
            self.assertTrue(service.running)
        finally:
            service.stop()
        self.assertFalse(service.running)


class SidecarGrantTests(unittest.TestCase):
    """契约 v1 追加端点 `/grant`（DP-2-08）：应用项目授权串。"""

    def _service(self, handler=None) -> SidecarService:
        return SidecarService(
            state_dir=Path(tempfile.gettempdir()) / "map-sidecar-grant",
            agents_probe=_fake_probe,
            grant_handler=handler,
        )

    def test_grant_requires_a_platform_url_and_blob(self) -> None:
        service = self._service(lambda payload: {"granted": True})
        with self.assertRaises(SidecarError) as no_url:
            service.grant({"grant_blob": "x"})
        self.assertEqual(no_url.exception.code, "sidecar_platform_url_invalid")
        with self.assertRaises(SidecarError) as no_blob:
            service.grant({"platform_url": "http://127.0.0.1:8010"})
        self.assertEqual(no_blob.exception.code, "sidecar_grant_blob_invalid")

    def test_grant_without_a_resident_body_reports_503_not_success(self) -> None:
        service = self._service(None)
        with self.assertRaises(SidecarError) as caught:
            service.grant({"platform_url": "http://127.0.0.1:8010", "grant_blob": "blob"})
        self.assertEqual(caught.exception.code, "sidecar_grant_unavailable")
        self.assertEqual(caught.exception.status, 503)

    def test_grant_delegates_to_the_resident_body_and_logs_without_the_blob(self) -> None:
        seen: list[dict] = []

        def handler(payload: dict) -> dict:
            seen.append(payload)
            return {"granted": True, "project_id": "p-1", "device_id": "device-test", "task_loop": "running"}

        service = self._service(handler)
        result = service.grant({"platform_url": "http://127.0.0.1:8010", "grant_blob": "secret-blob"})
        self.assertTrue(result["granted"])
        self.assertEqual(seen[0]["platform_url"], "http://127.0.0.1:8010")
        self.assertEqual(seen[0]["grant_blob"], "secret-blob")
        # 授权串是短期凭证：日志里只能出现项目标识，不能出现串本身
        self.assertTrue(service.logs)
        self.assertFalse(any("secret-blob" in line for line in service.logs))

    def test_invalid_blob_maps_to_a_stable_error_code(self) -> None:
        def handler(payload: dict) -> dict:
            raise ValueError("worker_grant_missing_project_token")

        service = self._service(handler)
        with self.assertRaises(SidecarError) as caught:
            service.grant({"platform_url": "http://127.0.0.1:8010", "grant_blob": "bad"})
        self.assertEqual(caught.exception.code, "sidecar_grant_blob_invalid")
        self.assertIn("worker_grant_missing_project_token", caught.exception.detail)


class PlatformInfoTests(unittest.TestCase):
    """配对结果落盘：重启后内核能自己找回平台地址与设备标识（Token 不落盘）。"""

    def test_pair_writes_platform_info_and_never_the_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            state_dir = Path(temp)
            service = SidecarService(
                state_dir=state_dir,
                agents_probe=_fake_probe,
                pair_handler=lambda payload: {
                    "paired": True,
                    "device_id": "device-info",
                    "agent_id": "agent-info",
                    "device_token": "dvc_should_never_be_written",
                },
            )
            service.pair({"platform_url": "http://127.0.0.1:8010", "pairing_blob": "blob"})
            payload = sidecar_api.read_platform_info(state_dir)
            self.assertEqual(payload["url"], "http://127.0.0.1:8010")
            self.assertEqual(payload["device_id"], "device-info")
            self.assertEqual(payload["agent_id"], "agent-info")
            raw = (state_dir / "platform.json").read_text(encoding="utf-8")
            self.assertNotIn("dvc_", raw)

    def test_missing_platform_info_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            self.assertEqual(sidecar_api.read_platform_info(Path(temp)), {})


class SidecarStateTests(unittest.TestCase):
    def test_state_rejects_unknown_field(self) -> None:
        state = SidecarState()
        with self.assertRaises(ValueError):
            state.update(nonexistent_field=True)

    def test_default_state_dir_is_localappdata_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("仅 Windows 适用")
        expected = Path(os.environ["LOCALAPPDATA"]) / "MathAgentPlatform"
        self.assertEqual(default_state_dir(), expected)

    def test_state_dir_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"MAP_STATE_DIR": str(Path(tempfile.gettempdir()) / "map-x")}):
            self.assertEqual(default_state_dir(), Path(tempfile.gettempdir()) / "map-x")

    def test_host_slug_matches_script_convention(self) -> None:
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "My-PC_01"}):
            self.assertEqual(host_slug(), "my-pc-01")


class SidecarPairHandlerTests(unittest.TestCase):
    def test_rejects_empty_payload(self) -> None:
        service = SidecarService(state_dir=Path(tempfile.gettempdir()) / "map-sidecar-test")
        with self.assertRaises(SidecarError) as caught:
            service.pair({"platform_url": "http://127.0.0.1:8000", "pairing_blob": ""})
        self.assertEqual(caught.exception.code, "sidecar_pairing_blob_invalid")



class PairingBlobTests(unittest.TestCase):
    """配对串解码：合法 / 缺字段 / 非法 base64 / 已过期 / 无 expires_at。"""

    @staticmethod
    def _blob(**fields) -> str:
        import base64

        payload = {"pairing_id": "p-1", "pairing_code": "map_x", "challenge": "-abc_123"}
        payload.update(fields)
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    def test_valid_blob_is_decoded(self) -> None:
        payload = decode_pairing_blob(self._blob())
        self.assertEqual(payload["pairing_id"], "p-1")
        self.assertEqual(payload["challenge"], "-abc_123")

    def test_blob_without_expiry_is_accepted(self) -> None:
        self.assertEqual(decode_pairing_blob(self._blob())["pairing_code"], "map_x")

    def test_missing_field_is_rejected(self) -> None:
        with self.assertRaises(SidecarError) as caught:
            decode_pairing_blob(self._blob(challenge=""))
        self.assertEqual(caught.exception.code, "sidecar_pairing_blob_invalid")
        self.assertIn("missing_challenge", caught.exception.detail)

    def test_malformed_base64_is_rejected(self) -> None:
        with self.assertRaises(SidecarError) as caught:
            decode_pairing_blob("!!!not-base64!!!")
        self.assertEqual(caught.exception.code, "sidecar_pairing_blob_invalid")

    def test_expired_blob_is_rejected(self) -> None:
        with self.assertRaises(SidecarError) as caught:
            decode_pairing_blob(self._blob(expires_at="2020-01-01T00:00:00Z"))
        self.assertEqual(caught.exception.code, "sidecar_pairing_expired")

    def test_future_expiry_is_accepted(self) -> None:
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        self.assertEqual(decode_pairing_blob(self._blob(expires_at=future))["pairing_id"], "p-1")


class PairRetryTests(unittest.TestCase):
    """设备标识冲突（一台机器只能注册一次）时必须自动换标识重试，并如实回报最终标识。"""

    def setUp(self) -> None:
        import agentd  # 同目录模块

        self.agentd = agentd
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state_dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, register_outcomes):
        """register_outcomes：按顺序决定每次 /api/devices/register 的结果。"""
        import base64

        import agentd

        calls: list[str] = []

        def fake_request(_url, _method, path, payload=None, **_kwargs):
            if path == "/api/agents/register":
                return {}
            if path == "/api/devices/register":
                calls.append(payload["device_id"])
                outcome = register_outcomes[len(calls) - 1]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return {}

        def fake_keygen(args):
            Path(args.directory).mkdir(parents=True, exist_ok=True)
            (Path(args.directory) / f"{args.name}.key").write_bytes(b"stub")

        def fake_payload(register_args, _key_path):
            # 从实参里取出本次真正使用的 device_id（替身不能把它藏起来，否则测不到"换标识重试"）
            args = list(register_args)
            device_id = args[args.index("--device-id") + 1]
            return {"device_id": device_id, "stub": True}

        blob = base64.urlsafe_b64encode(
            json.dumps({"pairing_id": "p-1", "pairing_code": "map_x", "challenge": "c"}).encode()
        ).decode().rstrip("=")

        with mock.patch.dict("os.environ", {"MAP_STATE_DIR": str(self.state_dir)}), \
             mock.patch.object(agentd, "request", fake_request), \
             mock.patch.object(agentd, "keygen", fake_keygen), \
             mock.patch.object(agentd, "detect_codex_cli", return_value=None), \
             mock.patch.object(sidecar_api, "_device_register_payload", side_effect=fake_payload), \
             mock.patch.object(agentd, "WindowsCredentialManager") as credentials:
            credentials.return_value.put.return_value = None
            result = default_pair_handler({
                "platform_url": "http://127.0.0.1:8000",
                "pairing_blob": blob,
                "device_id": "device-host",
                "agent_id": "agent-host",
                "codex": False,
            })
        return result, calls

    def test_conflict_triggers_suffixed_retry(self) -> None:
        conflict = ValueError('http_409:{"detail":"device_id_already_registered"}')
        result, calls = self._run([conflict, {"device": {"public_key_fingerprint": "f" * 64}, "device_token": "dvc_x"}])

        self.assertEqual(len(calls), 2, "应重试一次")
        self.assertEqual(calls[0], "device-host")
        self.assertTrue(calls[1].startswith("device-host-"), calls[1])
        self.assertEqual(result["device_id"], calls[1], "响应必须回报最终使用的标识")
        self.assertTrue(result["paired"])

    def test_other_errors_are_not_retried(self) -> None:
        other = ValueError('http_409:{"detail":"device_public_key_already_registered"}')
        with self.assertRaises(ValueError):
            self._run([other])

    def test_success_path_calls_register_once(self) -> None:
        result, calls = self._run([{"device": {"public_key_fingerprint": "f" * 64}, "device_token": "dvc_x"}])
        self.assertEqual(calls, ["device-host"])
        self.assertEqual(result["device_id"], "device-host")
        self.assertTrue(result["credential_stored"])

if __name__ == "__main__":
    unittest.main()
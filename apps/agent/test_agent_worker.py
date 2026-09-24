"""UX-5 `agentd worker-run` 的契约测试。

覆盖不依赖真实平台的部分：授权串解析、身份解析（含凭据与 device_id 校验）、
任务级命令选择优先级、以及"没有声明命令就显式失败"这条不假装成功的约定。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import agentd


def _grant_blob(**overrides) -> str:
    payload = {
        "project_id": "11111111-1111-4111-8111-111111111111",
        "project_token": "prj_testtoken",
        "capabilities": ["task.claim", "task.progress"],
        "agent_id": "agent-ux5",
        "device_id": "device-ux5",
        "expires_at": "2026-12-31T00:00:00Z",
    }
    payload.update(overrides)
    return base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")


class _FakeCredentials:
    """替代 WindowsCredentialManager：记录写入，内存读取。"""

    stored: dict[str, str] = {}

    def put(self, target: str, secret: str) -> None:
        type(self).stored[target] = secret

    def get(self, target: str) -> str:
        if target not in type(self).stored:
            raise RuntimeError("credential_not_found")
        return type(self).stored[target]


def _args(**overrides) -> argparse.Namespace:
    base = {
        "url": "http://127.0.0.1:9999",
        "grant": None,
        "project_id": None,
        "project_token": None,
        "agent_id": None,
        "device_id": None,
        "workspace": ".",
        "stages": [],
        "lease_seconds": 900,
        "idle_seconds": 1.0,
        "max_idle_seconds": 2.0,
        "task_timeout": 30.0,
        "executor_command": None,
        "process_stop_timeout": 5.0,
        "state_path": "",
        "skip_credential": False,  # 与生产一致：首次接入要把 token 写入凭据存储
        "once": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class WorkerGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeCredentials.stored = {}
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state_path = str(Path(self.temp_dir.name) / "agentd.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_decode_grant_round_trip(self) -> None:
        payload = agentd._decode_grant(_grant_blob())
        self.assertEqual(payload["agent_id"], "agent-ux5")
        self.assertEqual(payload["project_token"], "prj_testtoken")

    def test_decode_grant_rejects_malformed_and_incomplete(self) -> None:
        with self.assertRaises(ValueError) as malformed:
            agentd._decode_grant("not-a-base64-blob!!")
        self.assertIn("worker_grant_invalid", str(malformed.exception))

        with self.assertRaises(ValueError) as missing:
            agentd._decode_grant(_grant_blob(project_token=""))
        self.assertIn("worker_grant_missing_project_token", str(missing.exception))

    def test_first_run_persists_config_and_token(self) -> None:
        args = _args(grant=_grant_blob(), state_path=self.state_path)
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            identity = agentd._resolve_worker_identity(args)

        self.assertEqual(identity["agent_id"], "agent-ux5")
        self.assertEqual(identity["device_id"], "device-ux5")
        self.assertEqual(identity["project_token"], "prj_testtoken")
        # Token 进凭据管理器，不落本地配置文件
        config_text = Path(identity["config_path"]).read_text(encoding="utf-8")
        self.assertIn("agent-ux5", config_text)
        self.assertNotIn("prj_testtoken", config_text)
        self.assertIn("MathAgentPlatform/project-token/11111111-1111-4111-8111-111111111111", _FakeCredentials.stored)

    def test_second_run_reads_config_and_credential_without_grant(self) -> None:
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            agentd._resolve_worker_identity(_args(grant=_grant_blob(), state_path=self.state_path))
            identity = agentd._resolve_worker_identity(_args(state_path=self.state_path))

        self.assertEqual(identity["agent_id"], "agent-ux5")
        self.assertEqual(identity["device_id"], "device-ux5")
        self.assertEqual(identity["project_token"], "prj_testtoken")

    def test_missing_credential_gives_actionable_error(self) -> None:
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            agentd._resolve_worker_identity(_args(grant=_grant_blob(), state_path=self.state_path))
        _FakeCredentials.stored = {}
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            with self.assertRaises(ValueError) as raised:
                agentd._resolve_worker_identity(_args(state_path=self.state_path))
        self.assertIn("worker_project_token_missing", str(raised.exception))
        self.assertIn("--grant", str(raised.exception))

    def test_requires_device_id_for_runner(self) -> None:
        """LocalRunner 需要 device_id；授权串里没有就要显式报错，而不是执行时才炸。"""

        blob = _grant_blob(device_id=None)
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            with self.assertRaises(ValueError) as raised:
                # 显式给出 token，让流程走到 device_id 校验这一步
                agentd._resolve_worker_identity(_args(grant=blob, project_token="prj_x", state_path=self.state_path))
        self.assertIn("worker_device_required", str(raised.exception))

    def test_explicit_capabilities_survive_config(self) -> None:
        with patch.object(agentd, "WindowsCredentialManager", _FakeCredentials):
            agentd._resolve_worker_identity(_args(grant=_grant_blob(), state_path=self.state_path))
            identity = agentd._resolve_worker_identity(_args(state_path=self.state_path))
        self.assertEqual(identity["capabilities"], ["task.claim", "task.progress"])


class WorkerExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.args = _args(state_path=str(Path(self.temp_dir.name) / "agentd.db"), workspace=str(_REPOSITORY_ROOT))
        self.identity = {"project_id": "p", "agent_id": "a", "device_id": "d", "capabilities": []}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_task_command_prefers_task_level_over_worker_default(self) -> None:
        task = {"resource_policy": {"worker_command": ["python", "-c", "print(1)"]}}
        self.assertEqual(agentd._task_command(task, ["fallback"]), ["python", "-c", "print(1)"])
        self.assertEqual(agentd._task_command({"resource_policy": {}}, ["fallback"]), ["fallback"])
        self.assertIsNone(agentd._task_command({"resource_policy": {}}, None))
        # 字符串形态与列表形态都接受
        self.assertEqual(agentd._task_command({"resource_policy": {"worker_command": "echo hi"}}, None), ["echo hi"])

    def test_unsupported_task_fails_explicitly(self) -> None:
        """没有声明命令时必须是失败，且原因写清楚——不能假装成功。"""

        success, summary, stdout, stderr = asyncio.run(agentd._execute_task({"id": "t1", "title": "无命令"}, self.identity, self.args))
        self.assertFalse(success)
        self.assertIn("executor_not_configured", summary)

    def test_declared_command_is_really_executed(self) -> None:
        task = {
            "id": "t2",
            "title": "声明式命令",
            "resource_policy": {"worker_command": [sys.executable, "-c", "print('worker-marker')"]},
        }
        success, summary, stdout, stderr = asyncio.run(agentd._execute_task(task, self.identity, self.args))
        self.assertTrue(success, summary)
        self.assertIn("exit=0", summary)
        self.assertIn("worker-marker", summary)
        # stdout 单独返回：Run 记录会把它写进运行台账
        self.assertIn("worker-marker", stdout)

    def test_failing_command_reports_failure_with_exit_code(self) -> None:
        task = {
            "id": "t3",
            "title": "失败命令",
            "resource_policy": {"worker_command": [sys.executable, "-c", "import sys; sys.exit(3)"]},
        }
        success, summary, stdout, stderr = asyncio.run(agentd._execute_task(task, self.identity, self.args))
        self.assertFalse(success)
        self.assertIn("exit=3", summary)


class WorkerCliTests(unittest.TestCase):
    def test_worker_run_is_registered(self) -> None:
        source = Path(agentd.__file__).read_text(encoding="utf-8")
        self.assertIn('sub.add_parser("worker-run"', source)
        self.assertIn("worker_parser.set_defaults(func=worker_run)", source)


if __name__ == "__main__":
    unittest.main()
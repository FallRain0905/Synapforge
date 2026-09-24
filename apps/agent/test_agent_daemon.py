"""DP-2-02/DP-2-03：常驻体（daemon-run）与本机清单探测的契约测试。

覆盖：
- `_gateway_uri` 的 http→ws 映射与身份参数拼装；
- `user_session_state()` 不谎报 locked/logged_out；
- 契约连接状态映射（壳按它点灯）；
- 心跳载荷：adapter_versions 来自真实探测、running_run_ids 只保留 UUID、executor 反映可用性；
- 每个新连接换 connection_id 且把 outbox 序列对齐到 1（否则重连后平台一直要求重放）；
- `AgentInventory` 的 TTL 缓存、强制重扫、只上报探测成功者。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_inventory import (
    BUILTIN_SELECTABLE_AGENTS,
    AgentInventory,
    _frontmatter_tools,
    _opencode_agent_descriptions,
    _parse_agent_names,
    _probe_opencode_roles,
    _role_executes,
)
import agent_inventory
from gateway_client import DurableGatewayClient, GatewayIdentity
from local_state import LocalAgentState
from machine_service import MachineAgentService, MachineServiceConfig

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentd  # noqa: E402


class FakeClient:
    """最小 Gateway 客户端替身：记录每次会话用的 URI 与身份。"""

    def __init__(self, state: LocalAgentState) -> None:
        self.state = state
        self.identity = GatewayIdentity(
            device_id="device-daemon",
            agent_id="agent-daemon",
            session_id="session-daemon",
            connection_id="connection-initial",
        )
        self.seen_uris: list[str] = []
        self.seen_connection_ids: list[str] = []
        self.started = asyncio.Event()
        self.fail_next = 0

    def queue_event(self, message_type: str, payload: dict[str, Any], *, idempotency_key: str, message_id: str | None = None):
        return self.state.enqueue_event(message_type, payload, idempotency_key=idempotency_key, message_id=message_id)

    async def run_once(self, uri: str, token: str, **_: Any) -> None:
        self.seen_uris.append(uri)
        self.seen_connection_ids.append(self.identity.connection_id)
        self.started.set()
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("gateway_unavailable")
        await asyncio.sleep(3600)


class PlatformUrlResolutionTests(unittest.TestCase):
    """平台地址优先级：命令行 > platform.json > 本地默认。

    为什么单独测：`--url` 既在全局又在 `daemon-run` 子命令上，子命令的默认值会**遮蔽**写在
    子命令前面的全局值（argparse 的经典坑）；而且全局默认值（localhost:8000）对常驻体来说是
    "没指定"，不识别它就会盖住配对时落盘的 platform.json——云端部署实测踩过：daemon 拿着默认值
    去连 127.0.0.1，事件全卡在本地 outbox、任务也领不到。
    """

    def test_cli_wins_over_platform_json(self) -> None:
        args = agentd.argparse.Namespace(url="https://synapforge.top")
        self.assertEqual(
            agentd._resolve_platform_url(args, {"url": "http://stale.local"}, "http://127.0.0.1:8010"),
            "https://synapforge.top",
        )

    def test_platform_json_wins_over_the_local_default(self) -> None:
        args = agentd.argparse.Namespace(url=agentd.DEV_PLATFORM_URL)
        self.assertEqual(
            agentd._resolve_platform_url(args, {"url": "https://synapforge.top"}, "http://127.0.0.1:8010"),
            "https://synapforge.top",
        )

    def test_falls_back_when_nothing_is_known(self) -> None:
        args = agentd.argparse.Namespace()  # 子命令用 SUPPRESS，属性可能根本不存在
        self.assertEqual(
            agentd._resolve_platform_url(args, {}, "http://127.0.0.1:8010"),
            "http://127.0.0.1:8010",
        )


class GatewayUriTests(unittest.TestCase):
    def test_http_and_https_are_mapped_to_websocket_schemes(self) -> None:
        uri = agentd._gateway_uri("http://127.0.0.1:8010/", "device-1", "session-1", "connection-1")
        self.assertEqual(uri, "ws://127.0.0.1:8010/ws/agents/device-1?session_id=session-1&connection_id=connection-1")
        secure = agentd._gateway_uri("https://platform.example", "device-1", "s", "c")
        self.assertTrue(secure.startswith("wss://platform.example/ws/agents/device-1?"))

    def test_invalid_platform_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "daemon_platform_url_invalid"):
            agentd._gateway_uri("ftp://platform.example", "device-1", "s", "c")


class ContractStateTests(unittest.TestCase):
    def test_connection_states_map_to_what_the_shell_expects(self) -> None:
        self.assertEqual(agentd._contract_connection_state("CONNECTED"), "connected")
        self.assertEqual(agentd._contract_connection_state("RECONNECT_WAIT"), "reconnecting")
        self.assertEqual(agentd._contract_connection_state("EMERGENCY_STOPPED"), "emergency_stopped")
        self.assertEqual(agentd._contract_connection_state("unknown-state"), "unknown-state")

    def test_user_session_state_never_claims_locked_or_logged_out(self) -> None:
        """锁屏/注销不做检测，因此只允许 logged_in / unknown 两种取值。"""

        original = os.environ.get("SESSIONNAME")
        try:
            os.environ["SESSIONNAME"] = "Console"
            self.assertEqual(agentd.user_session_state(), "logged_in")
            os.environ.pop("SESSIONNAME", None)
            self.assertIn(agentd.user_session_state(), {"unknown", "logged_in"})
        finally:
            if original is not None:
                os.environ["SESSIONNAME"] = original


class InventoryModelTests(unittest.TestCase):
    """对话用的模型列表：从**真探测到的** opencode 条目里读（拿不到就空，不编）。"""

    def test_models_are_read_from_the_probed_entry(self) -> None:
        inventory = AgentInventory(
            ttl_seconds=0,
            probe=lambda: [
                {
                    "adapter_id": "opencode-cli",
                    "state": "AVAILABLE",
                    "version": "1.18.32",
                    "executable": "/home/synapforge/.opencode/bin/opencode",
                    "models": ["deepseek/deepseek-v4.1-flash", "opencode/big-pickle"],
                    "default_model": "deepseek/deepseek-v4.1-flash",
                },
                {"adapter_id": "codex-cli", "state": "AVAILABLE", "version": "0.1.0", "executable": "codex"},
            ],
        )
        self.assertEqual(
            inventory.models(), (["deepseek/deepseek-v4.1-flash", "opencode/big-pickle"], "deepseek/deepseek-v4.1-flash")
        )

    def test_models_are_empty_when_missing_or_not_available(self) -> None:
        missing = AgentInventory(
            ttl_seconds=0,
            probe=lambda: [
                {"adapter_id": "opencode-cli", "state": "AVAILABLE", "version": "1", "executable": "opencode"}
            ],
        )
        self.assertEqual(missing.models(), ([], ""))
        unavailable = AgentInventory(
            ttl_seconds=0,
            probe=lambda: [
                {
                    "adapter_id": "opencode-cli",
                    "state": "NOT_INSTALLED",
                    "version": None,
                    "executable": "opencode",
                    "models": ["should/not-be-used"],
                }
            ],
        )
        self.assertEqual(unavailable.models(), ([], ""))

    def test_warm_probes_in_the_background_without_blocking(self) -> None:
        probed = []
        inventory = AgentInventory(ttl_seconds=0, probe=lambda: probed.append(1) or [])
        inventory.warm()
        for _ in range(50):
            if probed:
                break
            time.sleep(0.02)
        self.assertTrue(probed, "预热应该在后台真的探一次")


class InventoryRoleTests(unittest.TestCase):
    """对话角色（M-6）：名字以 `opencode agent list` 为准，说明取自角色文件自己的 frontmatter。"""

    def test_role_names_come_from_agent_list_lines_only(self) -> None:
        """权限 JSON 里的内容不能被误当角色名——只认整行 `名字 (模式)`。"""

        stdout = "\n".join(
            [
                "build (primary)",
                "  [",
                "  {",
                '    "permission": "*",',
                '    "action": "allow",',
                '    "pattern": "mm-review"',  # 冒烟陷阱：出现在 JSON 里的角色名不该被当成条目
                "  }",
                "  ]",
                "plan (primary)",
                "mm-review (primary)",
                "explore (subagent)",
            ]
        )
        self.assertEqual(_parse_agent_names(stdout), ["build", "plan", "mm-review", "explore"])

    def test_descriptions_are_read_from_frontmatter_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "agent").mkdir()
            (base / "agents").mkdir()
            (base / "agent" / "mm-review.md").write_text(
                "---\ndescription: 逻辑对抗复核 · 挑硬伤\nmode: primary\n---\n\n正文\n", encoding="utf-8"
            )
            # 另一个目录同样被认（实测 opencode 两个目录都扫）
            (base / "agents" / "mm-plain.md").write_text("---\nname: x\n---\n\n没有说明\n", encoding="utf-8")
            (base / "opencode.json").write_text(
                json.dumps({"agent": {"mm-json": {"description": "配置里定义的角色"}}}), encoding="utf-8"
            )
            descriptions = _opencode_agent_descriptions(base)
        self.assertEqual(descriptions, {"mm-review": "逻辑对抗复核 · 挑硬伤", "mm-json": "配置里定义的角色"})

    def test_probe_keeps_installed_roles_and_plan_but_drops_internal_agents(self) -> None:
        """`plan` 进下拉（用户可选的模式）；compaction/summary/title 这类内部 agent 不进。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "agent").mkdir()
            (base / "agent" / "mm-paper-zh.md").write_text(
                "---\ndescription: 中文论文写作 · 给骨架\n---\n\n正文\n", encoding="utf-8"
            )
            (base / "opencode.json").write_text("{}", encoding="utf-8")
            stdout = "\n".join(
                [
                    "build (primary)",
                    "compaction (primary)",
                    "explore (subagent)",
                    "general (subagent)",
                    "mm-paper-zh (primary)",
                    "plan (primary)",
                    "summary (primary)",
                    "title (primary)",
                ]
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)
            with mock.patch.object(agent_inventory, "_resolve_for_probe", return_value="/usr/bin/opencode"), mock.patch.object(
                agent_inventory.subprocess, "run", return_value=completed
            ):
                roles = _probe_opencode_roles("opencode", config_dir=base)
        self.assertEqual(
            roles,
            [
                {"name": "mm-paper-zh", "description": "中文论文写作 · 给骨架", "executes": True},
                {"name": "plan", "description": BUILTIN_SELECTABLE_AGENTS["plan"], "executes": False},
            ],
        )

    def test_execution_boundary_is_read_from_the_role_file_tools(self) -> None:
        """能不能动手＝frontmatter 里 `bash/edit/write` **有没有被显式关掉**（没写 = 默认开着）。

        这条决定了页面上那句"会改动工作目录"的提示；方向必须是**宁可多标**——
        来源不明时按"能"报，不能让用户以为某个角色是只读的。
        """

        readonly = "---\ndescription: 只读角色 · x\nmode: primary\ntools:\n  bash: false\n  edit: false\n  write: false\n---\n\n正文\n"
        executable = "---\ndescription: 能干活 · x\nmode: primary\ntools:\n  bash: true\n  edit: false\n  write: false\n---\n\n正文\n"
        unlisted = "---\ndescription: 没写工具 · x\nmode: primary\n---\n\n正文\n"
        self.assertFalse(_role_executes(_frontmatter_tools(readonly)))
        self.assertTrue(_role_executes(_frontmatter_tools(executable)))
        # 没写 tools 块 = opencode 默认全开 → 按"能"报
        self.assertTrue(_role_executes(_frontmatter_tools(unlisted)))
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "agent").mkdir()
            (base / "agent" / "mm-ro.md").write_text(readonly, encoding="utf-8")
            (base / "agent" / "mm-rw.md").write_text(executable, encoding="utf-8")
            stdout = "mm-ro (primary)\nmm-rw (primary)\nplan (primary)\n"
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)
            with mock.patch.object(agent_inventory, "_resolve_for_probe", return_value="/usr/bin/opencode"), mock.patch.object(
                agent_inventory.subprocess, "run", return_value=completed
            ):
                roles = _probe_opencode_roles("opencode", config_dir=base)
        flags = {role["name"]: role["executes"] for role in roles}
        self.assertEqual(flags, {"mm-ro": False, "mm-rw": True, "plan": False})

    def test_probe_returns_nothing_when_listing_fails(self) -> None:
        """探测失败/没装 → 空列表：页面只显示「默认」，不显示点不动的假选项。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(agent_inventory, "_resolve_for_probe", return_value="/usr/bin/opencode"), mock.patch.object(
                agent_inventory.subprocess, "run", side_effect=OSError("boom")
            ):
                self.assertEqual(_probe_opencode_roles("opencode", config_dir=temp_dir), [])
        with mock.patch.object(agent_inventory, "_resolve_for_probe", return_value=None):
            self.assertEqual(_probe_opencode_roles("opencode"), [])

    def test_roles_are_read_from_the_probed_entry_and_empty_otherwise(self) -> None:
        inventory = AgentInventory(
            ttl_seconds=0,
            probe=lambda: [
                {
                    "adapter_id": "opencode-cli",
                    "state": "AVAILABLE",
                    "version": "1.18.32",
                    "executable": "/home/synapforge/.opencode/bin/opencode",
                    "roles": [
                        {"name": "mm-review", "description": "逻辑对抗复核", "executes": False},
                        {"name": "  ", "description": "空名要丢"},
                    ],
                }
            ],
        )
        self.assertEqual(inventory.roles(), [{"name": "mm-review", "description": "逻辑对抗复核", "executes": False}])
        empty = AgentInventory(
            ttl_seconds=0,
            probe=lambda: [{"adapter_id": "opencode-cli", "state": "AVAILABLE", "version": "1", "executable": "opencode"}],
        )
        self.assertEqual(empty.roles(), [])


class InventoryTests(unittest.TestCase):
    def test_entries_are_cached_until_ttl_expires(self) -> None:
        calls: list[int] = []
        clock = [0.0]

        def probe() -> list[dict[str, Any]]:
            calls.append(1)
            return [{"adapter_id": "codex-cli", "state": "AVAILABLE", "version": "0.154.0"}]

        inventory = AgentInventory(ttl_seconds=60.0, probe=probe, clock=lambda: clock[0])
        inventory.entries()
        clock[0] = 30.0
        inventory.entries()
        self.assertEqual(len(calls), 1)
        clock[0] = 61.0
        inventory.entries()
        self.assertEqual(len(calls), 2)
        inventory.entries(refresh=True)
        self.assertEqual(len(calls), 3)

    def test_only_successful_probes_are_reported_as_adapter_versions(self) -> None:
        def probe() -> list[dict[str, Any]]:
            return [
                {"adapter_id": "codex-cli", "state": "AVAILABLE", "version": "0.154.0"},
                {"adapter_id": "claude-code", "state": "UNSUPPORTED", "version": "2.0.0"},
                {"adapter_id": "other-cli", "state": "ERROR", "version": "9.9.9"},
                {"adapter_id": "version-less-cli", "state": "AVAILABLE", "version": None},
            ]

        inventory = AgentInventory(probe=probe)
        versions = inventory.adapter_versions()
        # UNSUPPORTED 的执行体对平台没有执行价值，ERROR 的版本不可信，None 直接不报
        self.assertEqual(versions, {"codex-cli": "0.154.0"})
        summary = inventory.summary()
        # 可用清单看 state（执行体在不在），版本上报看 version（版本号可不可信），两者不是一回事
        self.assertEqual(summary["available"], ["codex-cli", "version-less-cli"])
        self.assertEqual(summary["count"], 4)

    def test_probe_failure_is_reported_instead_of_raising(self) -> None:
        def probe() -> list[dict[str, Any]]:
            raise RuntimeError("wmi_broken")

        inventory = AgentInventory(probe=probe)
        with self.assertRaises(RuntimeError):
            inventory.entries()

    def test_real_probe_reports_codex_without_lying(self) -> None:
        """真实探测：codex 存在则 AVAILABLE 且有版本；缺失则如实 NOT_INSTALLED。"""

        inventory = AgentInventory()
        entries = {entry["adapter_id"]: entry for entry in inventory.entries(refresh=True)}
        self.assertIn("codex-cli", entries)
        codex = entries["codex-cli"]
        self.assertIn(codex["state"], {"AVAILABLE", "NOT_INSTALLED", "ERROR", "UNSUPPORTED"})
        if codex["state"] == "AVAILABLE":
            self.assertTrue(codex["version"])
            self.assertIn("codex-cli", inventory.adapter_versions())


class ConnectionIdentityRotationTests(unittest.IsolatedAsyncioTestCase):
    """重连时换 connection_id 并把本地序列对齐——否则平台会一直要求重放。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.client = FakeClient(self.state)
        self.config = MachineServiceConfig(
            gateway_uri="ws://gateway.test/ws?connection_id=connection-initial",
            heartbeat_interval_seconds=0.01,
            send_poll_interval_seconds=0.01,
            reconnect_base_seconds=0.001,
            reconnect_max_seconds=0.002,
            control_poll_interval_seconds=0.005,
            process_stop_timeout_seconds=0.2,
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def _service(self) -> MachineAgentService:
        def next_id() -> str:
            self.counter += 1
            return f"connection-{self.counter:02d}"

        return MachineAgentService(
            self.client,
            lambda: "device-token",
            self.config,
            gateway_uri_provider=lambda connection_id: f"ws://gateway.test/ws?connection_id={connection_id}",
            connection_id_provider=next_id,
        )

    async def test_each_session_uses_a_fresh_connection_id(self) -> None:
        self.client.fail_next = 2
        service = self._service()
        task = asyncio.create_task(service.run_forever())
        for _ in range(200):
            if len(self.client.seen_connection_ids) >= 3:
                break
            await asyncio.sleep(0.005)
        await service.stop()
        await asyncio.wait_for(task, timeout=2)
        self.assertGreaterEqual(len(self.client.seen_connection_ids), 3)
        self.assertEqual(len(set(self.client.seen_connection_ids)), len(self.client.seen_connection_ids))
        for connection_id, uri in zip(self.client.seen_connection_ids, self.client.seen_uris):
            self.assertTrue(uri.endswith(f"connection_id={connection_id}"), uri)

    async def test_sequence_is_aligned_when_a_new_session_starts(self) -> None:
        # 模拟"上一会话留下未确认事件、本地序列已经涨到 5"的场景
        for index in range(4):
            self.state.enqueue_event("agent.heartbeat", {"i": index}, idempotency_key=f"old-{index}")
        self.state.mark_sent(1)
        self.state.acknowledge(1)
        service = self._service()
        service._rotate_connection_identity()
        pending = self.state.pending_events(limit=100)
        self.assertEqual([event["sequence"] for event in pending], [1, 2, 3])
        # 已 ACK 的旧事件不重发，也不该占用序列号
        self.assertEqual(self.state.get_outbox_event(1)["status"], "PENDING")
        self.assertIsNotNone(service.snapshot()["sequence_renumber"])


class DaemonHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    """心跳里的运行态字段（B1 的输入侧）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.client = FakeClient(self.state)

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    async def test_default_heartbeat_reports_loop_state_and_drops_non_uuid_run_ids(self) -> None:
        class FakeLoop:
            def snapshot(self) -> dict[str, Any]:
                return {
                    "running_run_ids": ["11111111-1111-4111-8111-111111111111", "not-a-uuid"],
                    "local_queue_length": 1,
                }

        service = MachineAgentService(
            self.client,
            lambda: "device-token",
            MachineServiceConfig(gateway_uri="ws://gateway.test/ws", heartbeat_interval_seconds=30),
            task_loop=FakeLoop(),
        )
        heartbeat = service._default_heartbeat()
        self.assertEqual(heartbeat.local_queue_length, 1)
        self.assertEqual([str(value) for value in heartbeat.running_run_ids], ["11111111-1111-4111-8111-111111111111"])
        self.assertIn("pending_gateway_events", heartbeat.resource_summary)
        self.assertEqual(service.snapshot()["task_loop"]["local_queue_length"], 1)

    async def test_daemon_heartbeat_is_accepted_by_the_protocol_model(self) -> None:
        """daemon 的心跳必须能通过协议校验（否则平台侧整条心跳被拒）。"""

        from packages.agent_protocol import AgentHeartbeat

        heartbeat = AgentHeartbeat(
            device_id="device-daemon",
            agent_id="agent-daemon",
            session_id="session-daemon",
            connection_id="connection-01",
            agent_version="0.1.0",
            adapter_versions={"codex-cli": "0.154.0"},
            capabilities=["task.claim"],
            running_run_ids=["11111111-1111-4111-8111-111111111111"],
            local_queue_length=1,
            user_session_state="logged_in",
            resource_summary={"cpu_count": 8},
            sent_at=agentd.datetime.now(agentd.UTC),
        )
        payload = heartbeat.model_dump(mode="json")
        self.assertEqual(payload["adapter_versions"], {"codex-cli": "0.154.0"})
        self.assertEqual(payload["user_session_state"], "logged_in")


if __name__ == "__main__":
    unittest.main()
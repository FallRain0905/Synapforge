"""MY-AGENT 契约测试：「对话」轮询循环（`chat_loop.ChatLoop`）与命令组装。

覆盖四件事：
1. **命令组装**：`{prompt}` 替换 + `--session`/`-m` 插在提示词**前面**（插后面会被 CLI 吃进提示词）；
2. **一轮的完整闭环**：取活 → 跑执行体 → 过程事件逐条回传 → complete 带正文/用量/**会话句柄**；
3. **失败**：退出码非 0 时 complete 报 success=false 且带错误（不假装成功）；
4. **没有待办**：claim 返回空 → 不跑执行体、不完成任何东西（常量时间返回，好退避）。

样本用的是服务器上 `opencode run --format json` 的**真实输出**（与 test_opencode_executor.py 同一份）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = AGENT_ROOT.parent.parent
for candidate in (str(REPOSITORY_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from chat_loop import CHOICE_PROTOCOL_INSTRUCTION, ChatLoop, ChatLoopConfig, build_chat_command  # noqa: E402

SAMPLE_SESSION = "ses_f3384abe7ffem1JeVqvfn2lqxk"
SAMPLE = "\n".join(
    [
        '{"type":"step_start","timestamp":1790124141815,"sessionID":"%s","part":{"id":"prt_1","type":"step-start"}}' % SAMPLE_SESSION,
        '{"type":"text","timestamp":1790124142054,"sessionID":"%s","part":{"id":"prt_2","type":"text","text":"改完了：3 处错别字"}}' % SAMPLE_SESSION,
        '{"type":"step_finish","timestamp":1790124142055,"sessionID":"%s","part":{"id":"prt_3","reason":"stop","type":"step-finish","tokens":{"total":7683,"input":52,"output":3,"reasoning":12,"cache":{"write":0,"read":7616}},"cost":0}}' % SAMPLE_SESSION,
    ]
)


class BuildChatCommandTests(unittest.TestCase):
    def test_session_and_model_are_inserted_before_the_prompt(self) -> None:
        command = build_chat_command(
            ["opencode", "run", "--format", "json", "--auto", "{prompt}"],
            prompt="你好",
            session_key=SAMPLE_SESSION,
            model="deepseek/deepseek-v4.1-flash",
        )
        self.assertEqual(command[:5], ["opencode", "run", "--format", "json", "--auto"])
        self.assertEqual(
            command[5:],
            ["--session", SAMPLE_SESSION, "-m", "deepseek/deepseek-v4.1-flash", "你好"],
        )

    def test_nothing_optional_is_added_when_absent(self) -> None:
        command = build_chat_command(["opencode", "run", "{prompt}"], prompt="hi")
        self.assertEqual(command, ["opencode", "run", "hi"])

    def test_template_without_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_command_template_requires_prompt_placeholder"):
            build_chat_command(["opencode", "run", "hi"], prompt="x")

    def test_prompt_placeholder_inside_a_longer_token(self) -> None:
        command = build_chat_command(["/usr/bin/opencode", "run", "--prompt={prompt}"], prompt="q", model="m")
        self.assertEqual(command, ["/usr/bin/opencode", "run", "-m", "m", "--prompt=q"])

    def test_agent_role_is_inserted_before_the_prompt(self) -> None:
        """角色（M-6）：`--agent <名>` 与 `--session`/`-m` 同一处插入，且都在提示词前。"""

        command = build_chat_command(
            ["opencode", "run", "--format", "json", "--auto", "{prompt}"],
            prompt="帮我看这个结果",
            session_key=SAMPLE_SESSION,
            model="deepseek/deepseek-v4.1-flash",
            agent_role="mm-review",
        )
        self.assertEqual(command[:5], ["opencode", "run", "--format", "json", "--auto"])
        self.assertEqual(
            command[5:],
            [
                "--session",
                SAMPLE_SESSION,
                "-m",
                "deepseek/deepseek-v4.1-flash",
                "--agent",
                "mm-review",
                "帮我看这个结果",
            ],
        )
        # 没有角色时不能凭空多一个 --agent（否则每一轮都会被塞一个不存在的角色）
        plain = build_chat_command(["opencode", "run", "{prompt}"], prompt="hi", agent_role="")
        self.assertEqual(plain, ["opencode", "run", "hi"])


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.calls: list[tuple[str, dict]] = []
        self.claim_result: dict | None = {
            "turn": {
                "id": "11111111-1111-1111-1111-111111111111",
                "conversation_id": "22222222-2222-2222-2222-222222222222",
                "seq": 1,
                "status": "CLAIMED",
                "prompt": "把 summary.md 的错别字改掉",
                "model": "deepseek/deepseek-v4.1-flash",
                "session_key": None,
            },
            "command_template": ["opencode", "run", "--format", "json", "--auto", "{prompt}"],
            "worker_events": "opencode",
        }
        self.exit_code = 0
        self.stdout = SAMPLE
        self.commands: list[list[str]] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def loop(self) -> ChatLoop:
        config = ChatLoopConfig(
            url="http://platform.test",
            workspace=Path(self.temp_dir.name),
            state_path=Path(self.temp_dir.name) / "agentd.db",
            device_id="device-cloud-01",
        )
        chat = ChatLoop(
            config,
            identity_provider=lambda: {
                "project_id": "33333333-3333-3333-3333-333333333333",
                "project_token": "t" * 24,
                "agent_id": "agent-cloud-01",
                "device_id": "device-cloud-01",
            },
            log=lambda _message: None,
        )

        def fake_call(path: str, payload: dict, token: str, *, best_effort: bool = False):  # noqa: ANN001
            self.calls.append((path, payload))
            if path.endswith("/chat-turns/claim"):
                return self.claim_result
            return {"ok": True}

        async def fake_run(command, *, workspace, timeout_seconds, on_stdout, turn_id):  # noqa: ANN001
            self.commands.append(list(command))
            on_stdout(self.stdout)
            return self.exit_code, self.stdout, "" if self.exit_code == 0 else "boom"

        chat._call = fake_call  # noqa: SLF001 - 测试注入
        chat.run_command = fake_run
        return chat

    async def test_one_turn_end_to_end_carries_content_usage_and_session(self) -> None:
        chat = self.loop()
        worked = await chat.run_once()
        self.assertTrue(worked)
        paths = [path for path, _ in self.calls]
        self.assertTrue(paths[0].endswith("/chat-turns/claim"))
        self.assertTrue(any(path.endswith("/events") for path in paths))
        complete = next(payload for path, payload in self.calls if path.endswith("/complete"))
        self.assertTrue(complete["success"])
        self.assertEqual(complete["content"], "改完了：3 处错别字")
        self.assertEqual(complete["session_key"], SAMPLE_SESSION)
        self.assertEqual(complete["usage"]["source"], "opencode-jsonl")
        self.assertGreater(complete["usage"]["total_tokens"], 0)
        # 命令行里模型是从轮次带下来的（-m 插在提示词前）
        self.assertEqual(self.commands[0][5:7], ["-m", "deepseek/deepseek-v4.1-flash"])
        # 这一轮没选角色：命令行里就不该出现 --agent
        self.assertNotIn("--agent", self.commands[0])
        # 过程事件按序号递增、且带事件类型
        events = [payload for path, payload in self.calls if path.endswith("/events")]
        self.assertEqual([item["sequence"] for item in events], list(range(1, len(events) + 1)))
        self.assertIn("process.started", [item["event_type"] for item in events])

    async def test_second_turn_reuses_the_session_handle(self) -> None:
        self.claim_result["turn"]["session_key"] = SAMPLE_SESSION  # 平台把上一轮的句柄带下来了
        chat = self.loop()
        await chat.run_once()
        self.assertIn("--session", self.commands[0])
        self.assertIn(SAMPLE_SESSION, self.commands[0])

    async def test_role_from_the_turn_is_passed_to_the_executor(self) -> None:
        """M-6：会话上选的角色随轮次下来，内核把它变成 `--agent <名>`（名字以执行体探测为准，内核不校验也不猜）。"""

        self.claim_result["turn"]["role"] = "mm-review"
        chat = self.loop()
        await chat.run_once()
        command = self.commands[0]
        self.assertIn("--agent", command)
        self.assertEqual(command[command.index("--agent") + 1], "mm-review")
        # 角色必须插在提示词之前（插到后面会被 CLI 当成提示词的一部分）
        self.assertLess(command.index("--agent"), len(command) - 1)

    async def test_input_files_are_downloaded_and_announced_in_the_prompt(self) -> None:
        """M-3：这一轮带的附件先落盘，再在提示词里告诉执行体去哪儿看。"""

        self.claim_result["input_files"] = [{"artifact_id": "a1", "name": "报告.md"}]
        downloaded: list[str] = []

        class FakeArtifactClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def download(self, artifact_id, *, max_bytes=0):  # noqa: ANN001, ARG002
                downloaded.append(artifact_id)
                return "# 报告".encode("utf-8"), "报告.md"

        chat = self.loop()
        chat_artifact_client = FakeArtifactClient
        # 用真实客户端类名替换：chat_loop 里是按模块名引用的
        import chat_loop as chat_loop_module

        original = chat_loop_module.AgentArtifactClient
        chat_loop_module.AgentArtifactClient = chat_artifact_client
        try:
            await chat.run_once()
        finally:
            chat_loop_module.AgentArtifactClient = original
        self.assertEqual(downloaded, ["a1"])
        prompt = self.commands[0][-1]
        self.assertIn("inputs/", prompt)
        self.assertIn("报告.md", prompt)
        self.assertIn("把 summary.md 的错别字改掉", prompt)
        self.assertTrue((Path(self.temp_dir.name) / "inputs" / "报告.md").exists())

    async def test_choice_protocol_instruction_is_appended_to_every_turn(self) -> None:
        """结构化选择协议注入在**内核层**：不带附件、不选角色的普通一轮，提示词末尾也要有协议说明。"""

        chat = self.loop()
        await chat.run_once()
        prompt = self.commands[0][-1]
        self.assertIn("synapforge-choice", prompt)
        self.assertTrue(prompt.endswith(CHOICE_PROTOCOL_INSTRUCTION))
        # 只追加一次（重复追加会把提示词越撑越长，还会让模型读到互相矛盾的编号）
        self.assertEqual(prompt.count("synapforge-choice"), CHOICE_PROTOCOL_INSTRUCTION.count("synapforge-choice"))

    async def test_choice_protocol_comes_last_when_inputs_and_role_are_present(self) -> None:
        """带附件 + 选角色的一轮：附件前缀在最前、协议在最后，用户正文仍在中间（顺序不能乱）。"""

        self.claim_result["turn"]["role"] = "mm-topic"
        self.claim_result["input_files"] = [{"artifact_id": "a1", "name": "报告.md"}]

        class FakeArtifactClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def download(self, artifact_id, *, max_bytes=0):  # noqa: ANN001, ARG002
                return "# 报告".encode("utf-8"), "报告.md"

        chat = self.loop()
        import chat_loop as chat_loop_module

        original = chat_loop_module.AgentArtifactClient
        chat_loop_module.AgentArtifactClient = FakeArtifactClient
        try:
            await chat.run_once()
        finally:
            chat_loop_module.AgentArtifactClient = original

        prompt = self.commands[0][-1]
        self.assertTrue(prompt.startswith("[本轮提供了 1 个文件"))
        self.assertTrue(prompt.endswith(CHOICE_PROTOCOL_INSTRUCTION))
        self.assertLess(prompt.index("把 summary.md 的错别字改掉"), prompt.index("[平台交互协议：结构化用户选择]"))
        # 角色照旧以 --agent 传下去，协议是提示词的一部分，两者互不干扰
        self.assertIn("--agent", self.commands[0])

    def test_turn_cancelled_reads_the_agent_facing_status(self) -> None:
        """跑的过程中靠这条查询发现"被取消了"；查询失败当没取消（不误杀）。"""

        import urllib.request as urllib_request

        import chat_loop as chat_loop_module

        chat = self.loop()
        original = urllib_request.urlopen

        class FakeResponse:
            def __init__(self, body: str) -> None:
                self.body = body.encode("utf-8")

            def read(self) -> bytes:
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        try:
            urllib_request.urlopen = lambda *_args, **_kwargs: FakeResponse('{"status": "CANCELLED"}')
            self.assertTrue(chat._turn_cancelled("t" * 24, "agent-cloud-01", "turn-1"))
            urllib_request.urlopen = lambda *_args, **_kwargs: FakeResponse('{"status": "CLAIMED"}')
            self.assertFalse(chat._turn_cancelled("t" * 24, "agent-cloud-01", "turn-1"))

            def boom(*_args, **_kwargs):
                raise OSError("network down")

            urllib_request.urlopen = boom
            self.assertFalse(chat._turn_cancelled("t" * 24, "agent-cloud-01", "turn-1"))
        finally:
            urllib_request.urlopen = original
            del chat_loop_module

    async def test_default_runner_kills_the_process_when_cancelled(self) -> None:
        """真中断：取消后真的把子进程杀掉，而不是等它自己跑完（M-4）。"""

        import sys as _sys

        chat = self.loop()
        chat.config.turn_timeout_seconds = 60.0
        chat.identity_provider = lambda: {
            "project_id": "33333333-3333-3333-3333-333333333333",
            "project_token": "t" * 24,
            "agent_id": "agent-cloud-01",
            "device_id": "device-cloud-01",
        }
        started = time.monotonic()
        exit_code, _stdout, _stderr, cancelled = await chat._default_run_command(
            [_sys.executable, "-c", "import time; time.sleep(30)"],
            workspace=Path(self.temp_dir.name),
            timeout_seconds=60.0,
            on_stdout=lambda _chunk: None,
            turn_id="turn-1",
            cancel_check=lambda: True,
            cancel_poll_seconds=1.0,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(cancelled)
        self.assertLess(elapsed, 15.0, "取消后应该很快返回，而不是等满 30 秒")

    async def test_run_once_uses_the_cancellable_path_for_the_default_runner(self) -> None:
        """回归：默认路径必须走"可取消"的那条（判据写错时，取消不会生效——线上踩过）。"""

        chat = self.loop()
        chat.run_command = None  # 用内核默认 runner（真起进程）
        chat.config.cancel_poll_seconds = 1.0
        chat.config.turn_timeout_seconds = 60.0
        chat._turn_cancelled = lambda *_args: True  # 一开始就"已被取消"
        self.claim_result["command_template"] = [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "{prompt}",
        ]
        started = time.monotonic()
        worked = await chat.run_once()
        elapsed = time.monotonic() - started
        self.assertTrue(worked)
        self.assertLess(elapsed, 20.0, "被取消后应立刻返回，而不是等满 30 秒")
        complete = next(payload for path, payload in self.calls if path.endswith("/complete"))
        self.assertFalse(complete["success"])
        self.assertEqual(complete["error"], "cancelled_by_member")

    async def test_failure_is_reported_not_hidden(self) -> None:
        self.exit_code = 1
        chat = self.loop()
        await chat.run_once()
        complete = next(payload for path, payload in self.calls if path.endswith("/complete"))
        self.assertFalse(complete["success"])
        self.assertIn("boom", complete["error"])
        self.assertEqual(chat.snapshot()["chat_failed"], 1)

    async def test_no_pending_turn_means_no_work(self) -> None:
        self.claim_result = None
        chat = self.loop()
        self.assertFalse(await chat.run_once())
        self.assertEqual(self.commands, [])
        self.assertEqual([path for path, _ in self.calls], ["/api/agents/agent-cloud-01/chat-turns/claim"])

    async def test_missing_authorization_does_not_call_the_platform(self) -> None:
        chat = self.loop()
        chat.identity_provider = lambda: {}
        self.assertFalse(await chat.run_once())
        self.assertEqual(self.calls, [])

    async def test_loop_backs_off_when_idle_and_stops_on_request(self) -> None:
        self.claim_result = None
        chat = self.loop()
        stop = asyncio.Event()
        task = asyncio.create_task(chat.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertGreaterEqual(chat.snapshot()["chat_idle_seconds"], chat.config.idle_seconds)


    async def test_serve_channel_handles_the_turn_when_enabled(self) -> None:
            """M-5c S-1：配了端口 + 执行体是 opencode → 这一轮走常驻 server，不再拼 CLI 命令。"""

            import chat_loop as chat_loop_module
            from opencode_server import ServeTurnOutcome

            chat = self.loop()
            chat.config.chat_server_port = 4199

            class FakeServer:
                def __init__(self, *_args, **_kwargs) -> None:
                    self.client = object()
                    self.stopped = False

                def ensure_running(self) -> bool:
                    return True

                def snapshot(self) -> dict:
                    return {"chat_server_alive": True}

                def stop(self) -> None:
                    self.stopped = True

            calls: list[dict] = []

            def fake_run_serve_turn(_client, **kwargs):
                calls.append(kwargs)
                return ServeTurnOutcome(
                    ok=True,
                    text="serve 通道的回复",
                    session_key="ses_serve_new",
                    usage={"total_tokens": 123, "source": "opencode-serve"},
                )

            originals = (chat_loop_module.OpenCodeServer, chat_loop_module.run_serve_turn)
            chat_loop_module.OpenCodeServer = FakeServer
            chat_loop_module.run_serve_turn = fake_run_serve_turn
            try:
                worked = await chat.run_once()
            finally:
                chat_loop_module.OpenCodeServer, chat_loop_module.run_serve_turn = originals

            self.assertTrue(worked)
            # 提示词照旧带上了结构化选择协议；模型与角色（本轮没选角色 → None）都传给了 server
            self.assertIn("synapforge-choice", calls[0]["prompt"])
            self.assertEqual(calls[0]["model"], "deepseek/deepseek-v4.1-flash")
            self.assertIsNone(calls[0]["agent"])
            complete = next(payload for path, payload in self.calls if path.endswith("/complete"))
            self.assertTrue(complete["success"])
            self.assertEqual(complete["content"], "serve 通道的回复")
            self.assertEqual(complete["session_key"], "ses_serve_new")
            self.assertEqual(complete["usage"]["source"], "opencode-serve")
            # **没有**跑 CLI 执行体（两条通道不会同时跑）
            self.assertEqual(self.commands, [])
            # 退场清理真的会去停那个常驻进程
            chat.close()
            self.assertTrue(chat._server.stopped)

    async def test_falls_back_to_the_cli_channel_when_server_is_unavailable(self) -> None:
        """起不来就**如实降级**：端口配了但 server 不可用 → 还走原来的 CLI 通道，不假装用了新通道。"""

        import chat_loop as chat_loop_module

        chat = self.loop()
        chat.config.chat_server_port = 4199

        class DeadServer:
            def __init__(self, *_args, **_kwargs) -> None:
                self.client = object()

            def ensure_running(self) -> bool:
                return False

            def snapshot(self) -> dict:
                return {}

            def stop(self) -> None:
                return None

        original = chat_loop_module.OpenCodeServer
        chat_loop_module.OpenCodeServer = DeadServer
        try:
            worked = await chat.run_once()
        finally:
            chat_loop_module.OpenCodeServer = original

        self.assertTrue(worked)
        self.assertTrue(self.commands)  # CLI 真的跑了
        complete = next(payload for path, payload in self.calls if path.endswith("/complete"))
        self.assertTrue(complete["success"])
        self.assertEqual(complete["content"], "改完了：3 处错别字")

    async def test_channel_switch_is_off_by_default(self) -> None:
        """默认（端口 0）连 server 都不去看一眼——不配就不启用，别的机器不受影响。"""

        import chat_loop as chat_loop_module

        chat = self.loop()
        touched: list[str] = []

        class SpyServer:
            def __init__(self, *_args, **_kwargs) -> None:
                touched.append("constructed")

        original = chat_loop_module.OpenCodeServer
        chat_loop_module.OpenCodeServer = SpyServer
        try:
            await chat.run_once()
        finally:
            chat_loop_module.OpenCodeServer = original

        self.assertEqual(touched, [])
        self.assertTrue(self.commands)


if __name__ == "__main__":
    unittest.main()
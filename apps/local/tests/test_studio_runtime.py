"""单 Agent 运行器：渠道 settings 隔离。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synapforge.studio.agent_runtime import AgentRunWorker, RunBroker


class AgentRuntimeTests(unittest.TestCase):
    def test_channel_settings_are_private_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sf-runtime-") as tmp:
            worker = AgentRunWorker("arun-test", Path(tmp) / "state.db", RunBroker())
            worker._provider = {  # noqa: SLF001 — 直接验证 settings 内容
                "id": "prov-test", "name": "magpie", "kind": "anthropic",
                "base_url": "http://127.0.0.1:3425", "api_key": "magpie",
            }
            worker._model = "auto/glm-5.3-flash"  # noqa: SLF001
            path = worker._write_claude_settings()  # noqa: SLF001
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["env"]["ANTHROPIC_BASE_URL"],
                             "http://127.0.0.1:3425")
            self.assertEqual(payload["env"]["ANTHROPIC_AUTH_TOKEN"], "magpie")
            self.assertEqual(payload["env"]["ANTHROPIC_MODEL"], "auto/glm-5.3-flash")
            self.assertNotIn("OPENAI_API_KEY", payload["env"])

            worker._settings_path = path  # noqa: SLF001
            worker._cleanup_settings()  # noqa: SLF001
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_openai_channel_env_uses_v1_base(self) -> None:
        from synapforge.studio import providers

        env = providers.env_for({"name": "本地", "kind": "openai",
                                 "base_url": "http://127.0.0.1:8000/v1",
                                 "api_key": "k"}, "m")
        self.assertEqual(env["OPENAI_BASE_URL"], "http://127.0.0.1:8000/v1")
        self.assertEqual(env["OPENAI_API_KEY"], "k")
        self.assertEqual(env["OPENAI_MODEL"], "m")


if __name__ == "__main__":
    unittest.main()

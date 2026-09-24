"""能力卡解析的单元测试（AIP-1a）：`--capability-card` 的三种写法与"解析不了就报错"。

对应 `apps/agent/agentd.py::capability_cards_from_args`：内联 JSON / `@文件` / `技能@版本`。
平台侧会再做一次归一化与校验（`app.skill_match`），这里只管命令行取值别静默丢失。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import agentd


def args_with(cards: list[str]) -> argparse.Namespace:
    return argparse.Namespace(capability_card=cards)


class CapabilityCardArgumentTests(unittest.TestCase):
    def test_shorthand_and_version(self) -> None:
        cards = agentd.capability_cards_from_args(args_with(["codex@0.9.3", "python"]))
        self.assertEqual(
            cards,
            [{"skill": "codex", "version": "0.9.3"}, {"skill": "python", "version": ""}],
        )

    def test_inline_json_object_and_list(self) -> None:
        cards = agentd.capability_cards_from_args(
            args_with([json.dumps({"skill": "doc.write", "version": "2", "outputs": ["paper_source"]})])
        )
        self.assertEqual(cards[0]["skill"], "doc.write")
        self.assertEqual(cards[0]["outputs"], ["paper_source"])
        many = agentd.capability_cards_from_args(args_with([json.dumps([{"skill": "a1"}, {"skill": "b2"}])]))
        self.assertEqual([card["skill"] for card in many], ["a1", "b2"])

    def test_file_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cards.json"
            path.write_text(json.dumps([{"skill": "r-language", "version": "4.4"}]), encoding="utf-8")
            cards = agentd.capability_cards_from_args(args_with([f"@{path}"]))
        self.assertEqual(cards, [{"skill": "r-language", "version": "4.4", "inputs": [], "outputs": [], "description": ""}])

    def test_invalid_input_raises_instead_of_silently_dropping(self) -> None:
        with self.assertRaises(ValueError):
            agentd.capability_cards_from_args(args_with(["@C:/definitely/missing/cards.json"]))
        with self.assertRaises(ValueError):
            agentd.capability_cards_from_args(args_with(["{not json"]))
        with self.assertRaises(ValueError):
            agentd.capability_cards_from_args(args_with([json.dumps({"version": "1"})]))

    def test_register_payload_includes_cards_and_executor(self) -> None:
        """注册体只在真的声明了卡片/执行体时才带字段（老调用方零变化）。"""

        captured: dict[str, object] = {}

        def fake_request(url: str, method: str, path: str, payload: dict | None = None) -> dict:
            captured["payload"] = payload
            return {"agent_id": "agent-x"}

        original = agentd.request
        agentd.request = fake_request
        try:
            base = argparse.Namespace(
                url="http://localhost:8000",
                agent_id="agent-x",
                display_name="执行体 X",
                owner="member-001",
                provider="local",
                model="unspecified",
                workspace=".",
                tools=[],
                languages=["python"],
                capability_card=[],
                executor_kind="",
                executor_version="",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                agentd.register(base)
                self.assertNotIn("capability_cards", captured["payload"])
                self.assertNotIn("executor", captured["payload"])
                agentd.register(
                    argparse.Namespace(
                        **{**vars(base), "capability_card": ["codex@0.9.3"], "executor_kind": "codex", "executor_version": "0.9.3"}
                    )
                )
            self.assertEqual(captured["payload"]["capability_cards"], [{"skill": "codex", "version": "0.9.3"}])
            self.assertEqual(captured["payload"]["executor"], {"kind": "codex", "version": "0.9.3"})
        finally:
            agentd.request = original


if __name__ == "__main__":
    unittest.main()
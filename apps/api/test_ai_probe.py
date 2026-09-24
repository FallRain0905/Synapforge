"""UX-7-03 `ai_probe` 的契约测试。

探针要真的发请求，但测试不该依赖外网：**打桩 `_post_json`**，断言的是"如何解释结果"，
包括把网络失败、HTTP 错误、维度不一致都如实回报，以及 MinerU 不假装测过。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from app import ai_probe


class LlmProbeTests(unittest.TestCase):
    def test_missing_credentials_is_reported_not_probed(self) -> None:
        with patch.object(ai_probe, "_post_json") as post:
            result = ai_probe.probe_llm({})
        self.assertFalse(result["ok"])
        self.assertIn("未配置完整", result["detail"])
        post.assert_not_called()

    def test_success_reports_model(self) -> None:
        settings = {"llm_api_key": "sk-x", "llm_base_url": "https://api.example.com/v1", "llm_model": "gpt-x"}
        with patch.object(ai_probe, "_post_json", return_value=(200, {"choices": [{"message": {"content": "p"}}]})) as post:
            result = ai_probe.probe_llm(settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-x")
        # 复用 base_url 时不能重复拼接 /chat/completions
        self.assertEqual(post.call_args.args[0], "https://api.example.com/v1/chat/completions")

    def test_endpoint_suffix_is_not_duplicated(self) -> None:
        settings = {"llm_api_key": "sk", "llm_base_url": "https://api.example.com/v1/chat/completions", "llm_model": "m"}
        with patch.object(ai_probe, "_post_json", return_value=(200, {})) as post:
            ai_probe.probe_llm(settings)
        self.assertEqual(post.call_args.args[0], "https://api.example.com/v1/chat/completions")

    def test_http_error_and_network_error_are_distinguished(self) -> None:
        settings = {"llm_api_key": "sk", "llm_base_url": "https://api.example.com/v1", "llm_model": "m"}
        with patch.object(ai_probe, "_post_json", return_value=(401, {"_error": "invalid api key"})):
            unauthorized = ai_probe.probe_llm(settings)
        self.assertFalse(unauthorized["ok"])
        self.assertIn("HTTP 401", unauthorized["detail"])
        self.assertIn("invalid api key", unauthorized["detail"])

        with patch.object(ai_probe, "_post_json", return_value=(0, {"_error": "timed out"})):
            unreachable = ai_probe.probe_llm(settings)
        self.assertFalse(unreachable["ok"])
        self.assertIn("网络不可达", unreachable["detail"])


class EmbeddingProbeTests(unittest.TestCase):
    settings = {
        "embedding_api_key": "sk-e",
        "embedding_base_url": "https://api.example.com/v1",
        "embedding_model": "bge-m3",
        "embedding_dimensions": 1024,
    }

    def test_success_reports_dimensions(self) -> None:
        with patch.object(ai_probe, "_post_json", return_value=(200, {"data": [{"embedding": [0.0] * 1024}]})):
            result = ai_probe.probe_embedding(self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["dimensions"], 1024)
        self.assertNotIn("注意", result["detail"])

    def test_dimension_mismatch_is_flagged(self) -> None:
        """维度不一致必须在界面上被点出来：索引与查询维度不符会全面失败。"""

        with patch.object(ai_probe, "_post_json", return_value=(200, {"data": [{"embedding": [0.0] * 768}]})):
            result = ai_probe.probe_embedding(self.settings)
        self.assertTrue(result["ok"])
        self.assertIn("实际返回 768 维", result["detail"])


class MineruProbeTests(unittest.TestCase):
    def test_mineru_does_not_pretend_to_be_verified(self) -> None:
        missing = ai_probe.probe_mineru({})
        self.assertFalse(missing["ok"])

        configured = ai_probe.probe_mineru({"mineru_api_key": "tok"})
        self.assertTrue(configured["ok"])
        self.assertFalse(configured["verified"])
        self.assertIn("没有免费探活接口", configured["detail"])


class TestAllTests(unittest.TestCase):
    def test_one_component_failure_does_not_hide_others(self) -> None:
        settings = {
            "llm_api_key": "sk",
            "llm_base_url": "https://api.example.com/v1",
            "llm_model": "m",
            "embedding_api_key": "sk-e",
            "embedding_base_url": "https://api.example.com/v1",
            "embedding_model": "e",
            "mineru_api_key": "tok",
        }

        def fake_post(url: str, payload: dict, api_key: str):
            if url.endswith("/embeddings"):
                return 500, {"_error": "boom"}
            return 200, {"choices": [{"message": {"content": "p"}}]}

        with patch.object(ai_probe, "_post_json", side_effect=fake_post):
            report = ai_probe.test_all(settings)

        self.assertTrue(report["llm"]["ok"])
        self.assertFalse(report["embedding"]["ok"])
        self.assertTrue(report["mineru"]["ok"])
        self.assertEqual(set(report.keys()), {"llm", "embedding", "mineru"})

    def test_probe_exception_is_reported_as_result(self) -> None:
        with patch.object(ai_probe, "probe_llm", side_effect=RuntimeError("weird")):
            report = ai_probe.test_all({"llm_api_key": "x"})
        self.assertFalse(report["llm"]["ok"])
        self.assertIn("RuntimeError", report["llm"]["detail"])


if __name__ == "__main__":
    unittest.main()
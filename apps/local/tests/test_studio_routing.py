"""渠道路由：多 key 池、失败分类、状态恢复。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import providers as studio_providers
from synapforge.studio import routing as studio_routing
from synapforge.studio import store as studio_store


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-route-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.provider = studio_providers.create_provider(
            self.store, name="多 key 渠道", kind="openai",
            base_url="http://127.0.0.1:9", api_key="main-key-111111")

    def test_pool_includes_main_key_first(self) -> None:
        pool = studio_routing.add_key(self.store, self.provider["id"],
                                      api_key="pool-key-222222", label="备用 1")
        self.assertEqual(pool["total"], 2)
        self.assertEqual(pool["keys"][0]["source"], "provider")
        self.assertEqual(pool["keys"][1]["label"], "备用 1")
        usable = studio_routing.usable_keys(
            self.store, studio_store.get_provider(self.store, self.provider["id"]))
        self.assertEqual([key.api_key for key in usable],
                         ["main-key-111111", "pool-key-222222"])

    def test_auth_failure_marks_key_failed_and_skips_it(self) -> None:
        pool = studio_routing.add_key(self.store, self.provider["id"],
                                      api_key="pool-key-222222", label="备用")
        key_id = pool["keys"][1]["id"]
        studio_routing.mark_key(self.store, self.provider["id"], key_id, "auth", "401")
        provider = studio_store.get_provider(self.store, self.provider["id"])
        usable = studio_routing.usable_keys(self.store, provider)
        self.assertEqual([key.label for key in usable], ["主 key"])
        summary = studio_routing.route_summary(self.store, provider)
        self.assertEqual(summary["available"], 1)
        self.assertEqual(summary["keys"][1]["status"], "failed")

        restored = studio_routing.restore_key(self.store, self.provider["id"], key_id)
        self.assertEqual(restored["available"], 2)

    def test_limited_key_cools_down_then_recovers(self) -> None:
        pool = studio_routing.add_key(self.store, self.provider["id"],
                                      api_key="pool-key-333333", label="限流")
        key_id = pool["keys"][1]["id"]
        studio_routing.mark_key(self.store, self.provider["id"], key_id,
                                "limited", "429")
        provider = studio_store.get_provider(self.store, self.provider["id"])
        self.assertEqual(len(studio_routing.usable_keys(self.store, provider)), 1)

        studio_store.update_provider_key(
            self.store, key_id, last_check={"until": time.time() - 1})
        provider = studio_store.get_provider(self.store, self.provider["id"])
        self.assertEqual(len(studio_routing.usable_keys(self.store, provider)), 2)

    def test_disabled_key_excluded(self) -> None:
        pool = studio_routing.add_key(self.store, self.provider["id"],
                                      api_key="pool-key-444444", label="停用")
        key_id = pool["keys"][1]["id"]
        studio_routing.update_key(self.store, self.provider["id"], key_id,
                                  enabled=False)
        provider = studio_store.get_provider(self.store, self.provider["id"])
        self.assertEqual(len(studio_routing.usable_keys(self.store, provider)), 1)

    def test_delete_pool_key_and_main_protection(self) -> None:
        pool = studio_routing.add_key(self.store, self.provider["id"],
                                      api_key="pool-key-555555")
        key_id = pool["keys"][1]["id"]
        pool = studio_routing.delete_key(self.store, self.provider["id"], key_id)
        self.assertEqual(pool["total"], 1)
        with self.assertRaises(Exception) as ctx:
            studio_routing.delete_key(self.store, self.provider["id"],
                                      f"{self.provider['id']}:main")
        self.assertEqual(getattr(ctx.exception, "code", ""), "provider_key_main")

    def test_classify_failure(self) -> None:
        self.assertEqual(studio_routing.classify_failure("401 invalid token"), "auth")
        self.assertEqual(studio_routing.classify_failure("429 rate limit"), "limited")
        self.assertEqual(studio_routing.classify_failure("额度不足"), "limited")
        self.assertEqual(studio_routing.classify_failure("disk full"), "other")

    def test_env_uses_selected_key(self) -> None:
        raw = studio_store.get_provider(self.store, self.provider["id"])
        env = studio_providers.env_for(raw, "m", api_key="chosen-key")
        self.assertEqual(env["OPENAI_API_KEY"], "chosen-key")
        env = studio_providers.env_for(raw, "m")
        self.assertEqual(env["OPENAI_API_KEY"], "main-key-111111")

    def test_deleting_provider_removes_keys(self) -> None:
        studio_routing.add_key(self.store, self.provider["id"], api_key="pool-key-666666")
        studio_providers.delete_provider(self.store, self.provider["id"])
        self.assertEqual(studio_store.list_provider_keys(self.store, self.provider["id"]), [])


if __name__ == "__main__":
    unittest.main()

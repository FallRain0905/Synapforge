"""方案（Profile）：保存/应用一组 Agent 配置。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import profiles as studio_profiles
from synapforge.studio import providers as studio_providers
from synapforge.studio import store as studio_store


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-profile-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.provider = studio_providers.create_provider(
            self.store, name="便宜渠道", base_url="https://api.example.com",
            models=["cheap-model"])
        self.agent = studio_agents.create_agent(
            self.store, name="Writer", model="expensive-model",
            provider_id=self.provider["id"], system_prompt="旧角色卡",
            workspace=str(Path(self.tmp.name) / "ws"))

    def test_save_and_apply_round_trip(self) -> None:
        profile = studio_profiles.save_profile(
            self.store, name="省钱", note="都切到便宜模型")
        self.assertEqual(profile["count"], 1)
        self.assertEqual(profile["agents"][0]["name"], "Writer")
        self.assertEqual(profile["agents"][0]["provider_name"], "便宜渠道")
        self.assertTrue(profile["agents"][0]["present"])

        studio_agents.update_agent(
            self.store, self.agent["id"], model="expensive-model-2",
            system_prompt="新角色卡", provider_id="", allow_web=False)
        result = studio_profiles.apply_profile(self.store, profile["id"])
        self.assertEqual(result["applied"], ["Writer"])
        self.assertEqual(result["skipped"], [])
        refreshed = studio_store.get_agent(self.store, self.agent["id"])
        self.assertEqual(refreshed["model"], "expensive-model")
        self.assertEqual(refreshed["system_prompt"], "旧角色卡")
        self.assertEqual(refreshed["provider_id"], self.provider["id"])
        self.assertTrue(refreshed["allow_web"])

    def test_apply_skips_missing_and_keeps_extra_agents(self) -> None:
        profile = studio_profiles.save_profile(self.store, name="基线")
        studio_store.delete_agent(self.store, self.agent["id"])
        extra = studio_agents.create_agent(
            self.store, name="Later", workspace=str(Path(self.tmp.name) / "ws2"))
        result = studio_profiles.apply_profile(self.store, profile["id"])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["skipped"], ["Writer"])
        still = studio_store.get_agent(self.store, extra["id"])
        self.assertEqual(still["name"], "Later")

    def test_duplicate_name_rejected_and_rename(self) -> None:
        first = studio_profiles.save_profile(self.store, name="方案A")
        with self.assertRaises(studio_agents.StudioError) as ctx:
            studio_profiles.save_profile(self.store, name="方案A")
        self.assertEqual(ctx.exception.code, "profile_name_exists")
        renamed = studio_profiles.update_profile_meta(
            self.store, first["id"], name="方案B", note="改过")
        self.assertEqual(renamed["name"], "方案B")
        self.assertEqual(renamed["note"], "改过")

    def test_resave_overwrites_snapshot(self) -> None:
        profile = studio_profiles.save_profile(self.store, name="快照")
        studio_agents.update_agent(self.store, self.agent["id"], model="new-model")
        again = studio_profiles.save_profile(
            self.store, name="快照", profile_id=profile["id"])
        self.assertEqual(again["agents"][0]["model"], "new-model")
        self.assertEqual(len(studio_store.list_profiles(self.store)), 1)

    def test_delete_profile(self) -> None:
        profile = studio_profiles.save_profile(self.store, name="临时")
        result = studio_profiles.delete_profile(self.store, profile["id"])
        self.assertEqual(result["name"], "临时")
        self.assertEqual(studio_store.list_profiles(self.store), [])


if __name__ == "__main__":
    unittest.main()

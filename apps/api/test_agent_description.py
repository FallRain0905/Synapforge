"""执行体描述结构化（AIP-1a / AIP-1b / AIP-1c，见 docs/AIP_1_PLAN.md）。

覆盖：
  * 能力卡：注册带卡 → 归一化落库、`supported_tools` 派生、包/实例两段身份；
  * 词表收敛：`Python` 与 `python` 是同一种能力（读时归一化，无需数据回填）；
  * 版本约束：`codex@>=1.0` 与 `codex@>=2.0` 的差别；缺版本不满足下限；
  * 技能域 / 授权范围域分离：声明 `artifact.write` **授权**不等于有 `artifact.write` **技能**；
  * 候选推荐：satisfied / partial 分层、离线也列出、排序确定、调度器取"界面推荐的第一个"；
  * HTTP：非法技能名 422、候选端点 200/404、能力目录带 cards 与 packages。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main
from app.contracts import (
    AgentExecutor,
    AgentRegister,
    CapabilityCard,
    HumanMemberCreate,
    ProjectCreate,
    SessionCreate,
    TaskClaimRequest,
    TaskCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class AgentDescriptionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(name="执行体描述测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )

    def grant_project(self, agent_id: str, capabilities: list[str] | None = None) -> None:
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, str(self.project.id), json.dumps(capabilities or ["task.claim"]), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()

    def attach_device(self, agent_id: str, *, capabilities: list[str], adapter_versions: dict[str, str] | None = None) -> str:
        """造一台已连接设备（设备的 capabilities 是**授权范围**，心跳/运行态按需带执行体版本）。"""

        device_id = f"device-{uuid4().hex[:10]}"
        stamp = "2026-01-01T00:00:00+00:00"
        self.store.db.execute(
            "INSERT INTO devices (device_id, organization_id, agent_id, owner_member_id, device_name, public_key, "
            "public_key_fingerprint, device_token_hash, platform, agent_version, capabilities, status, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                device_id,
                str(DEV_ORG_ID),
                agent_id,
                self.lead.id,
                "测试机",
                "pk",
                f"fp-{device_id}",
                f"hash-{device_id}",
                "win32",
                "0.1.1",
                json.dumps(capabilities),
                stamp,
                stamp,
            ),
        )
        self.store.db.execute(
            "INSERT INTO agent_connections (connection_id, device_id, agent_id, session_id, transport, status, connected_at, last_heartbeat_at) "
            "VALUES (?, ?, ?, ?, 'websocket', 'CONNECTED', ?, ?)",
            (f"conn-{device_id}", device_id, agent_id, f"session-{device_id}", stamp, stamp),
        )
        if adapter_versions is not None:
            self.store.db.execute(
                "INSERT OR REPLACE INTO device_runtime_state (device_id, connection_id, adapter_versions, capabilities, reported_at) "
                "VALUES (?, ?, ?, '[]', ?)",
                (device_id, f"conn-{device_id}", json.dumps(adapter_versions), stamp),
            )
        self.store.db.commit()
        return device_id

    def make_task(self, title: str, **overrides):
        return self.store.create_task(self.project.id, TaskCreate(title=title, description="测试", **overrides))

    def claim_request(self, agent_id: str) -> TaskClaimRequest:
        return TaskClaimRequest(agent_id=agent_id, lease_seconds=600, idempotency_key=uuid4().hex)


class CapabilityCardTests(AgentDescriptionFixture):
    def test_register_with_cards_normalizes_and_derives_skills(self) -> None:
        agent = self.store.register_agent(
            AgentRegister(
                agent_id="agent-cards",
                display_name="能力卡执行体",
                owner_member_id=self.lead.id,
                supported_tools=["Extra-Tool"],
                capability_cards=[
                    CapabilityCard(skill="Python", version="3.11", inputs=["data_profile"], outputs=["code"]),
                    CapabilityCard(skill="doc__write", version="", description="写文档"),
                ],
                executor=AgentExecutor(kind="codex", version="0.9.3"),
            )
        )
        self.assertEqual([card.skill for card in agent.capability_cards], ["python", "doc-write"])
        self.assertEqual(agent.capability_cards[0].version, "3.11")
        # supported_tools 是"卡片技能 ∪ 老写法"的派生视图（归一化、去重）
        self.assertEqual(sorted(agent.supported_tools), ["doc-write", "extra-tool", "python"])
        self.assertEqual(agent.package_id, "codex@0.9.3")
        self.assertEqual(agent.package_source, "reported")
        self.assertEqual(agent.instance_id, "agent-cards")

    def test_invalid_card_skill_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentRegister(
                agent_id="agent-bad",
                display_name="坏技能名",
                capability_cards=[CapabilityCard(skill="有中文", version="1")],
            )

    def test_legacy_register_infers_package_from_device_probe(self) -> None:
        """老内核不发 executor：包信息按设备探测值推断，并标成 inferred。"""

        self.store.register_agent(AgentRegister(agent_id="agent-legacy", display_name="老内核", owner_member_id=self.lead.id))
        self.attach_device("agent-legacy", capabilities=["task.claim"], adapter_versions={"Codex": "0.9.1"})
        self.store._backfill_agent_packages()
        row = self.store.db.execute("SELECT package_id, package_source FROM agents WHERE agent_id = 'agent-legacy'").fetchone()
        self.assertEqual(row["package_id"], "codex@0.9.1")
        self.assertEqual(row["package_source"], "inferred")

    def test_reregister_does_not_downgrade_reported_package(self) -> None:
        self.store.register_agent(
            AgentRegister(
                agent_id="agent-keep",
                display_name="上报过的执行体",
                owner_member_id=self.lead.id,
                executor=AgentExecutor(kind="cli", version="0.4"),
            )
        )
        agent = self.store.register_agent(AgentRegister(agent_id="agent-keep", display_name="上报过的执行体", owner_member_id=self.lead.id))
        self.assertEqual(agent.package_id, "cli@0.4")
        self.assertEqual(agent.package_source, "reported")

    def test_unknown_package_stays_unknown_instead_of_fake_grouping(self) -> None:
        """既没上报、设备也探测不到：包身份留空，不按 agent_id 前缀造一个假的分组。"""

        agent = self.store.register_agent(AgentRegister(agent_id="agent-mystery", display_name="未知执行体", owner_member_id=self.lead.id))
        self.assertIsNone(agent.package_id)
        self.store._backfill_agent_packages()
        row = self.store.db.execute("SELECT package_id FROM agents WHERE agent_id = 'agent-mystery'").fetchone()
        self.assertIsNone(row["package_id"])
        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        entry = next(item for item in catalog["agents"] if item["agent_id"] == "agent-mystery")
        self.assertIsNone(entry["package_id"])
        self.assertFalse([item for item in catalog["packages"] if item["package_id"] in {"unknown", "legacy:agent"}])

    def test_device_probe_fills_previously_unknown_package(self) -> None:
        """设备上报运行态后，未知的包被补成探测到的具体版本（`_upgrade_agent_package`）。"""

        self.store.register_agent(AgentRegister(agent_id="agent-late", display_name="后补执行体", owner_member_id=self.lead.id))
        self.attach_device("agent-late", capabilities=["task.claim"], adapter_versions={"codex": "0.9.9"})
        self.store._upgrade_agent_package("agent-late")
        self.store.db.commit()
        row = self.store.db.execute("SELECT package_id, package_source FROM agents WHERE agent_id = 'agent-late'").fetchone()
        self.assertEqual((row["package_id"], row["package_source"]), ("codex@0.9.9", "inferred"))


class SkillVocabularyTests(AgentDescriptionFixture):
    def test_case_insensitive_matching_without_backfill(self) -> None:
        """历史库里的 `Python` 与要求里的 `python` 是同一种能力。"""

        self.store.register_agent(
            AgentRegister(agent_id="agent-case", display_name="大小写执行体", owner_member_id=self.lead.id, supported_tools=["Python"])
        )
        self.grant_project("agent-case")
        task = self.make_task("大小写无关", required_capabilities=["python"])
        claimed, _ = self.store.claim_task(task.id, self.claim_request("agent-case"))
        self.assertEqual(claimed.status, "CLAIMED")

    def test_version_constraint_blocks_and_allows(self) -> None:
        self.store.register_agent(
            AgentRegister(
                agent_id="agent-version",
                display_name="版本执行体",
                owner_member_id=self.lead.id,
                capability_cards=[CapabilityCard(skill="codex", version="1.2")],
            )
        )
        self.grant_project("agent-version")
        allowed = self.make_task("版本够", required_capabilities=["codex@>=1.0"])
        blocked = self.make_task("版本不够", required_capabilities=["codex@>=2.0"])
        self.store.claim_task(allowed.id, self.claim_request("agent-version"))
        with self.assertRaises(PermissionError):
            self.store.claim_task(blocked.id, self.claim_request("agent-version"))

    def test_scope_grant_is_not_a_skill(self) -> None:
        """有 `artifact.write` **授权**不代表有 `artifact.write` **技能**（词表混淆的误匹配）。"""

        self.store.register_agent(AgentRegister(agent_id="agent-scope", display_name="只有授权", owner_member_id=self.lead.id))
        self.grant_project("agent-scope")
        self.attach_device("agent-scope", capabilities=["task.claim", "artifact.write"])
        self.store.db.execute(
            "INSERT OR REPLACE INTO device_runtime_state (device_id, connection_id, adapter_versions, capabilities, reported_at) "
            "SELECT d.device_id, NULL, '{}', ?, '2026-01-01T00:00:00+00:00' FROM devices d WHERE d.agent_id = 'agent-scope'",
            (json.dumps(["task.claim", "artifact.write"]),),
        )
        self.store.db.commit()
        self.assertIn("artifact.write", self.store._agent_scope_set("agent-scope"))
        self.assertNotIn("artifact.write", self.store._agent_skill_set("agent-scope"))
        task = self.make_task("要求授权范围当技能", required_capabilities=["artifact.write"])
        with self.assertRaises(PermissionError):
            self.store.claim_task(task.id, self.claim_request("agent-scope"))

    def test_catalog_splits_skills_and_scopes(self) -> None:
        self.store.register_agent(
            AgentRegister(
                agent_id="agent-split",
                display_name="分栏执行体",
                owner_member_id=self.lead.id,
                capability_cards=[CapabilityCard(skill="python", version="3.12")],
            )
        )
        self.grant_project("agent-split")
        self.attach_device("agent-split", capabilities=["task.claim"])
        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        entry = next(item for item in catalog["agents"] if item["agent_id"] == "agent-split")
        self.assertEqual(entry["skill_versions"], {"python": "3.12"})
        self.assertIn("task.claim", entry["scope_capabilities"])
        self.assertNotIn("task.claim", entry["capabilities"])

    def test_unmet_task_reports_normalized_missing_skill(self) -> None:
        """要求侧写入时就归一化（`R-Language` → `r-language`），没人能跑时按归一化后的名字报告。"""

        self.store.register_agent(AgentRegister(agent_id="agent-none", display_name="无技能", owner_member_id=self.lead.id))
        task = self.make_task("需要 r", required_capabilities=["R-Language"])
        self.assertEqual(task.required_capabilities, ["r-language"])
        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        unmet = next(item for item in catalog["unmet_tasks"] if item["title"] == "需要 r")
        self.assertEqual(unmet["required_capabilities"], ["r-language"])
        self.assertEqual(unmet["missing_capabilities"], ["r-language"])

    def test_satisfied_requirement_drops_task_from_unmet(self) -> None:
        self.store.register_agent(
            AgentRegister(agent_id="agent-r", display_name="R 执行体", owner_member_id=self.lead.id, supported_tools=["r-language"])
        )
        self.make_task("需要 r", required_capabilities=["R-Language"])
        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        self.assertFalse([item for item in catalog["unmet_tasks"] if item["title"] == "需要 r"])


class CandidateRankingTests(AgentDescriptionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.other = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="cand@example.local", display_name="候选成员"
            )
        )
        self.store.add_project_member(self.project.id, self.other.id, "contributor")

    def test_candidates_split_satisfied_and_partial_and_stay_offline_visible(self) -> None:
        self.store.register_agent(
            AgentRegister(agent_id="agent-ok", display_name="能跑", owner_member_id=self.lead.id, supported_tools=["python"])
        )
        self.store.register_agent(
            AgentRegister(agent_id="agent-half", display_name="差一半", owner_member_id=self.other.id, supported_tools=["python"])
        )
        self.store.db.execute("UPDATE agents SET status = 'offline' WHERE agent_id = 'agent-half'")
        self.store.db.commit()
        self.grant_project("agent-ok")
        self.grant_project("agent-half")
        task = self.make_task("需要 python 与 r", required_capabilities=["python", "r-language"])
        row = self.store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task.id),)).fetchone()
        ranked = self.store.rank_task_candidates(self.project.id, row)
        self.assertEqual(ranked["satisfied"], [])
        self.assertEqual([item["agent_id"] for item in ranked["partial"]], ["agent-ok", "agent-half"])
        offline = next(item for item in ranked["partial"] if item["agent_id"] == "agent-half")
        self.assertFalse(offline["online"])
        self.assertIn("离线", offline["reason"])
        self.assertEqual(sorted(offline["missing_skills"]), ["r-language"])

    def test_ranking_is_deterministic_and_prefers_higher_success_rate(self) -> None:
        for agent_id, member in (("agent-a", self.lead.id), ("agent-b", self.other.id)):
            self.store.register_agent(
                AgentRegister(agent_id=agent_id, display_name=agent_id, owner_member_id=member, supported_tools=["python"])
            )
            self.grant_project(agent_id)
        # 给 agent-b 记两次成功、给 agent-a 记两次失败（同一项目、同一任务）
        empty = "[]"
        for index, (agent_id, status) in enumerate(
            [("agent-a", "FAILED"), ("agent-a", "FAILED"), ("agent-b", "SUCCEEDED"), ("agent-b", "SUCCEEDED")]
        ):
            self.store.db.execute(
                "INSERT INTO runs (id, project_id, task_id, agent_id, status, input_artifact_ids, parameters, tool_versions, "
                "network_policy, execution_profile, data_access_policy, observed_input_files, output_artifact_ids, stdout, stderr, "
                "summary, information_boundary, started_at, completed_at) VALUES (?, ?, NULL, ?, ?, ?, '{}', '{}', 'deny-by-default', '{}', "
                "'{}', ?, ?, '', '', '', '{}', ?, ?)",
                (str(uuid4()), str(self.project.id), agent_id, status, empty, empty, empty, f"2026-01-0{index + 1}T00:00:00+00:00", f"2026-01-0{index + 1}T01:00:00+00:00"),
            )
        self.store.db.commit()
        task = self.make_task("挑成功率高的", required_capabilities=["python"])
        row = self.store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task.id),)).fetchone()
        first = self.store.rank_task_candidates(self.project.id, row)
        second = self.store.rank_task_candidates(self.project.id, row)
        self.assertEqual(first["satisfied"], second["satisfied"])
        self.assertEqual([item["agent_id"] for item in first["satisfied"]], ["agent-b", "agent-a"])
        self.assertEqual(first["satisfied"][0]["runs_total"], 2)
        # 调度器与界面共用排序：派给排第一的那一台
        match = self.store._auto_dispatch_candidate(self.project.id, row)
        self.assertEqual(match[1], "agent-b")

    def test_candidate_preview_attached_to_unmet_task(self) -> None:
        self.store.register_agent(
            AgentRegister(agent_id="agent-close", display_name="差一点", owner_member_id=self.lead.id, supported_tools=["python"])
        )
        self.grant_project("agent-close")
        self.make_task("缺 r", required_capabilities=["python", "r-language"])
        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        unmet = next(item for item in catalog["unmet_tasks"] if item["title"] == "缺 r")
        self.assertEqual([item["agent_id"] for item in unmet["candidates"]], ["agent-close"])
        self.assertEqual(unmet["candidates"][0]["missing_skills"], ["r-language"])


class AgentDescriptionHttpTests(AgentDescriptionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.previous_store = main.store
        main.store = self.store
        self.addCleanup(self._restore_store)
        self.client = TestClient(main.app)
        # `/api/team/capabilities` 走会话解析（团队级视图），所以要有真实令牌
        self.headers = {"Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=self.lead.id)).token}"}

    def _restore_store(self) -> None:
        main.store = self.previous_store
        self.client.close()

    def test_register_rejects_invalid_skill_name(self) -> None:
        response = self.client.post(
            "/api/agents/register",
            json={
                "agent_id": "agent-http-bad",
                "display_name": "坏技能",
                "owner_member_id": self.lead.id,
                "capability_cards": [{"skill": "有中文", "version": "1"}],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("capability_skill_invalid", response.text)

    def test_register_with_cards_then_catalog_exposes_them(self) -> None:
        response = self.client.post(
            "/api/agents/register",
            json={
                "agent_id": "agent-http-ok",
                "display_name": "带卡执行体",
                "owner_member_id": self.lead.id,
                "capability_cards": [{"skill": "Codex", "version": "0.9.3", "inputs": ["problem_source"], "outputs": ["code"]}],
                "executor": {"kind": "codex", "version": "0.9.3"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["capability_cards"][0]["skill"], "codex")
        self.assertEqual(body["package_id"], "codex@0.9.3")
        self.assertEqual(body["package_source"], "reported")
        catalog = self.client.get("/api/team/capabilities", headers=self.headers)
        self.assertEqual(catalog.status_code, 200)
        payload = catalog.json()
        entry = next(item for item in payload["agents"] if item["agent_id"] == "agent-http-ok")
        self.assertEqual(entry["skill_versions"], {"codex": "0.9.3"})
        self.assertTrue(any(item["package_id"] == "codex@0.9.3" for item in payload["packages"]))

    def test_task_candidates_endpoint(self) -> None:
        self.store.register_agent(
            AgentRegister(agent_id="agent-http-cand", display_name="候选", owner_member_id=self.lead.id, supported_tools=["python"])
        )
        self.grant_project("agent-http-cand")
        task = self.make_task("端点候选", required_capabilities=["python"])
        response = self.client.get(f"/api/tasks/{task.id}/candidates")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["task_id"], str(task.id))
        self.assertEqual([item["agent_id"] for item in payload["satisfied"]], ["agent-http-cand"])
        self.assertEqual(payload["required_capabilities"], ["python"])
        missing = self.client.get(f"/api/tasks/{uuid4()}/candidates")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
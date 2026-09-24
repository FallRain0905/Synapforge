"""阶段 9 最小切片：健康/可观测、配额与成本统计契约测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app import main, observability
from app.contracts import ArtifactCreate, RunCreate
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                "PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT",
                "PLATFORM_QUOTA_MAX_ARTIFACTS_PER_PROJECT",
                "PLATFORM_QUOTA_MAX_STORAGE_BYTES_PER_PROJECT",
                "PLATFORM_COST_PER_RUN",
                "PLATFORM_COST_PER_GB_MONTH",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- 健康与平台指标 ---------------------------------------------------

    def test_health_endpoint_reports_key_counts(self) -> None:
        health = main.platform_health()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["version"])
        self.assertGreaterEqual(health["metrics"]["projects"], 1)
        self.assertIn("pending_outbox", health)

    def test_platform_metrics_aggregates_facts(self) -> None:
        self.store.create_artifact(
            self.project.id, ArtifactCreate(name="MODELING_REPORT.md", artifact_type="model_spec"), created_by="member-001"
        )
        metrics = main.platform_metrics_endpoint()
        self.assertGreaterEqual(metrics["counts"]["artifacts"], 1)
        self.assertIn("tasks_by_status", metrics)
        self.assertIn("runs_by_status", metrics)
        self.assertIn("quotas", metrics)
        self.assertIsInstance(metrics["storage_bytes"], int)

    def test_quota_endpoint_exposes_limits_and_rates(self) -> None:
        payload = main.platform_quota()
        self.assertIn("max_runs_per_project", payload["limits"])
        self.assertIn("cost_per_run", payload["cost_rates"])
        self.assertIn("runs", payload["resources"])

    # ---- 项目用量与成本 ---------------------------------------------------

    def test_project_usage_counts_artifacts_storage_and_tokens(self) -> None:
        # 用全新项目，避免种子项目自带成果物影响计数断言。
        self.project = self.store.create_project(
            main.ProjectCreate(name="用量统计项目", competition_pack="cumcm-2026", problem_code="C", description="x")
        )
        artifact = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="result1.xlsx", artifact_type="result_table"), created_by="member-001"
        )
        self.store.store_artifact_content(artifact.id, b"problem one results\n" * 10)
        self.store.register_agent(
            main.AgentRegister(agent_id="agent-001", display_name="用量统计 Agent", owner_member_id="member-001")
        )
        self.store.grant_agent_project(
            main.AgentProjectGrant(
                project_id=self.project.id, agent_id="agent-001", granted_by="member-001", capabilities=["task.claim"]
            )
        )
        usage = main.project_usage_endpoint(self.project.id)
        self.assertEqual(usage["counts"]["artifacts"], 1)
        self.assertGreater(usage["storage"]["artifact_bytes"], 0)
        self.assertEqual(usage["counts"]["capability_tokens"], 1)
        self.assertEqual(usage["estimated_cost"]["currency"], "CNY")
        self.assertIn("note", usage["estimated_cost"])
        self.assertIn("quotas", usage)

    def test_project_usage_applies_configured_rates(self) -> None:
        os.environ["PLATFORM_COST_PER_RUN"] = "1.5"
        os.environ["PLATFORM_COST_PER_GB_MONTH"] = "10"
        usage = main.project_usage_endpoint(self.project.id)
        self.assertEqual(usage["estimated_cost"]["basis"]["cost_per_run"], 1.5)
        self.assertEqual(usage["estimated_cost"]["basis"]["cost_per_gb_month"], 10.0)

    # ---- 配额判定与写路径 -------------------------------------------------

    def test_quota_limits_can_be_overridden_by_env(self) -> None:
        os.environ["PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT"] = "7"
        self.assertEqual(observability.quota_limits()["max_runs_per_project"], 7)
        os.environ["PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT"] = "not-a-number"
        self.assertEqual(observability.quota_limits()["max_runs_per_project"], observability.DEFAULT_QUOTAS["max_runs_per_project"])

    def test_enforce_quota_raises_before_limit(self) -> None:
        os.environ["PLATFORM_QUOTA_MAX_ARTIFACTS_PER_PROJECT"] = "1"
        self.store.create_artifact(
            self.project.id, ArtifactCreate(name="one.md", artifact_type="paper_source"), created_by="member-001"
        )
        with self.assertRaises(observability.QuotaExceeded) as caught:
            observability.enforce_quota(self.store, self.project.id, resource="artifacts")
        self.assertEqual(caught.exception.code, "quota_exceeded_artifacts")

    def test_artifact_route_returns_429_when_quota_exceeded(self) -> None:
        os.environ["PLATFORM_QUOTA_MAX_ARTIFACTS_PER_PROJECT"] = "1"
        self.store.create_artifact(
            self.project.id, ArtifactCreate(name="one.md", artifact_type="paper_source"), created_by="member-001"
        )
        with self.assertRaises(HTTPException) as caught:
            main.create_artifact(
                self.project.id,
                ArtifactCreate(name="two.md", artifact_type="paper_source"),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("quota_exceeded_artifacts", str(caught.exception.detail))

    def test_run_route_returns_429_when_quota_exceeded(self) -> None:
        os.environ["PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT"] = "0"
        with self.assertRaises(HTTPException) as caught:
            main.create_run(
                self.project.id,
                RunCreate(task_id=None, agent_id="agent-001", summary="超限运行", idempotency_key="quota-run-00001"),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("quota_exceeded_runs", str(caught.exception.detail))

    def test_unknown_quota_resource_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            observability.enforce_quota(self.store, self.project.id, resource="not-a-resource")

    def test_quota_status_reports_exceeded_flags(self) -> None:
        os.environ["PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT"] = "0"
        status = observability.quota_status(self.store, self.project.id)
        self.assertTrue(status["exceeded"]["runs"])
        self.assertFalse(status["exceeded"]["artifacts"])


if __name__ == "__main__":
    unittest.main()
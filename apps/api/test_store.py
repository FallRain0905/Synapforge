from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from app.contracts import AgentProjectGrant, AgentRegister, ArtifactCreate, ExecutionProfile, HandoffCreate, ProjectCreate, RunComplete, RunCreate, TaskClaimRequest, TaskCreate, TaskProgressRequest, TaskResultSubmit, TaskStatus
from app.cumcm_importer import CumcmImporter
from app.outbox import EventOutboxDispatcher
from app.store import Store


class StoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_seed_project_contains_cumcm_workflow(self) -> None:
        tasks = self.store.list_tasks(self.project.id)
        artifacts = self.store.list_artifacts(self.project.id)
        agents = self.store.list_agents()
        self.assertEqual(self.project.competition_pack, "cumcm-2026")
        self.assertGreaterEqual(len(tasks), 5)
        self.assertGreaterEqual(len(artifacts), 5)
        self.assertEqual(len(agents), 3)

    def test_task_artifact_handoff_and_event_lineage(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="测试任务", stage="review", output_types=["audit_report"]))
        self.assertEqual(task.status, TaskStatus.READY)
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="test.json", artifact_type="audit_report", task_id=task.id))
        self.assertEqual(len(artifact.content_hash), 64)
        handoff = self.store.create_handoff(self.project.id, HandoffCreate(task_id=task.id, objective="测试交接", output_artifacts=[str(artifact.id)]))
        self.assertEqual(handoff.task_id, task.id)
        events = self.store.list_events(self.project.id)
        self.assertTrue(any(event.event_type == "task.created" for event in events))
        self.assertTrue(any(event.event_type == "artifact.created" for event in events))
        self.assertTrue(any(event.event_type == "handoff.created" for event in events))

    def test_event_outbox_is_atomic_and_retryable(self) -> None:
        before_artifacts = len(self.store.list_artifacts(self.project.id))
        before_events = len(self.store.list_events(self.project.id))
        before_outbox = len(self.store.list_event_outbox(self.project.id))
        with patch.object(self.store, "_insert_event", side_effect=RuntimeError("event_insert_failed")):
            with self.assertRaisesRegex(RuntimeError, "event_insert_failed"):
                self.store.create_artifact(
                    self.project.id,
                    ArtifactCreate(name="atomic-outbox.json", artifact_type="result_table"),
                )
        self.assertEqual(len(self.store.list_artifacts(self.project.id)), before_artifacts)
        self.assertEqual(len(self.store.list_events(self.project.id)), before_events)
        self.assertEqual(len(self.store.list_event_outbox(self.project.id)), before_outbox)

        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="retryable-outbox.json", artifact_type="result_table"),
        )
        event = next(item for item in self.store.list_events(self.project.id) if item.object_id == artifact.id)
        outbox = next(item for item in self.store.list_event_outbox(self.project.id) if item.event_id == event.id)
        self.assertEqual(outbox.status, "PENDING")
        failed = self.store.mark_event_outbox_failed(
            outbox.id,
            "temporary delivery failure",
            retry_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.attempts, 1)
        self.assertIn(outbox.id, {item.id for item in self.store.list_pending_event_outbox()})
        delivered = self.store.mark_event_outbox_delivered(outbox.id)
        self.assertEqual(delivered.status, "DELIVERED")
        self.assertEqual(self.store.mark_event_outbox_delivered(outbox.id).status, "DELIVERED")

    def test_event_outbox_dispatcher_claims_publishes_and_retries(self) -> None:
        timestamp = datetime.now(UTC).isoformat()
        self.store.db.execute("UPDATE event_outbox SET status = 'DELIVERED', delivered_at = ?, updated_at = ?", (timestamp, timestamp))
        self.store.db.commit()

        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="dispatchable.json", artifact_type="result_table"),
        )
        event = next(item for item in self.store.list_events(self.project.id) if item.object_id == artifact.id)

        class Publisher:
            def __init__(self) -> None:
                self.events = []
                self.fail = True

            def publish(self, value) -> None:
                if self.fail:
                    raise RuntimeError("publisher_unavailable")
                self.events.append(value)

        publisher = Publisher()
        dispatcher = EventOutboxDispatcher(self.store, publisher, retry_base_seconds=1, retry_max_seconds=2)
        failed = dispatcher.dispatch_once(limit=1)
        self.assertEqual(failed, type(failed)(claimed=1, delivered=0, failed=1))
        outbox = next(item for item in self.store.list_event_outbox(self.project.id) if item.event_id == event.id)
        self.assertEqual(outbox.status, "FAILED")
        self.assertEqual(outbox.attempts, 1)

        self.store.db.execute("UPDATE event_outbox SET available_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), str(outbox.id)))
        self.store.db.commit()
        publisher.fail = False
        delivered = dispatcher.dispatch_once(limit=1)
        self.assertEqual(delivered.claimed, 1)
        self.assertEqual(delivered.delivered, 1)
        self.assertEqual(publisher.events[0].id, event.id)

    def test_event_outbox_reclaims_expired_processing_lock(self) -> None:
        timestamp = datetime.now(UTC).isoformat()
        self.store.db.execute("UPDATE event_outbox SET status = 'DELIVERED', delivered_at = ?, updated_at = ?", (timestamp, timestamp))
        self.store.db.commit()
        self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="lock-expiry.json", artifact_type="result_table"),
        )
        first = self.store.claim_event_outbox(limit=1, lock_seconds=60)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].status, "PROCESSING")
        self.assertEqual(self.store.claim_event_outbox(limit=1, lock_seconds=60), [])
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.store.db.execute("UPDATE event_outbox SET lock_expires_at = ? WHERE id = ?", (expired, str(first[0].id)))
        self.store.db.commit()
        reclaimed = self.store.claim_event_outbox(limit=1, lock_seconds=60)
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].id, first[0].id)
        self.assertEqual(reclaimed[0].status, "PROCESSING")

    def test_task_status_update_is_recorded(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="状态测试"))
        updated = self.store.update_task(task.id, TaskStatus.RUNNING, "agent-test", None)
        self.assertEqual(updated.status, TaskStatus.RUNNING)
        self.assertEqual(updated.assignee, "agent-test")

    def test_agent_claim_progress_result_and_idempotency(self) -> None:
        agent = self.store.register_agent(AgentRegister(agent_id="agent-test", display_name="测试 Agent"))
        self.store.grant_agent_project(AgentProjectGrant(agent_id=agent.agent_id, project_id=self.project.id, granted_by="member-001"))
        task = self.store.create_task(self.project.id, TaskCreate(title="可执行任务", assignee="Unassigned", output_types=["result_table"]))
        claim_data = TaskClaimRequest(agent_id=agent.agent_id, lease_seconds=120, idempotency_key="claim-test-001")
        claimed_task, lease = self.store.claim_task(task.id, claim_data)
        duplicate_task, duplicate_lease = self.store.claim_task(task.id, claim_data)
        self.assertEqual(claimed_task.id, duplicate_task.id)
        self.assertEqual(lease.lease_token, duplicate_lease.lease_token)
        running = self.store.update_task_progress(task.id, TaskProgressRequest(agent_id=agent.agent_id, lease_token=lease.lease_token, status="RUNNING", idempotency_key="progress-test-001"))
        self.assertEqual(running.status, TaskStatus.RUNNING)
        result = self.store.submit_task_result(task.id, TaskResultSubmit(agent_id=agent.agent_id, lease_token=lease.lease_token, success=True, summary="完成", idempotency_key="result-test-001"))
        self.assertTrue(result.accepted)
        self.assertEqual(result.task.status, TaskStatus.WAITING_REVIEW)
        duplicate_result = self.store.submit_task_result(task.id, TaskResultSubmit(agent_id=agent.agent_id, lease_token=lease.lease_token, success=True, summary="完成", idempotency_key="result-test-001"))
        self.assertEqual(duplicate_result.task.id, result.task.id)
        self.assertEqual(duplicate_result.lease.lease_token, result.lease.lease_token)

    def test_cumcm_importer_preserves_hash_and_creates_workflow(self) -> None:
        project = self.store.create_project(ProjectCreate(name="导入测试项目", competition_pack="cumcm-2026", problem_code="C"))
        with tempfile.TemporaryDirectory() as source:
            root = Path(source)
            (root / "code").mkdir()
            (root / "paper").mkdir()
            (root / "PROBLEM_ANALYSIS.md").write_text("# analysis", encoding="utf-8")
            (root / "PROBLEM_FACTS.json").write_text('{"information_boundary": {"future_data": "deny"}}', encoding="utf-8")
            (root / "code" / "solve.py").write_text("print('ok')", encoding="utf-8")
            (root / "paper" / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
            summary = CumcmImporter(self.store).import_project(project.id, source)
            self.assertGreaterEqual(summary.imported_artifacts, 4)
            self.assertEqual(summary.created_tasks, 5)
            artifacts = self.store.list_artifacts(project.id)
            self.assertTrue(any(artifact.name == "code/solve.py" and len(artifact.content_hash) == 64 for artifact in artifacts))
            self.assertTrue(any(artifact.name == "PROBLEM_FACTS.json" and artifact.data_policy["future_data"] == "deny" for artifact in artifacts))
            imported_code = next(artifact for artifact in artifacts if artifact.name == "code/solve.py")
            self.assertEqual(self.store.get_artifact_content(imported_code.id), b"print('ok')")

    def test_run_manifest_records_environment_and_blocks_undeclared_input(self) -> None:
        agent = self.store.register_agent(AgentRegister(agent_id="agent-run", display_name="运行 Agent"))
        self.store.grant_agent_project(AgentProjectGrant(agent_id=agent.agent_id, project_id=self.project.id, granted_by="member-001"))
        task = self.store.create_task(self.project.id, TaskCreate(title="运行任务", assignee=agent.agent_id))
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="input.csv", artifact_type="problem_source", source_path=str(Path(self.temp_dir.name) / "input.csv"), data_policy={"future_data": "deny"}))
        run = self.store.create_run(self.project.id, RunCreate(task_id=task.id, agent_id=agent.agent_id, source_commit="abc123", input_artifact_ids=[artifact.id], environment_image_digest="sha256:test", dependency_lock="numpy==2", parameters={"alpha": 0.2}, random_seed=42, tool_versions={"python": "3.12"}, execution_profile={"mode": "HEADLESS", "network_policy": "deny-by-default"}, observed_input_files=[artifact.source_path], idempotency_key="run-create-001"))
        self.assertEqual(run.status, "RUNNING")
        self.assertEqual(run.execution_profile.mode, "HEADLESS")
        finished = self.store.complete_run(run.id, RunComplete(success=True, stdout="ok", summary="可复现", observed_input_files=[artifact.source_path]))
        self.assertEqual(finished.status, "SUCCEEDED")
        self.assertEqual(finished.random_seed, 42)
        blocked = self.store.create_run(self.project.id, RunCreate(task_id=task.id, agent_id=agent.agent_id, input_artifact_ids=[artifact.id], observed_input_files=[str(Path(self.temp_dir.name) / "undeclared.csv")], idempotency_key="run-create-002"))
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertFalse(blocked.information_boundary["allowed"])

    def test_run_execution_profile_keeps_legacy_network_policy_compatible(self) -> None:
        legacy = RunCreate(agent_id="agent-legacy", network_policy="allow-listed", idempotency_key="legacy-profile-001")
        self.assertEqual(legacy.execution_profile.network_policy, "allow-listed")
        explicit = RunCreate(
            agent_id="agent-explicit",
            execution_profile=ExecutionProfile(network_policy="allow-listed"),
            idempotency_key="explicit-profile-001",
        )
        self.assertEqual(explicit.network_policy, "allow-listed")
        with self.assertRaisesRegex(ValueError, "run_network_policy_mismatch"):
            RunCreate(
                agent_id="agent-conflict",
                network_policy="deny-by-default",
                execution_profile=ExecutionProfile(network_policy="allow-listed"),
                idempotency_key="conflicting-profile-001",
            )


if __name__ == "__main__":
    unittest.main()

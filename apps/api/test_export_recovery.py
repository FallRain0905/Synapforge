from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    ArtifactCreate,
    ExecutionProfile,
    EvidenceCreate,
    GitRepositoryCreate,
    HandoffCreate,
    ReviewCreate,
    RunComplete,
    RunCreate,
    TaskCreate,
)
from app.store import Store


class ProjectExportRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = Store(self.root / "source" / "platform.db")
        self.project = self.source.list_projects()[0]

    def tearDown(self) -> None:
        self.source.close()
        self.temp_dir.cleanup()

    def _build_complete_project(self) -> tuple[Path, dict[str, str]]:
        agent = self.source.register_agent(AgentRegister(agent_id="agent-export", display_name="Export Agent"))
        self.source.grant_agent_project(AgentProjectGrant(agent_id=agent.agent_id, project_id=self.project.id, capabilities=["task.claim", "run.create"], granted_by="member-001"))
        task = self.source.create_task(self.project.id, TaskCreate(title="Export recovery task", assignee=agent.agent_id, output_types=["result_table"]))
        input_artifact = self.source.create_artifact(self.project.id, ArtifactCreate(name="export-input.csv", artifact_type="problem_source", task_id=task.id, mime_type="text/csv", data_policy={"future_data": "deny"}))
        input_artifact = self.source.store_artifact_content(input_artifact.id, b"time,value\n1,2\n", "text/csv")
        run = self.source.create_run(self.project.id, RunCreate(task_id=task.id, agent_id=agent.agent_id, input_artifact_ids=[input_artifact.id], environment_image_digest="sha256:test", random_seed=7, execution_profile=ExecutionProfile(auto_retry=True), idempotency_key="export-run-001"))
        output_artifact = self.source.create_artifact(self.project.id, ArtifactCreate(name="export-result.json", artifact_type="result_table", task_id=task.id, run_id=run.id, input_artifact_ids=[input_artifact.id], mime_type="application/json"), created_by=agent.agent_id, created_by_kind="agent")
        output_artifact = self.source.store_artifact_content(output_artifact.id, b'{"objective": 12.5}', "application/json")
        self.source.complete_run(run.id, RunComplete(success=True, summary="complete", output_artifact_ids=[output_artifact.id]))
        self.source.create_review(self.project.id, ReviewCreate(target_type="artifact", target_id=output_artifact.id, verdict="APPROVED", summary="approved", reviewer="member-001", reviewer_kind="member"))
        handoff = self.source.create_handoff(self.project.id, HandoffCreate(task_id=task.id, receiver={"kind": "agent", "id": "agent-mira"}, objective="continue", input_artifacts=[str(input_artifact.id)], output_artifacts=[str(output_artifact.id)], evidence_refs=[str(output_artifact.id)]), sender_agent_id=agent.agent_id)
        self.source.accept_handoff(handoff.id, "agent-mira")
        self.source.create_evidence(self.project.id, EvidenceCreate(claim="objective is reproducible", evidence_type="run", run_id=run.id, created_by="member-001"))

        repo = self.root / "repo"
        repo.mkdir()
        import subprocess

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "export@example.local"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Export Test"], cwd=repo, check=True, capture_output=True)
        (repo / "solve.py").write_text("print('reproducible')\n", encoding="utf-8")
        subprocess.run(["git", "add", "solve.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "export"], cwd=repo, check=True, capture_output=True)
        self.source.register_git_repository(GitRepositoryCreate(project_id=self.project.id, local_path=str(repo)))
        self.source.index_git_repository(self.project.id)

        bundle = self.source.export_project_bundle(self.project.id, self.root / "project.zip")
        return bundle, {
            "task": str(task.id),
            "input": str(input_artifact.id),
            "output": str(output_artifact.id),
            "run": str(run.id),
            "handoff": str(handoff.id),
        }

    def test_project_bundle_restores_all_project_relations(self) -> None:
        bundle, ids = self._build_complete_project()
        restored_store = Store(self.root / "restored" / "platform.db")
        try:
            restored = restored_store.restore_project_bundle(bundle)
            self.assertEqual(restored.id, self.project.id)
            self.assertTrue(any(str(item.id) == ids["task"] for item in restored_store.list_tasks(restored.id)))
            self.assertTrue(any(str(item.id) == ids["handoff"] and item.receipt_status == "ACCEPTED" for item in restored_store.list_handoffs(restored.id)))
            artifacts = restored_store.list_artifacts(restored.id)
            self.assertTrue(any(str(item.id) == ids["output"] and item.immutable for item in artifacts))
            self.assertEqual(restored_store.get_artifact_content(UUID(ids["input"])), b"time,value\n1,2\n")
            self.assertEqual(restored_store.get_artifact_content(UUID(ids["output"])), b'{"objective": 12.5}')
            self.assertTrue(any(str(item.id) == ids["run"] and item.status == "SUCCEEDED" for item in restored_store.list_runs(restored.id)))
            restored_run = next(item for item in restored_store.list_runs(restored.id) if str(item.id) == ids["run"])
            self.assertTrue(restored_run.execution_profile.auto_retry)
            self.assertGreaterEqual(len(restored_store.list_reviews(restored.id)), 1)
            self.assertGreaterEqual(len(restored_store.list_gates(restored.id)), 1)
            self.assertGreaterEqual(len(restored_store.list_evidence(restored.id)), 1)
            self.assertEqual(len(restored_store.list_events(restored.id)), len(self.source.list_events(self.project.id, limit=100000)))
            self.assertEqual(restored_store.list_git_index(restored.id)[0].content_hash, self.source.list_git_index(self.project.id)[0].content_hash)
            self.assertEqual(restored_store.get_git_repository(restored.id).local_path, str((self.root / "repo").resolve()))
            restored_store._assert_agent_project_access("agent-export", restored.id)
        finally:
            restored_store.close()

    def test_tampered_object_is_rejected_without_partial_project(self) -> None:
        bundle, _ = self._build_complete_project()
        tampered = self.root / "tampered.zip"
        with ZipFile(bundle, "r") as source_archive, ZipFile(tampered, "w", compression=ZIP_DEFLATED) as target_archive:
            for name in source_archive.namelist():
                content = source_archive.read(name)
                target_archive.writestr(name, b"tampered" if name.startswith("objects/") else content)
        restored_store = Store(self.root / "tampered" / "platform.db")
        try:
            with self.assertRaisesRegex(ValueError, "artifact_hash_mismatch"):
                restored_store.restore_project_bundle(tampered)
            with self.assertRaises(KeyError):
                restored_store.get_project(self.project.id)
        finally:
            restored_store.close()

    def test_dangling_reference_is_rejected(self) -> None:
        bundle, _ = self._build_complete_project()
        invalid = self.root / "invalid.zip"
        with ZipFile(bundle, "r") as source_archive:
            manifest = json.loads(source_archive.read("manifest.json"))
            object_files = {name: source_archive.read(name) for name in source_archive.namelist() if name.startswith("objects/")}
        manifest["runs"][0]["input_artifact_ids"] = ["00000000-0000-4000-8000-999999999999"]
        with ZipFile(invalid, "w", compression=ZIP_DEFLATED) as target_archive:
            target_archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for name, content in object_files.items():
                target_archive.writestr(name, content)
        restored_store = Store(self.root / "invalid" / "platform.db")
        try:
            with self.assertRaisesRegex(ValueError, "bundle_run_artifact_missing"):
                restored_store.restore_project_bundle(invalid)
            with self.assertRaises(KeyError):
                restored_store.get_project(self.project.id)
        finally:
            restored_store.close()

    def test_manifest_checksum_tampering_is_rejected(self) -> None:
        bundle, _ = self._build_complete_project()
        invalid = self.root / "checksum-invalid.zip"
        with ZipFile(bundle, "r") as source_archive:
            manifest = json.loads(source_archive.read("manifest.json"))
            object_files = {name: source_archive.read(name) for name in source_archive.namelist() if name.startswith("objects/")}
        manifest["object_checksums"][0]["sha256"] = "0" * 64
        with ZipFile(invalid, "w", compression=ZIP_DEFLATED) as target_archive:
            target_archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for name, content in object_files.items():
                target_archive.writestr(name, content)
        restored_store = Store(self.root / "checksum-invalid" / "platform.db")
        try:
            with self.assertRaisesRegex(ValueError, "bundle_checksum_mismatch"):
                restored_store.restore_project_bundle(invalid)
            with self.assertRaises(KeyError):
                restored_store.get_project(self.project.id)
        finally:
            restored_store.close()

    def test_invalid_uuid_is_rejected_before_project_insert(self) -> None:
        bundle, _ = self._build_complete_project()
        invalid = self.root / "uuid-invalid.zip"
        with ZipFile(bundle, "r") as source_archive:
            manifest = json.loads(source_archive.read("manifest.json"))
            object_files = {name: source_archive.read(name) for name in source_archive.namelist() if name.startswith("objects/")}
        manifest["tasks"][0]["id"] = "not-a-uuid"
        with ZipFile(invalid, "w", compression=ZIP_DEFLATED) as target_archive:
            target_archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for name, content in object_files.items():
                target_archive.writestr(name, content)
        restored_store = Store(self.root / "uuid-invalid" / "platform.db")
        try:
            with self.assertRaisesRegex(ValueError, "invalid_bundle_task_id"):
                restored_store.restore_project_bundle(invalid)
            with self.assertRaises(KeyError):
                restored_store.get_project(self.project.id)
        finally:
            restored_store.close()


if __name__ == "__main__":
    unittest.main()

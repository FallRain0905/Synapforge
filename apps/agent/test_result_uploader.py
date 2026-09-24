import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from .local_state import LocalAgentState
    from .result_uploader import DiscoveredOutput, OutputDiscovery, ResultUploader, RunManifestBuilder, reproducibility_hash
except ImportError:
    from local_state import LocalAgentState
    from result_uploader import DiscoveredOutput, OutputDiscovery, ResultUploader, RunManifestBuilder, reproducibility_hash


class FakeArtifactClient:
    def __init__(self) -> None:
        self.created_statuses: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.uploaded: list[str] = []
        self.next_id = 1
        self.fail_upload_once = False

    def create(self, project_id: str, output: DiscoveredOutput, *, task_id: str | None, run_id: str, status: str = "DRAFT") -> dict:
        self.created_statuses.append(status)
        artifact_id = f"artifact-{self.next_id:03d}"
        self.next_id += 1
        self.created.append((project_id, output.relative_path))
        return {"id": artifact_id}

    def upload_content(self, artifact_id: str, output: DiscoveredOutput) -> dict:
        if self.fail_upload_once:
            self.fail_upload_once = False
            raise RuntimeError("temporary_upload_failure")
        self.uploaded.append(artifact_id)
        return {"id": artifact_id}


class ResultUploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = LocalAgentState(self.root / "agent.db")

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def test_output_discovery_hashes_explicit_files_and_rejects_escape(self) -> None:
        result = self.root / "result.json"
        result.write_text('{"ok": true}', encoding="utf-8")
        output = OutputDiscovery(self.root).discover(["result.json"])[0]
        self.assertEqual(output.relative_path, "result.json")
        self.assertEqual(output.size_bytes, result.stat().st_size)
        with self.assertRaisesRegex(ValueError, "output_path_outside_workspace"):
            OutputDiscovery(self.root).discover([self.root.parent / "outside.json"])

    def test_queue_and_retry_upload_preserve_upload_identity(self) -> None:
        result = self.root / "result.json"
        result.write_text('{"ok": true}', encoding="utf-8")
        request = SimpleNamespace(
            project_id="project-001",
            task_id="task-001",
            run_id="run-001",
            workspace_path=str(self.root),
            output_paths=("result.json",),
            command=("python", "script.py"),
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            profile=SimpleNamespace(),
            environment={},
            input_paths=(),
            timeout_seconds=None,
            max_output_bytes=1000,
        )
        client = FakeArtifactClient()
        uploader = ResultUploader(self.state, client)
        outputs = OutputDiscovery(self.root).discover(request.output_paths)
        upload_ids = uploader.queue_outputs(request.project_id, request.run_id, request, outputs)
        first = uploader.upload_pending(task_id=request.task_id, run_id=request.run_id)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].upload_id, upload_ids[0])
        self.assertEqual(self.state.get_upload(upload_ids[0])["status"], "SUCCEEDED")
        uploader.queue_outputs(request.project_id, request.run_id, request, outputs)
        self.assertEqual(uploader.upload_pending(task_id=request.task_id, run_id=request.run_id), [])
        self.assertEqual(len(client.created), 1)

    def test_changed_output_is_not_uploaded_after_manifest_queue(self) -> None:
        result = self.root / "result.csv"
        result.write_text("a,b\n1,2\n", encoding="utf-8")
        request = SimpleNamespace(
            project_id="project-001", task_id=None, run_id="run-002", workspace_path=str(self.root), output_paths=("result.csv",)
        )
        output = OutputDiscovery(self.root).discover(request.output_paths)
        uploader = ResultUploader(self.state, FakeArtifactClient())
        uploader.queue_outputs(request.project_id, request.run_id, request, output)
        result.write_text("a,b\n9,9\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "output_file_changed_after_queue"):
            uploader.upload_pending(task_id=None, run_id=request.run_id)

    def test_content_retry_reuses_created_artifact(self) -> None:
        result = self.root / "result.json"
        result.write_text('{"ok": true}', encoding="utf-8")
        request = SimpleNamespace(project_id="project-001", task_id=None, run_id="run-004")
        output = OutputDiscovery(self.root).discover([result])
        client = FakeArtifactClient()
        client.fail_upload_once = True
        uploader = ResultUploader(self.state, client)
        uploader.queue_outputs(request.project_id, request.run_id, request, output)
        with self.assertRaisesRegex(RuntimeError, "temporary_upload_failure"):
            uploader.upload_pending(task_id=None, run_id=request.run_id)
        uploaded = uploader.upload_pending(task_id=None, run_id=request.run_id)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0][1], "result.json")

    def test_manifest_contains_output_and_stream_digests(self) -> None:
        request = SimpleNamespace(
            run_id="run-003", project_id="project-001", task_id=None, agent_id="agent-001", device_id="device-001",
            workspace_id="workspace-001", workspace_path=str(self.root), command=("python", "x.py"), profile=SimpleNamespace(),
            environment={}, input_paths=(), output_paths=("result.json",), timeout_seconds=5, max_output_bytes=1000,
        )
        output = DiscoveredOutput("C:/result.json", "result.json", "result.json", "abc", 3, "application/json")
        process = SimpleNamespace(process_id="process-001", status="SUCCEEDED", exit_code=0, stdout="ok", stderr="")
        manifest = RunManifestBuilder.build(request, process, [output])
        self.assertEqual(manifest["run_id"], "run-003")
        self.assertEqual(manifest["outputs"][0]["content_hash"], "abc")
        self.assertIn("stdout_sha256", manifest["process"])

    def test_manifest_embeds_fail_closed_information_boundary_audit(self) -> None:
        request = SimpleNamespace(
            run_id="run-boundary-001", project_id="project-001", task_id=None, agent_id="agent-001", device_id="device-001",
            workspace_id="workspace-001", workspace_path=str(self.root), command=("python", "x.py"), profile=SimpleNamespace(),
            environment={}, input_paths=("input/data.csv",), output_paths=(), timeout_seconds=5, max_output_bytes=1000,
            observed_input_files=(), data_access_policy={},
        )
        process = SimpleNamespace(process_id="process-boundary-001", status="SUCCEEDED", exit_code=0, stdout="", stderr="")
        manifest = RunManifestBuilder.build(request, process, [])
        self.assertFalse(manifest["information_boundary"]["allowed"])
        self.assertEqual(manifest["information_boundary"]["violations"][0]["code"], "input_observation_not_captured")

    def test_manifest_records_observation_diagnostics_but_excludes_them_from_reproducibility(self) -> None:
        diagnostics = {
            "adapter": "windows-etw",
            "stop_status": "ok",
            "sessions": {"MathAgentAccess-1-file": {"etl_sha256": "aaa"}},
        }
        request = SimpleNamespace(
            run_id="run-diag-001", project_id="project-001", task_id=None, agent_id="agent-001", device_id="device-001",
            workspace_id="workspace-001", workspace_path=str(self.root), command=("python", "x.py"), profile=SimpleNamespace(),
            environment={}, input_paths=(), output_paths=(), timeout_seconds=5, max_output_bytes=1000,
            observed_input_files=(), data_access_policy={},
            access_observation_diagnostics=diagnostics,
        )
        process = SimpleNamespace(
            process_id="process-diag-001", status="SUCCEEDED", exit_code=0, stdout="", stderr="",
            access_observation_status="captured", access_observation_reason=None,
        )
        manifest = RunManifestBuilder.build(request, process, [])
        self.assertEqual(manifest["access_observation"]["status"], "captured")
        self.assertEqual(manifest["access_observation"]["diagnostics"], diagnostics)

        # Trace session names and ETL hashes differ between identical runs;
        # the reproducibility hash must not change with them.
        changed = SimpleNamespace(
            run_id="run-diag-001", project_id="project-001", task_id=None, agent_id="agent-001", device_id="device-001",
            workspace_id="workspace-001", workspace_path=str(self.root), command=("python", "x.py"), profile=SimpleNamespace(),
            environment={}, input_paths=(), output_paths=(), timeout_seconds=5, max_output_bytes=1000,
            observed_input_files=(), data_access_policy={},
            access_observation_diagnostics={
                "adapter": "windows-etw",
                "stop_status": "ok",
                "sessions": {"MathAgentAccess-2-file": {"etl_sha256": "bbb"}},
            },
        )
        other_manifest = RunManifestBuilder.build(changed, process, [])
        self.assertEqual(reproducibility_hash(manifest), reproducibility_hash(other_manifest))


if __name__ == "__main__":
    unittest.main()

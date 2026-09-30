from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from app.main import app
from app import main
from app.migrations import migration_files


class PlatformContractTests(unittest.TestCase):
    def test_cors_origins_default_and_environment_override(self) -> None:
        """自托管部署必须能配工作台来源，否则远程浏览器一律 "Failed to fetch"。"""

        original = os.environ.get("PLATFORM_CORS_ORIGINS")
        try:
            os.environ.pop("PLATFORM_CORS_ORIGINS", None)
            self.assertEqual(main._cors_origins(), ["http://localhost:3000", "http://127.0.0.1:3000"])
            os.environ["PLATFORM_CORS_ORIGINS"] = "http://10.0.0.5:3000, https://map.example.com/"
            self.assertEqual(main._cors_origins(), ["http://10.0.0.5:3000", "https://map.example.com"])
            os.environ["PLATFORM_CORS_ORIGINS"] = "   "
            self.assertEqual(main._cors_origins(), ["http://localhost:3000", "http://127.0.0.1:3000"])
        finally:
            if original is None:
                os.environ.pop("PLATFORM_CORS_ORIGINS", None)
            else:
                os.environ["PLATFORM_CORS_ORIGINS"] = original

    def test_postgres_migrations_cover_core_tables_and_rls(self) -> None:
        files = migration_files()
        self.assertEqual(
            [path.name for path in files],
            [
                "001_initial.sql",
                "002_rls.sql",
                "003_idempotency_request_hash.sql",
                "004_event_outbox.sql",
                "005_agent_devices.sql",
                "006_gateway_command_results.sql",
                "007_run_execution_profile.sql",
                "008_device_registration_challenge.sql",
                "009_device_token_rotation.sql",
                "010_gateway_command_request_hash.sql",
                "011_workflow_review_relations.sql",
                "012_p4_02_receipts_risks_gate_snapshots.sql",
                "013_p4_04_force_rls_and_event_idempotency.sql",
                "014_runtime_role_and_grants.sql",
                "015_device_runtime_state.sql",
                "016_document_drafts.sql",
                "017_accounts.sql",
                "018_task_dispatch.sql",
                "019_run_attribution.sql",
                "020_project_workspace.sql",
                "021_agent_description.sql",
                "022_task_intent.sql",
                "023_run_usage.sql",
                "024_agent_conversations.sql",
                "025_agent_chat_roles.sql",
                "026_agent_turn_outputs.sql",
                "027_agent_turn_approvals.sql",
                "028_agent_chat_variants.sql",
                "029_drive_nodes.sql",
                "030_agent_workspaces.sql",
                "031_drive_grants.sql",
                "032_file_transfer_parts.sql",
                "033_llm_channels.sql",
                "034_agent_turn_stop_reason.sql",
                "035_artifact_receipt.sql",
                "036_workflow_packages.sql",
                "037_workflow_engine.sql",
            ],
        )
        initial = files[0].read_text(encoding="utf-8").lower()
        rls = files[1].read_text(encoding="utf-8").lower()
        idempotency = files[2].read_text(encoding="utf-8").lower()
        outbox = files[3].read_text(encoding="utf-8").lower()
        devices = files[4].read_text(encoding="utf-8").lower()
        gateway_results = files[5].read_text(encoding="utf-8").lower()
        run_profile = files[6].read_text(encoding="utf-8").lower()
        device_challenge = files[7].read_text(encoding="utf-8").lower()
        p4_02 = files[11].read_text(encoding="utf-8").lower()
        p4_04 = files[12].read_text(encoding="utf-8").lower()
        for table in ["organizations", "projects", "tasks", "handoffs", "artifacts", "artifact_multipart_uploads", "runs", "reviews", "gates", "evidence", "events", "event_outbox"]:
            self.assertIn(f"create table if not exists {table}", initial)
            self.assertIn(f"alter table {table} enable row level security", rls)
        self.assertIn("status text not null default 'active'", initial)
        self.assertIn("completed_at timestamptz", initial)
        self.assertIn("lock_expires_at timestamptz", initial)
        self.assertIn("request_hash text", initial)
        self.assertIn("add column if not exists request_hash", idempotency)
        self.assertIn("insert into event_outbox", outbox)
        self.assertIn("add column if not exists lock_expires_at", outbox)
        self.assertIn("event_outbox_tenant_policy", outbox)
        for table in ["devices", "device_pairings", "device_project_grants", "agent_connections"]:
            self.assertIn(f"create table if not exists {table}", devices)
            self.assertIn(f"alter table {table} enable row level security", devices)
        self.assertNotIn("?", devices)
        self.assertIn("create table if not exists gateway_command_results", gateway_results)
        self.assertIn("alter table gateway_command_results enable row level security", gateway_results)
        self.assertIn("gateway_command_results_tenant_policy", gateway_results)
        self.assertNotIn("?", gateway_results)
        self.assertIn("add column if not exists execution_profile", run_profile)
        self.assertIn("jsonb", run_profile)
        self.assertIn("add column if not exists challenge_hash", device_challenge)
        # 021（AIP-1a/1c）：能力卡 + 包/实例两段身份
        agent_description = files[20].read_text(encoding="utf-8").lower()
        for column in ["capability_cards", "package_id", "instance_id", "package_source"]:
            self.assertIn(f"add column if not exists {column}", agent_description)
        self.assertIn("agents_package_id_idx", agent_description)
        self.assertIn("update agents set instance_id = agent_id", agent_description)
        self.assertNotIn("?", agent_description)
        # 032（FM-6 断点续传）：分片表 + 会话上的多分片会话 id / 分片大小 / 属主
        parts = files[31].read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists file_transfer_parts", parts)
        self.assertIn("alter table file_transfer_parts enable row level security", parts)
        self.assertIn("file_transfer_parts_unique_idx", parts)
        for column in ["multipart_upload_id", "part_size_bytes", "owner_member_id"]:
            self.assertIn(f"add column if not exists {column}", parts)
        self.assertNotIn("?", parts)
        # 031（FM-5 云盘授权）：Grant / Grant Node / Lease，三张表都要有 RLS
        grants = files[30].read_text(encoding="utf-8").lower()
        for table in ["file_access_grants", "file_access_grant_nodes", "file_access_leases"]:
            self.assertIn(f"create table if not exists {table}", grants)
            self.assertIn(f"alter table {table} enable row level security", grants)
        # 撤销靠 epoch 而不是删行；lease 只存哈希
        self.assertIn("revocation_epoch", grants)
        self.assertIn("token_hash", grants)
        self.assertNotIn("?", grants)
        # 030（FM-3 Agent 工作区）：工作区登记 + 操作队列 + 传输会话 + 审计
        workspaces = files[29].read_text(encoding="utf-8").lower()
        for table in ["agent_workspaces", "workspace_operations", "file_transfer_sessions", "workspace_audit"]:
            self.assertIn(f"create table if not exists {table}", workspaces)
            self.assertIn(f"alter table {table} enable row level security", workspaces)
        # 幂等键唯一 + 队列索引（领取走它们）
        self.assertIn("workspace_operations_idempotency_idx", workspaces)
        self.assertIn("workspace_operations_queue_idx", workspaces)
        # 平台不存宿主机绝对路径：工作区标识是哈希
        self.assertIn("workspace_identity", workspaces)
        self.assertNotIn("?", workspaces)
        # 029（FM-1 个人云盘）：节点树 + 项目引用 + 对象清理队列 + 审计，四张表都要有 RLS
        drive_nodes = files[28].read_text(encoding="utf-8").lower()
        for table in ["drive_nodes", "drive_project_refs", "drive_object_cleanup", "drive_audit"]:
            self.assertIn(f"create table if not exists {table}", drive_nodes)
            self.assertIn(f"alter table {table} enable row level security", drive_nodes)
        # 同父同名唯一只约束存活节点（回收站里的名字要能被重新用）/ 每成员一个根
        self.assertIn("drive_nodes_sibling_name_idx", drive_nodes)
        self.assertIn("where deleted_at is null and is_root = false", drive_nodes)
        self.assertIn("drive_nodes_root_idx", drive_nodes)
        self.assertNotIn("?", drive_nodes)
        # 022（AIP-1d）：意图对象的两个字段（预算 / 显式证据要求）
        task_intent = files[21].read_text(encoding="utf-8").lower()
        for column in ["budget", "evidence_requirements"]:
            self.assertIn(f"add column if not exists {column}", task_intent)
        self.assertIn("jsonb", task_intent)
        self.assertNotIn("?", task_intent)
        # 023（COST-1）：执行用量回报
        run_usage = files[22].read_text(encoding="utf-8").lower()
        self.assertIn("add column if not exists usage", run_usage)
        self.assertIn("jsonb", run_usage)
        self.assertNotIn("?", run_usage)
        for table in ["handoff_receipts", "risks"]:
            self.assertIn(f"create table if not exists {table}", p4_02)
        self.assertIn("input_snapshot", p4_02)
        self.assertNotIn("?", p4_02)
        self.assertIn("force row level security", p4_04)
        self.assertIn("agents_tenant_policy", p4_04)
        self.assertIn("sessions_tenant_policy", p4_04)
        self.assertIn("idempotency_records_tenant_policy", p4_04)
        self.assertIn("add column if not exists organization_id", p4_04)
        self.assertIn("idx_events_project_idempotency", p4_04)
        self.assertNotIn("?", p4_04)
        domain = json.loads((Path(__file__).parents[2] / "packages" / "contracts" / "domain.json").read_text(encoding="utf-8"))
        self.assertEqual(domain["device_identity_algorithms"], ["ed25519"])
        self.assertIn("INVALIDATED", domain["gate_statuses"])

    def test_shared_gateway_protocol_and_result_schema_are_present(self) -> None:
        from packages.agent_protocol import GATEWAY_COMMAND_TYPES, GatewayCommandResult, GatewayEnvelope

        self.assertEqual(len(GATEWAY_COMMAND_TYPES), 11)
        self.assertEqual(GatewayEnvelope.__module__, "packages.agent_protocol")
        self.assertIn("status", GatewayCommandResult.model_fields)
        self.assertTrue(GatewayCommandResult.model_fields["status"].is_required())

    def test_openapi_contains_stage_two_routes(self) -> None:
        paths = app.openapi()["paths"]
        expected = {
            "/api/artifacts/{artifact_id}/content",
            "/api/artifacts/{artifact_id}/multipart",
            "/api/artifacts/{artifact_id}/multipart/{upload_id}/parts/{part_number}",
            "/api/artifacts/{artifact_id}/multipart/{upload_id}/complete",
            "/api/artifacts/{artifact_id}/multipart/{upload_id}",
            "/api/artifacts/{artifact_id}/archive",
            "/api/projects/{project_id}/git",
            "/api/projects/{project_id}/git/index",
            "/api/projects/{project_id}/artifacts/{artifact_id}/versions",
            "/api/projects/{project_id}/export",
            "/api/projects/restore",
        }
        self.assertTrue(expected.issubset(paths))

    def test_openapi_contains_stage_three_device_routes(self) -> None:
        paths = app.openapi()["paths"]
        expected = {
            "/api/devices/pairings",
            "/api/devices/register",
            "/api/devices",
            "/api/devices/{device_id}/revoke",
            "/api/projects/{project_id}/device-grants",
            "/api/device-grants/{grant_id}/revoke",
        }
        self.assertTrue(expected.issubset(paths))

    def test_openapi_contains_stage_four_workflow_routes(self) -> None:
        paths = app.openapi()["paths"]
        expected = {
            "/api/projects/{project_id}/review-center",
            "/api/projects/{project_id}/risks/{risk_id}",
            "/api/agent/handoffs/{handoff_id}/accept",
            "/api/agent/handoffs/{handoff_id}/reject",
        }
        self.assertTrue(expected.issubset(paths))


if __name__ == "__main__":
    unittest.main()

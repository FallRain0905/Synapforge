from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    ArtifactCreate,
    EvidenceCreate,
    HandoffCreate,
    ReviewerKind,
    ReviewCenter,
    ReviewCreate,
    RiskDecisionRequest,
    TaskClaimRequest,
    TaskCreate,
    TaskResultSubmit,
    TaskStatus,
)
from app.store import Store


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.list_projects()[0]
        for agent_id in ["agent-a", "agent-b", "agent-c", "agent-d"]:
            self.store.register_agent(AgentRegister(agent_id=agent_id, display_name=agent_id))
            self.store.grant_agent_project(AgentProjectGrant(agent_id=agent_id, project_id=self.project.id, granted_by="member-001"))

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _approve_task(self, task_id) -> None:
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task_id,
                verdict="APPROVED",
                summary="人工确认任务结果",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.assertEqual(review.verdict, "APPROVED")
        self.assertEqual(self.store.get_task(task_id).status, TaskStatus.APPROVED)

    def _approve_artifact(self, artifact_id) -> None:
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=artifact_id,
                verdict="APPROVED",
                summary="人工确认成果物",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.assertEqual(review.verdict, "APPROVED")
        artifact = next(item for item in self.store.list_artifacts(self.project.id) if item.id == artifact_id)
        self.assertTrue(artifact.downstream_allowed)

    def test_relay_handoff_requires_human_gate_before_downstream_claim(self) -> None:
        source = self.store.create_task(
            self.project.id,
            TaskCreate(title="接力上游分析", stage="problem_analysis", output_types=["model_spec"]),
        )
        output = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="analysis.md", artifact_type="model_spec", task_id=source.id),
            created_by="agent-a",
            created_by_kind="agent",
        )
        source_task, lease = self.store.claim_task(
            source.id,
            TaskClaimRequest(agent_id="agent-a", lease_seconds=120, idempotency_key="relay-claim-001"),
        )
        self.assertEqual(source_task.status, TaskStatus.CLAIMED)
        handoff = self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=source.id,
                receiver={"type": "agent", "id": "agent-b"},
                objective="把已完成的分析交给下游 Agent",
                completed=["完成变量定义"],
                output_artifacts=[str(output.id)],
                assumptions=["尚未进行独立复核"],
                requires_human_approval=True,
            ),
            sender_agent_id="agent-a",
        )
        self.store.submit_task_result(
            source.id,
            TaskResultSubmit(
                agent_id="agent-a",
                lease_token=lease.lease_token,
                success=True,
                summary="上游分析完成",
                output_artifact_ids=[output.id],
                handoff_id=handoff.id,
                idempotency_key="relay-result-001",
            ),
        )

        with self.assertRaises(PermissionError):
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="task",
                    target_id=source.id,
                    verdict="APPROVED",
                    summary="Agent 建议通过",
                    reviewer="agent-reviewer",
                    reviewer_kind=ReviewerKind.AGENT,
                ),
            )

        self._approve_task(source.id)
        self._approve_artifact(output.id)
        handoff_review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="handoff",
                target_id=handoff.id,
                verdict="APPROVED",
                summary="人工确认交接包",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.assertEqual(handoff_review.verdict, "APPROVED")
        self.assertEqual(self.store.list_handoffs(self.project.id)[0].status, "PASS")

        downstream = self.store.create_task(
            self.project.id,
            TaskCreate(
                title="接力下游建模",
                stage="modeling",
                dependency_task_ids=[source.id],
                input_artifacts=[str(output.id)],
                input_handoff_ids=[handoff.id],
                output_types=["result_table"],
            ),
        )
        with self.assertRaises(ValueError):
            self.store.claim_task(
                downstream.id,
                TaskClaimRequest(agent_id="agent-b", lease_seconds=120, idempotency_key="relay-claim-before-accept"),
            )
        accepted_handoff = self.store.accept_handoff(handoff.id, "agent-b")
        self.assertEqual(accepted_handoff.receipt_status, "ACCEPTED")
        claimed, downstream_lease = self.store.claim_task(
            downstream.id,
            TaskClaimRequest(agent_id="agent-b", lease_seconds=120, idempotency_key="relay-claim-002"),
        )
        self.assertEqual(claimed.status, TaskStatus.CLAIMED)
        self.assertEqual(downstream_lease.agent_id, "agent-b")

    def test_fanout_requires_all_approved_inputs_and_is_idempotent(self) -> None:
        worker_tasks = [
            self.store.create_task(self.project.id, TaskCreate(title="并行子任务 B", stage="coding", output_types=["result_table"])),
            self.store.create_task(self.project.id, TaskCreate(title="并行子任务 C", stage="coding", output_types=["result_table"])),
        ]
        artifacts = []
        leases = []
        for index, (agent_id, task) in enumerate(zip(["agent-b", "agent-c"], worker_tasks), start=1):
            artifact = self.store.create_artifact(
                self.project.id,
                ArtifactCreate(name=f"parallel-{index}.json", artifact_type="result_table", task_id=task.id),
                created_by=agent_id,
                created_by_kind="agent",
            )
            artifacts.append(artifact)
            _, lease = self.store.claim_task(
                task.id,
                TaskClaimRequest(agent_id=agent_id, lease_seconds=120, idempotency_key=f"fanout-claim-{index}"),
            )
            leases.append(lease)
            self.store.submit_task_result(
                task.id,
                TaskResultSubmit(
                    agent_id=agent_id,
                    lease_token=lease.lease_token,
                    success=True,
                    output_artifact_ids=[artifact.id],
                    idempotency_key=f"fanout-result-{index}",
                ),
            )

        aggregate = self.store.create_task(
            self.project.id,
            TaskCreate(
                title="并行结果汇总",
                stage="review",
                dependency_task_ids=[task.id for task in worker_tasks],
                input_artifacts=[str(artifact.id) for artifact in artifacts],
                output_types=["audit_report"],
            ),
        )
        with self.assertRaises(ValueError):
            self.store.claim_task(
                aggregate.id,
                TaskClaimRequest(agent_id="agent-d", lease_seconds=120, idempotency_key="fanout-aggregate-early"),
            )

        for task, artifact in zip(worker_tasks, artifacts):
            self._approve_task(task.id)
            self._approve_artifact(artifact.id)

        claimed, lease = self.store.claim_task(
            aggregate.id,
            TaskClaimRequest(agent_id="agent-d", lease_seconds=120, idempotency_key="fanout-aggregate-ready"),
        )
        self.assertEqual(claimed.status, TaskStatus.CLAIMED)
        self.assertEqual(lease.agent_id, "agent-d")

        events_before_aggregate_result = len(self.store.list_events(self.project.id))
        duplicate = self.store.submit_task_result(
            aggregate.id,
            TaskResultSubmit(
                agent_id="agent-d",
                lease_token=lease.lease_token,
                success=True,
                summary="汇总完成",
                idempotency_key="fanout-result-aggregate",
            ),
        )
        events_after_first_result = len(self.store.list_events(self.project.id))
        replay = self.store.submit_task_result(
            aggregate.id,
            TaskResultSubmit(
                agent_id="agent-d",
                lease_token=lease.lease_token,
                success=True,
                summary="汇总完成",
                idempotency_key="fanout-result-aggregate",
            ),
        )
        self.assertEqual(duplicate.task.id, replay.task.id)
        self.assertEqual(duplicate.task.status, TaskStatus.WAITING_REVIEW)
        self.assertEqual(events_after_first_result, events_before_aggregate_result + 1)
        self.assertEqual(len(self.store.list_events(self.project.id)), events_after_first_result)

    def test_schema_catalog_contains_formal_core_objects(self) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "domain.schema.json"
        catalog_path = schema_path.with_name("domain.json")
        state_machine_path = schema_path.with_name("task-state-machine.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        state_machine = json.loads(state_machine_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        for name in [
            "Task",
            "Handoff",
            "HandoffReceipt",
            "Artifact",
            "Run",
            "Review",
            "Gate",
            "RiskRegistryEntry",
            "RiskDecisionRequest",
            "Evidence",
            "Event",
            "EventOutbox",
            "Device",
            "DevicePairing",
            "DeviceProjectGrant",
            "AgentConnection",
            "GatewayEnvelope",
            "AgentHeartbeat",
            "GatewayEventAck",
            "GatewayReplayRequest",
            "IdempotencyEnvelope",
            "TaskTransition",
        ]:
            self.assertIn(name, schema["$defs"])
        self.assertEqual(catalog["schema_ref"], "domain.schema.json")
        self.assertIn("APPROVED", catalog["properties"]["review_verdicts"]["items"]["enum"])
        self.assertIn("WAITING_REVIEW", {item["from"] for item in state_machine["transitions"]})
        self.assertEqual(state_machine["schema_version"], "1.0")
        self.assertIn("device_statuses", catalog["properties"])
        self.assertIn("INTERACTIVE_DESKTOP", catalog["properties"]["execution_modes"]["items"]["enum"])

    def test_direct_approval_and_illegal_transition_are_rejected(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="门禁测试任务"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        with self.assertRaises(PermissionError):
            self.store.update_task(task.id, TaskStatus.APPROVED, None, None)
        with self.assertRaises(ValueError):
            self.store.update_task(task.id, TaskStatus.CLAIMED, None, None)

    def test_review_gate_and_event_roll_back_together(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="事务审核任务"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        before_events = len(self.store.list_events(self.project.id))
        with patch.object(self.store, "_insert_event", side_effect=RuntimeError("event_insert_failed")):
            with self.assertRaisesRegex(RuntimeError, "event_insert_failed"):
                self.store.create_review(
                    self.project.id,
                    ReviewCreate(
                        target_type="task",
                        target_id=task.id,
                        verdict="APPROVED",
                        summary="事务审核",
                        reviewer="member-001",
                        reviewer_kind=ReviewerKind.MEMBER,
                    ),
                )
        self.assertEqual(self.store.get_task(task.id).status, TaskStatus.WAITING_REVIEW)
        self.assertFalse(any(gate.target_id == task.id for gate in self.store.list_gates(self.project.id)))
        self.assertFalse(any(review.target_id == task.id for review in self.store.list_reviews(self.project.id)))
        self.assertEqual(len(self.store.list_events(self.project.id)), before_events)

    def test_invalid_review_does_not_leave_gate_row(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="非法审核任务"))
        with self.assertRaisesRegex(ValueError, "task_not_waiting_for_review"):
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="task",
                    target_id=task.id,
                    verdict="APPROVED",
                    summary="不应通过",
                    reviewer="member-001",
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
        self.assertFalse(any(gate.target_id == task.id for gate in self.store.list_gates(self.project.id)))

    def test_handoff_and_review_idempotency_replay_without_duplicate_side_effects(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="幂等交接审核任务"))
        handoff_data = HandoffCreate(
            task_id=task.id,
            objective="验证交接重试",
            idempotency_key="handoff-idempotency-001",
        )
        first_handoff = self.store.create_handoff(self.project.id, handoff_data, sender_agent_id="agent-a")
        repeated_handoff = self.store.create_handoff(self.project.id, handoff_data, sender_agent_id="agent-a")
        self.assertEqual(first_handoff.id, repeated_handoff.id)
        with self.assertRaisesRegex(ValueError, "idempotency_key_reused_for_different_request"):
            self.store.create_handoff(
                self.project.id,
                handoff_data.model_copy(update={"objective": "篡改后的交接内容"}),
                sender_agent_id="agent-a",
            )

        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        review_data = ReviewCreate(
            target_type="task",
            target_id=task.id,
            verdict="NEEDS_REVISION",
            summary="需要补充证据",
            reviewer="agent-reviewer",
            reviewer_kind=ReviewerKind.AGENT,
            idempotency_key="review-idempotency-001",
        )
        first_review = self.store.create_review(self.project.id, review_data)
        repeated_review = self.store.create_review(self.project.id, review_data)
        self.assertEqual(first_review.id, repeated_review.id)
        self.assertEqual(
            len([item for item in self.store.list_reviews(self.project.id) if item.target_id == task.id]),
            1,
        )
        with self.assertRaisesRegex(ValueError, "idempotency_key_reused_for_different_request"):
            self.store.create_review(
                self.project.id,
                review_data.model_copy(update={"summary": "篡改后的审核内容"}),
            )

    def test_rejected_handoff_requires_revision_before_it_can_be_accepted(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="需要返工的交接"))
        original = self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=task.id,
                receiver={"type": "agent", "id": "agent-b"},
                objective="交给下游复核",
            ),
            sender_agent_id="agent-a",
        )
        rejected = self.store.reject_handoff(
            original.id,
            "agent-b",
            "缺少模型边界说明",
            [{"severity": "major", "code": "missing-boundary", "text": "缺少模型边界说明"}],
        )
        self.assertEqual(rejected.receipt_status, "REJECTED")
        self.assertEqual(rejected.status, "NEEDS_REVISION")
        self.assertEqual(rejected.decision_reason, "缺少模型边界说明")
        with self.assertRaisesRegex(ValueError, "handoff_rejected_requires_revision"):
            self.store.accept_handoff(original.id, "agent-b")

        revision = self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=task.id,
                receiver={"type": "agent", "id": "agent-b"},
                objective="补充边界后的交接",
                revision_of_handoff_id=original.id,
            ),
            sender_agent_id="agent-a",
        )
        self.assertEqual(revision.revision_number, 2)
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="handoff",
                target_id=revision.id,
                verdict="APPROVED",
                summary="人工确认返工后的交接",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.assertEqual(self.store.accept_handoff(revision.id, "agent-b").receipt_status, "ACCEPTED")

    def test_aggregate_handoff_requires_accepted_and_gated_inputs(self) -> None:
        input_tasks = [self.store.create_task(self.project.id, TaskCreate(title=f"汇总输入 {index}")) for index in (1, 2)]
        input_handoffs = []
        for task in input_tasks:
            handoff = self.store.create_handoff(
                self.project.id,
                HandoffCreate(task_id=task.id, receiver={"type": "agent", "id": "agent-d"}, objective="汇总前置结果"),
                sender_agent_id="agent-a",
            )
            self.store.accept_handoff(handoff.id, "agent-d")
            input_handoffs.append(handoff)
        aggregate_task = self.store.create_task(
            self.project.id,
            TaskCreate(title="汇总交接任务", input_handoff_ids=[item.id for item in input_handoffs]),
        )
        with self.assertRaisesRegex(ValueError, "aggregate_input_handoff_gate_not_passed"):
            self.store.create_handoff(
                self.project.id,
                HandoffCreate(
                    task_id=aggregate_task.id,
                    receiver={"type": "agent", "id": "agent-d"},
                    handoff_type="AGGREGATE",
                    input_handoff_ids=[item.id for item in input_handoffs],
                    objective="汇总两个已接收结果",
                ),
                sender_agent_id="agent-d",
            )
        for handoff in input_handoffs:
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="handoff",
                    target_id=handoff.id,
                    verdict="APPROVED",
                    summary="前置交接已核验",
                    reviewer="member-001",
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
        aggregate = self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=aggregate_task.id,
                receiver={"type": "agent", "id": "agent-d"},
                handoff_type="AGGREGATE",
                input_handoff_ids=[item.id for item in input_handoffs],
                objective="汇总两个已接收结果",
            ),
            sender_agent_id="agent-d",
        )
        self.assertEqual(aggregate.handoff_type, "AGGREGATE")
        self.assertEqual(aggregate.input_handoff_ids, [item.id for item in input_handoffs])

    def test_risk_rules_and_review_center_keep_gate_lineage(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="风险规则任务"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        evidence = self.store.create_evidence(
            self.project.id,
            EvidenceCreate(
                claim="运行日志支撑边界检查",
                evidence_type="event",
                source_ref="event://run-001",
            ),
        )
        with self.assertRaisesRegex(ValueError, "review_blocked_by_findings"):
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="task",
                    target_id=task.id,
                    verdict="APPROVED",
                    summary="带未解决重大风险，不应通过",
                    findings=[{"severity": "major", "code": "boundary", "text": "信息边界未核对"}],
                    evidence_ids=[evidence.id],
                    reviewer="member-001",
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task.id,
                verdict="NEEDS_REVISION",
                summary="先补充边界审计",
                findings=[{"severity": "major", "code": "boundary", "text": "信息边界未核对"}],
                evidence_ids=[evidence.id],
                reviewer="agent-a",
                reviewer_kind=ReviewerKind.AGENT,
            ),
        )
        gate = next(item for item in self.store.list_gates(self.project.id) if item.target_id == task.id)
        self.assertEqual(gate.status, "FAILED")
        self.assertEqual(gate.review_ids, [review.id])
        self.assertEqual(gate.evidence_ids, [evidence.id])
        center = ReviewCenter(
            gates=self.store.list_gates(self.project.id),
            reviews=self.store.list_reviews(self.project.id),
            evidence=self.store.list_evidence(self.project.id),
            risks=self.store.list_risks(self.project.id),
        )
        self.assertEqual(len(center.risks), 1)
        self.assertEqual(center.risks[0].severity, "major")

    def test_fanout_receipts_are_independent_and_downstream_waits_for_all(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Fanout 主任务"))
        handoff = self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=task.id,
                receiver={"type": "agents", "ids": ["agent-b", "agent-c"]},
                handoff_type="FANOUT",
                requires_human_approval=False,
                objective="分别交给两个并行 Agent",
            ),
            sender_agent_id="agent-a",
        )
        self.assertEqual([(item.receiver_id, item.status) for item in handoff.receipts], [("agent-b", "PENDING"), ("agent-c", "PENDING")])
        self.assertEqual(self.store.accept_handoff(handoff.id, "agent-b").receipt_status, "PENDING")
        self.assertEqual(self.store.get_handoff(handoff.id).receipts[1].status, "PENDING")

        downstream = self.store.create_task(
            self.project.id,
            TaskCreate(title="等待完整 Fanout", input_handoff_ids=[handoff.id]),
        )
        with self.assertRaisesRegex(ValueError, "task_dependencies_or_inputs_not_approved"):
            self.store.claim_task(
                downstream.id,
                TaskClaimRequest(agent_id="agent-d", lease_seconds=120, idempotency_key="fanout-receipt-wait"),
            )

        completed = self.store.accept_handoff(handoff.id, "agent-c")
        self.assertEqual(completed.receipt_status, "ACCEPTED")
        self.assertTrue(all(item.status == "ACCEPTED" for item in completed.receipts))
        claimed, _ = self.store.claim_task(
            downstream.id,
            TaskClaimRequest(agent_id="agent-d", lease_seconds=120, idempotency_key="fanout-receipt-ready"),
        )
        self.assertEqual(claimed.status, TaskStatus.CLAIMED)

    def test_risk_requires_independent_closure_before_approval(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="风险关闭任务"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        evidence = self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="已补充边界审计", evidence_type="event", source_ref="event://boundary-fixed"),
        )
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task.id,
                verdict="NEEDS_REVISION",
                summary="存在待关闭风险",
                findings=[{"severity": "major", "code": "boundary", "text": "需要补充边界审计"}],
                reviewer="agent-a",
                reviewer_kind=ReviewerKind.AGENT,
            ),
        )
        risk = next(item for item in self.store.list_risks(self.project.id) if item.review_id == review.id)
        with self.assertRaisesRegex(ValueError, "review_blocked_by_open_risks"):
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="task",
                    target_id=task.id,
                    verdict="APPROVED",
                    summary="不能只改审核结论来清除风险",
                    reviewer="member-001",
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
        assigned = self.store.update_risk(
            self.project.id,
            risk.id,
            RiskDecisionRequest(action="ASSIGN", owner="member-001", idempotency_key="risk-assign-001"),
            actor="member-001",
        )
        self.assertEqual(assigned.owner, "member-001")
        resolved = self.store.update_risk(
            self.project.id,
            risk.id,
            RiskDecisionRequest(action="RESOLVE", reason="边界审计已补齐", evidence_ids=[evidence.id], idempotency_key="risk-resolve-001"),
            actor="member-001",
        )
        self.assertTrue(resolved.resolved)
        self.assertEqual(resolved.closure_evidence_ids, [evidence.id])
        approved = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task.id,
                verdict="APPROVED",
                summary="风险关闭后重新审核通过",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.assertEqual(approved.verdict, "APPROVED")

    def test_gate_is_invalidated_when_evidence_source_changes(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Gate 快照任务"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="snapshot-input.txt", artifact_type="problem_facts", task_id=task.id),
            created_by="member-001",
        )
        self.store.store_artifact_content(artifact.id, b"version-1", "text/plain")
        evidence = self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="快照输入文件", evidence_type="artifact", artifact_id=artifact.id),
        )
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task.id,
                verdict="APPROVED",
                summary="建立 Gate 快照",
                evidence_ids=[evidence.id],
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        passed = next(item for item in self.store.list_gates(self.project.id) if item.target_id == task.id)
        self.assertEqual(passed.status, "PASSED")
        self.store.store_artifact_content(artifact.id, b"version-2", "text/plain")
        invalidated = next(item for item in self.store.list_gates(self.project.id) if item.target_id == task.id)
        self.assertEqual(invalidated.status, "INVALIDATED")
        self.assertEqual(invalidated.invalidation_reason, "input_snapshot_changed")

    def test_gate_invalidation_propagates_to_downstream_tasks(self) -> None:
        source_input = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="propagation-input.txt", artifact_type="problem_facts"),
            created_by="member-001",
        )
        self.store.store_artifact_content(source_input.id, b"before", "text/plain")
        source = self.store.create_task(
            self.project.id,
            TaskCreate(title="传播上游任务", input_artifacts=[str(source_input.id)]),
        )
        downstream = self.store.create_task(
            self.project.id,
            TaskCreate(title="传播下游任务", dependency_task_ids=[source.id]),
        )
        evidence = self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="传播输入文件", evidence_type="artifact", artifact_id=source_input.id),
        )
        for task in (source, downstream):
            self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
            self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="task",
                    target_id=task.id,
                    verdict="APPROVED",
                    summary="传播前人工确认",
                    evidence_ids=[evidence.id] if task.id == source.id else [],
                    reviewer="member-001",
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
        self.store.store_artifact_content(source_input.id, b"after", "text/plain")

        self.store.list_gates(self.project.id)
        self.assertEqual(self.store.get_task(source.id).status, TaskStatus.NEEDS_REVISION)
        self.assertEqual(self.store.get_task(downstream.id).status, TaskStatus.NEEDS_REVISION)
        gates = {gate.target_id: gate for gate in self.store.list_gates(self.project.id)}
        self.assertEqual(gates[source.id].status, "INVALIDATED")
        self.assertEqual(gates[downstream.id].status, "INVALIDATED")
        events = self.store.list_events(self.project.id, limit=1000)
        self.assertTrue(any(event.event_type == "task.upstream_gate_invalidated" and event.payload.get("task_id") == str(downstream.id) for event in events))


if __name__ == "__main__":
    unittest.main()

"""信息边界审计 Gate：把 pack 的 information_boundary 规则接进平台 Gate/Runner 审计。

平台侧已有运行事实：每个 Run 保存 `information_boundary`（Agent Manifest 的审计
结果：allowed / violations）、`data_access_policy`（观察模式、是否允许未来数据）、
`observed_input_files`（系统观察到的输入）。本模块把这些事实与 pack 声明的
边界规则逐条比对，产出可落库的审计结论：

- 边界违规（`allowed=false` 或带 fatal 违规）→ `BLOCKED`
- 观察缺失 / 观察模式不符 / 任务级 allow_future_data 与 deny 规则冲突 → `NEEDS_REVISION`
- 干净 → 不自动批准（正式批准仍需人工 Review）

结论通过 `store.create_review` 落成平台 Review + Gate，目标优先取任务，其次运行
产出成果物，最后项目的结果表槽位。
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from .pack_api import MachineReviewError, PLATFORM_SEVERITY, pack_for_project


class BoundaryGateError(RuntimeError):
    """稳定错误族：缺少可挂载的审计目标。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def evaluate_information_boundary(
    store: Any,
    project_id: UUID,
    *,
    task_id: UUID | None = None,
) -> dict[str, Any]:
    """按 pack 规则审计项目（或指定任务）的运行事实，返回 findings 与判定。"""

    project = store.get_project(project_id)
    pack = pack_for_project(project)
    rules = dict(pack.validation_rules().get("information_boundary", {}) or {})
    boundary = dict(pack.boundary_rules() or {})
    required_mode = str(rules.get("require_observation_mode", "") or "")
    deny_future = bool(rules.get("deny_future_data"))
    require_declared = bool(rules.get("require_declared_inputs"))
    fail_closed = bool(rules.get("fail_closed_on_missing_observation"))

    tasks = {task.id: task for task in store.list_tasks(project_id)}
    runs = store.list_runs(project_id)
    if task_id is not None:
        if task_id not in tasks:
            raise BoundaryGateError("boundary_audit_task_not_found")
        runs = [run for run in runs if run.task_id == task_id]
        if not tasks[task_id].allow_future_data and deny_future:
            pass

    findings: list[dict[str, Any]] = []

    for task in tasks.values() if task_id is None else [tasks[task_id]]:
        if deny_future and bool(getattr(task, "allow_future_data", False)):
            findings.append(
                {
                    "severity": "fatal",
                    "code": "task_future_data_allowed",
                    "message": f"任务「{task.title}」允许未来数据，与 pack 的 deny 规则冲突",
                    "subject": f"task:{task.id}",
                }
            )

    for run in runs:
        info = run.information_boundary or {}
        policy = dict(run.data_access_policy or {})
        subject = f"run:{run.id}"
        if isinstance(info, Mapping) and info.get("allowed") is False:
            findings.append(
                {
                    "severity": "fatal",
                    "code": "information_boundary_violation",
                    "message": f"运行 {run.id} 的信息边界审计未通过",
                    "subject": subject,
                }
            )
        for violation in list(info.get("violations", []) or []) if isinstance(info, Mapping) else []:
            if not isinstance(violation, Mapping):
                continue
            severity = str(violation.get("severity", "major")).lower()
            findings.append(
                {
                    "severity": severity if severity in PLATFORM_SEVERITY else "major",
                    "code": str(violation.get("code", "information_boundary_violation")),
                    "message": str(violation.get("message") or violation.get("code") or "边界违规"),
                    "subject": subject,
                }
            )
        if deny_future and policy.get("allow_future_data") is True:
            findings.append(
                {
                    "severity": "fatal",
                    "code": "future_data_allowed",
                    "message": f"运行 {run.id} 的数据访问策略允许未来数据",
                    "subject": subject,
                }
            )
        if required_mode and str(policy.get("observation_mode", "")) not in {"", required_mode}:
            findings.append(
                {
                    "severity": "major" if fail_closed else "minor",
                    "code": "observation_mode_mismatch",
                    "message": f"运行 {run.id} 的观察模式为 {policy.get('observation_mode') or '未声明'}，要求 {required_mode}",
                    "subject": subject,
                }
            )
        if require_declared and str(policy.get("observation_mode", "")) == "system" and not run.observed_input_files:
            findings.append(
                {
                    "severity": "major" if fail_closed else "minor",
                    "code": "input_observation_not_captured",
                    "message": f"运行 {run.id} 声明系统观察但未记录任何输入文件",
                    "subject": subject,
                }
            )

    if not runs:
        findings.append(
            {
                "severity": "minor",
                "code": "boundary_audit_no_run",
                "message": "尚无运行记录可供边界审计" if task_id is None else "该任务尚无运行记录",
                "subject": f"project:{project_id}",
            }
        )

    severities = {item["severity"] for item in findings}
    verdict = None
    if "fatal" in severities:
        verdict = "BLOCKED"
    elif "major" in severities:
        verdict = "NEEDS_REVISION"

    return {
        "project_id": str(project_id),
        "task_id": str(task_id) if task_id else None,
        "pack_id": pack.pack_id,
        "pack_version": pack.version,
        "rules": {
            "deny_future_data": deny_future,
            "require_declared_inputs": require_declared,
            "require_observation_mode": required_mode or None,
            "fail_closed_on_missing_observation": fail_closed,
        },
        "boundary_rules": boundary,
        "run_count": len(runs),
        "findings": findings,
        "verdict": verdict,
        "allowed": verdict is None,
    }


def _boundary_target(store: Any, project_id: UUID, task_id: UUID | None, runs: list[Any]) -> tuple[str, UUID]:
    if task_id is not None:
        return "task", task_id
    for run in runs:
        if getattr(run, "output_artifact_ids", None):
            return "artifact", run.output_artifact_ids[0]
    for artifact in store.list_artifacts(project_id):
        if str(artifact.artifact_type) == "result_table":
            return "artifact", artifact.id
    raise BoundaryGateError("boundary_gate_target_missing")


def create_information_boundary_gate(
    store: Any,
    project_id: UUID,
    *,
    task_id: UUID | None = None,
    actor: str = "boundary-audit",
    actor_kind: str = "agent",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """审计并落库：结论不干净时写入 Review + Gate（干净则不自动批准）。"""

    report = evaluate_information_boundary(store, project_id, task_id=task_id)
    verdict = report["verdict"]
    if verdict is None:
        return {
            **report,
            "created": False,
            "reason": "boundary_audit_clean_requires_human_approval",
        }

    runs = [run for run in store.list_runs(project_id) if task_id is None or run.task_id == task_id]
    try:
        target_type, target_id = _boundary_target(store, project_id, task_id, runs)
    except BoundaryGateError as error:
        raise MachineReviewError(error.code) from error

    from .contracts import ReviewCreate

    review = store.create_review(
        project_id,
        ReviewCreate(
            target_type=target_type,
            target_id=target_id,
            verdict=verdict,
            summary=f"信息边界审计（{report['pack_id']} v{report['pack_version']}）：{len(report['findings'])} 项发现",
            findings=[
                {
                    "severity": PLATFORM_SEVERITY.get(item["severity"], "minor"),
                    "code": item["code"],
                    "message": f"{item['subject']}: {item['message']}",
                }
                for item in report["findings"]
            ],
            reviewer=actor,
            reviewer_kind=actor_kind,
            idempotency_key=idempotency_key,
        ),
    )
    gates = [gate for gate in store.list_gates(project_id) if str(gate.target_id) == str(target_id)]
    return {
        **report,
        "created": True,
        "review_id": str(review.id),
        "target_type": target_type,
        "target_id": str(target_id),
        "gate_status": str(gates[0].status) if gates else None,
    }


__all__ = [
    "BoundaryGateError",
    "create_information_boundary_gate",
    "evaluate_information_boundary",
]
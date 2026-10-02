"""协作协议冻结（D0，多 Agent 协作开发计划 §8）。

在既有 schema v1、receipt、fencing 和引擎 v1 之上，冻结**剩余**的公共协议：
统一 ``CoordinationEnvelope``（命令/消息/信息请求/回复/阶段报告/交接/ACK/决策）、
``StageReport`` 结构、统一状态迁移表、``OrchestrationDecision``、运行时信息请求与
可行性异议生命周期、ACK 与幂等语义、通信预算。

设计纪律（计划 §5/§6/§9）：
- 纯逻辑、无 IO——存储与路由接线属 D1（扩展现有引擎 v1，不重写）；
- Event 回答"已发生什么"，Command 回答"要求做什么"，Message 回答"交给谁什么"，
  三者不得混用；事件仍走既有事件信封与事件目录，本模块的协作信封**不替代**事件；
- 伪造身份零容忍：sender 由服务端注入为准，payload 里自报权威字段一律拒绝；
- ``UNVERIFIED`` 是一等状态；blocked/needs_decision 必须携带可读 blocker 与建议处置；
- ``required`` 信息请求必须有截止时间；超时/拒绝/过期只进显式状态，不静默丢弃；
- 幂等：同一 idempotency_key 的重复命令/请求必须返回原结果，不重复执行。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

__all__ = [
    "COORDINATION_SCHEMA_VERSION",
    "MESSAGE_KINDS",
    "SENDER_TYPES",
    "SERVER_OWNED_PAYLOAD_KEYS",
    "ACK_OUTCOMES",
    "STAGE_REPORT_STATUSES",
    "RUN_STOP_REASONS",
    "CoordinationError",
    "validate_envelope",
    "validate_stage_report",
    "validate_orchestration_decision",
    "validate_information_request",
    "validate_information_response",
    "validate_feasibility_concern",
    "check_transition",
    "decide_node_effect",
    "check_request_budget",
    "ack_outcome_for",
]

#: 协作信封版本（独立于事件信封的 schema_version；不兼容演进时 +1）。
COORDINATION_SCHEMA_VERSION = 1

MESSAGE_KINDS = (
    "command",
    "message",
    "information_request",
    "information_response",
    "stage_report",
    "handoff",
    "ack",
    "decision",
)

SENDER_TYPES = ("agent", "platform", "human")

#: payload 里的服务端所有权字段（W2.2 可信归属口径的协作协议版）：
#: 客户端在 payload 自报这些键 = 伪造审批人/归属，整封信拒绝。
SERVER_OWNED_PAYLOAD_KEYS = ("actor", "actor_kind", "approved_by", "verdict", "owner_member_id", "run_attribution")

_ACK_OUTCOMES = ("received", "rejected", "unable_to_execute", "expired")

STAGE_REPORT_STATUSES = ("completed", "partial", "blocked", "failed", "needs_review", "needs_decision")

#: 工作流运行的终止原因（D0 冻结；引擎 v1 的 STALLED 状态与 needs_replan 映射到
#: loop_detected / needs_replan，不再另造词表）。
RUN_STOP_REASONS = (
    "completed",
    "budget_exhausted",
    "loop_detected",
    "needs_replan",
    "feasibility_confirmed",
    "cancelled_by_member",
    "upstream_failed",
    "external_wait",
)

_INFO_REQUEST_TYPES = (
    "context",
    "fact",
    "interface",
    "example",
    "decision",
    "capability",
    "artifact_version",
    "clarification",
)
_BLOCKING_LEVELS = ("required", "optional", "conditional")
_RESPONSE_STATUSES = ("answered", "provisional", "unavailable", "rejected", "expired")
_CONCERN_TYPES = (
    "missing_input",
    "invalid_assumption",
    "incompatible_contract",
    "impossible_goal",
    "insufficient_capability",
    "budget_or_time",
    "acceptance_conflict",
    "unsafe_to_continue",
)
_CONCERN_RECOMMENDATIONS = (
    "request_information",
    "revise_task",
    "change_agent",
    "replan",
    "escalate_human",
    "stop",
    "continue_with_assumption",
)
_DECISION_POLICIES = _CONCERN_RECOMMENDATIONS + ("dispatch", "retry", "pause", "resume")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,160}$")


class CoordinationError(ValueError):
    """协作协议稳定错误族：``errors`` 逐条列出（沿用计划 §9 纪律，绝不静默丢弃）。"""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        super().__init__(code if not errors else f"{code}:{' | '.join(errors[:3])}")
        self.code = code
        self.errors = errors or []


def _iso(value: Any) -> bool:
    return isinstance(value, str) and bool(_ISO_RE.match(value))


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# ---- 统一信封（§6.1）---------------------------------------------------------


def validate_envelope(envelope: Mapping) -> list[str]:
    """校验协作信封。返回逐条问题（空列表 = 合法）。伪造身份/自报权威字段整封拒绝。"""

    errors: list[str] = []
    if not isinstance(envelope, Mapping):
        return ["envelope: 必须是 JSON 对象"]
    for field in ("message_id", "message_kind", "schema_version", "project_id", "run_id", "task_id",
                  "sender", "recipient", "correlation_id", "idempotency_key", "seq", "created_at", "payload"):
        if field not in envelope:
            errors.append(f"missing required field: {field}")
    if errors:
        return errors

    if not _is_id(envelope["message_id"]):
        errors.append("message_id: 必须是 4-160 位 [A-Za-z0-9_.:-]")
    if envelope["message_kind"] not in MESSAGE_KINDS:
        errors.append(f"message_kind: 必须是 {MESSAGE_KINDS} 之一（收到 {envelope['message_kind']!r}）")
    if envelope["schema_version"] != COORDINATION_SCHEMA_VERSION:
        errors.append(f"schema_version: 仅支持 {COORDINATION_SCHEMA_VERSION}")
    if not _is_id(envelope["project_id"]) or not _is_id(envelope["run_id"]) or not _is_id(envelope["task_id"]):
        errors.append("project_id/run_id/task_id: 必须是 4-160 位标识符")
    if not _is_id(envelope["correlation_id"]):
        errors.append("correlation_id: 必须是 4-160 位标识符")
    if not _is_id(envelope["idempotency_key"]):
        errors.append("idempotency_key: 必须是 4-160 位标识符（重复投递按它返回原结果）")
    if not _is_positive_int(envelope["seq"]):
        errors.append("seq: 必须是 ≥1 的整数（同一持久化通信流内递增，客户端不编造）")
    if not _iso(envelope["created_at"]):
        errors.append("created_at: 必须是 ISO-8601 时间戳")
    if not isinstance(envelope["payload"], dict):
        errors.append("payload: 必须是对象")
    else:
        owned = sorted(set(envelope["payload"]) & set(SERVER_OWNED_PAYLOAD_KEYS))
        if owned:
            # 伪造身份/审批人/归属——整封拒绝，不信任何自报权威字段（§6.7.3）。
            errors.append(f"payload: 携带服务端所有权字段 {owned}（伪造身份零容忍）")

    for side in ("sender", "recipient"):
        party = envelope.get(side)
        if not isinstance(party, Mapping) or party.get("type") not in SENDER_TYPES or not _is_id(party.get("id")):
            errors.append(f"{side}: 必须是 {{type: {SENDER_TYPES}, id}} 结构")
    sender = envelope.get("sender")
    if isinstance(sender, Mapping) and sender.get("type") == "platform" and str(sender.get("id")) != "platform":
        # 平台身份只有一个：冒充平台 = 伪造身份（审计归属只能来自服务端注入）。
        errors.append("sender: type=platform 时 id 必须是 'platform'")
    if envelope.get("requires_ack") and envelope["message_kind"] not in {"command", "information_request", "handoff"}:
        errors.append("requires_ack: 仅 command/information_request/handoff 可要求 ACK")
    if envelope.get("expires_at") is not None and not _iso(envelope["expires_at"]):
        errors.append("expires_at: 必须是 ISO-8601 时间戳或 null")
    return errors


# ---- StageReport（§6.3）-------------------------------------------------------


def validate_stage_report(report: Mapping) -> list[str]:
    """阶段报告结构校验：报告不是业务状态本身，"我完成了"不是系统验收结论。"""

    errors: list[str] = []
    if not isinstance(report, Mapping):
        return ["stage_report: 必须是 JSON 对象"]
    status = report.get("status")
    if status not in STAGE_REPORT_STATUSES:
        errors.append(f"status: 必须是 {STAGE_REPORT_STATUSES} 之一（收到 {status!r}）")
    if not isinstance(report.get("summary"), str) or not report.get("summary"):
        errors.append("summary: 必填且非空")
    for field in ("completed_items", "incomplete_items", "output_artifacts", "evidence_refs", "blockers"):
        if not isinstance(report.get(field), list):
            errors.append(f"{field}: 必须是数组")
    tests = report.get("tests")
    if not isinstance(tests, dict) or any(not isinstance(tests.get(k), int) or isinstance(tests.get(k), bool) for k in ("passed", "failed", "skipped")):
        errors.append("tests: 必须是 {passed, failed, skipped} 整数计数")
    if not isinstance(report.get("can_continue_safely"), bool):
        errors.append("can_continue_safely: 必填布尔（Agent 对安全继续的判断，不是编排结论）")

    artifacts = report.get("output_artifacts") or []
    for item in artifacts:
        if not isinstance(item, Mapping) or not _is_id(str(item.get("artifact_id") or "")) or not _is_positive_int(item.get("version")):
            errors.append(f"output_artifacts: 每条必须是 {{artifact_id, version≥1, role?}}（收到 {item!r}）")

    incomplete = report.get("incomplete_items") or []
    blockers = report.get("blockers") or []
    if status == "completed" and incomplete:
        errors.append("status=completed 时 incomplete_items 必须为空（有未完成项就是 partial）")
    if status in {"blocked", "needs_decision"}:
        if not blockers:
            errors.append(f"status={status} 必须携带可读 blockers（§6.3 接收规则 6）")
        if status == "needs_decision" and not report.get("requested_decision"):
            errors.append("status=needs_decision 必须给出 requested_decision（请编排器决策什么）")
    if report.get("continued_under_assumption") and not (report.get("assumptions") or []):
        # 带假设继续必须记录假设（§6.5.2 规则 4）——假设不能替代 Gate 证据。
        errors.append("continued_under_assumption=true 时必须给出 assumptions（假设清单）")
    return errors


# ---- 统一状态迁移表（§4.1 P0：Task/Run/Handoff/Review/信息请求）-----------------

TRANSITIONS: dict[str, frozenset[tuple[str, str]]] = {
    # 任务：与引擎 v1 白名单同源（test_lease_fencing / multi_agent_assertions 已锁）
    "task": frozenset(
        {
            ("DRAFT", "READY"), ("READY", "CLAIMED"), ("CLAIMED", "RUNNING"),
            ("CLAIMED", "WAITING_REVIEW"), ("CLAIMED", "FAILED"), ("CLAIMED", "CANCELLED"),
            ("RUNNING", "WAITING_REVIEW"), ("RUNNING", "FAILED"), ("RUNNING", "CANCELLED"),
            ("RUNNING", "WAITING"), ("WAITING", "RUNNING"),
            ("WAITING_REVIEW", "APPROVED"), ("WAITING_REVIEW", "NEEDS_REVISION"),
            ("NEEDS_REVISION", "READY"), ("FAILED", "READY"), ("READY", "CANCELLED"),
            ("BLOCKED", "READY"),
        }
    ),
    # 工作流运行：引擎 v1 现状 + 计划阶段 8/9
    "run": frozenset(
        {
            ("RUNNING", "COMPLETED"), ("RUNNING", "STALLED"), ("RUNNING", "FAILED"), ("RUNNING", "CANCELLED"),
            ("STALLED", "RUNNING"), ("STALLED", "CANCELLED"), ("FAILED", "RUNNING"),
        }
    ),
    # 交接（§阶段 7）
    "handoff": frozenset(
        {
            ("PREPARED", "SENT"), ("SENT", "RECEIVED"), ("RECEIVED", "ACCEPTED"),
            ("SENT", "NEEDS_REVISION"), ("RECEIVED", "NEEDS_REVISION"), ("SENT", "REJECTED"),
        }
    ),
    # 运行时信息请求（§6.5 生命周期）
    "information_request": frozenset(
        {
            ("OPEN", "ACKNOWLEDGED"), ("OPEN", "ROUTED"), ("ROUTED", "ACKNOWLEDGED"),
            ("ROUTED", "REDIRECTED"), ("REDIRECTED", "ROUTED"), ("ACKNOWLEDGED", "ANSWERED"),
            ("ACKNOWLEDGED", "PROVISIONAL"), ("ACKNOWLEDGED", "UNAVAILABLE"), ("ACKNOWLEDGED", "REJECTED"),
            ("ANSWERED", "CONSUMER_ACKNOWLEDGED"), ("PROVISIONAL", "CONSUMER_ACKNOWLEDGED"),
            ("ANSWERED", "DISPUTED"), ("PROVISIONAL", "DISPUTED"), ("DISPUTED", "NEEDS_DECISION"),
            ("OPEN", "EXPIRED"), ("ROUTED", "EXPIRED"), ("ACKNOWLEDGED", "EXPIRED"),
        }
    ),
}


class IllegalTransition(CoordinationError):
    def __init__(self, kind: str, prev: str, nxt: str) -> None:
        super().__init__("illegal_transition", [f"{kind}: {prev!r} → {nxt!r} 不在合法迁移表"])
        self.kind, self.prev, self.nxt = kind, prev, nxt


def check_transition(kind: str, prev: str, nxt: str) -> None:
    """非法迁移直接抛错（fail-closed）；合法迁移返回 None。条件更新的守卫在 D1 接线。"""

    table = TRANSITIONS.get(kind)
    if table is None:
        raise CoordinationError("unknown_transition_kind", [f"kind: {kind!r} 未定义迁移表"])
    # 各类状态词表统一大写（任务/运行/交接/信息请求全是）
    prev, nxt = str(prev).upper(), str(nxt).upper()
    if (prev, nxt) not in table:
        raise IllegalTransition(kind, prev, nxt)


# ---- 运行时信息请求与回复（§6.5）-----------------------------------------------


def validate_information_request(request: Mapping) -> list[str]:
    errors: list[str] = []
    if not isinstance(request, Mapping):
        return ["information_request: 必须是 JSON 对象"]
    if request.get("request_type") not in _INFO_REQUEST_TYPES:
        errors.append(f"request_type: 必须是 {_INFO_REQUEST_TYPES} 之一")
    if not isinstance(request.get("question"), str) or not request["question"]:
        errors.append("question: 必填且非空")
    blocking = request.get("blocking")
    if blocking not in _BLOCKING_LEVELS:
        errors.append(f"blocking: 必须是 {_BLOCKING_LEVELS} 之一")
    if not isinstance(request.get("reason"), str) or not request["reason"]:
        errors.append("reason: 必填（没有该信息时无法安全完成哪一步）")
    deadline = request.get("response_deadline")
    # 验收口径：无截止时间的请求被拒绝——required 必须有 deadline；optional 允许 null。
    if blocking == "required" and not _iso(deadline):
        errors.append("response_deadline: blocking=required 必须给出截止时间（超时走转问/升级/阻塞，不静默）")
    if deadline is not None and not _iso(deadline):
        errors.append("response_deadline: 必须是 ISO-8601 或 null")
    hint = request.get("routing_hint")
    if hint is not None and not isinstance(hint, dict):
        errors.append("routing_hint: 必须是对象（只是建议，不能绕过权限与 Orchestrator）")
    return errors


def validate_information_response(response: Mapping) -> list[str]:
    errors: list[str] = []
    if not isinstance(response, Mapping):
        return ["information_response: 必须是 JSON 对象"]
    if response.get("status") not in _RESPONSE_STATUSES:
        errors.append(f"status: 必须是 {_RESPONSE_STATUSES} 之一")
    status = response.get("status")
    if status in {"answered", "provisional"} and not isinstance(response.get("answer_summary"), str) or (
        status in {"answered", "provisional"} and not response.get("answer_summary")
    ):
        errors.append("answer_summary: answered/provisional 必须给出可读摘要")
    for field in ("facts", "artifact_refs", "evidence_refs", "assumptions"):
        if not isinstance(response.get(field), list):
            errors.append(f"{field}: 必须是数组")
    if status == "provisional" and not (response.get("assumptions") or []):
        # 暂定意见必须保留风险——消费方据此决定能否继续，不能当已确认事实。
        errors.append("status=provisional 必须给出 assumptions（暂定假设清单）")
    return errors


# ---- 可行性异议（§6.5.2）-------------------------------------------------------


def validate_feasibility_concern(concern: Mapping) -> list[str]:
    errors: list[str] = []
    if not isinstance(concern, Mapping):
        return ["feasibility_concern: 必须是 JSON 对象"]
    if concern.get("concern_type") not in _CONCERN_TYPES:
        errors.append(f"concern_type: 必须是 {_CONCERN_TYPES} 之一")
    if not isinstance(concern.get("claim"), str) or not concern["claim"]:
        errors.append("claim: 必填（当前任务无法按原口径安全完成）")
    if not isinstance(concern.get("basis"), list) or not concern["basis"]:
        # 可行性异议能保留证据进入 Orchestrator 决策（D0 验收口径）——无证据不接受。
        errors.append("basis: 必须是非空证据引用数组（event/artifact/receipt/test_run）")
    impact = concern.get("impact")
    if not isinstance(impact, dict) or "current_node" not in impact:
        errors.append("impact: 必须是对象且含 current_node")
    if concern.get("recommendation") not in _CONCERN_RECOMMENDATIONS:
        errors.append(f"recommendation: 必须是 {_CONCERN_RECOMMENDATIONS} 之一")
    if not isinstance(concern.get("can_continue_safely"), bool):
        errors.append("can_continue_safely: 必填布尔")
    if concern.get("can_continue_safely") is False and concern.get("recommendation") in (None, "continue_with_assumption"):
        # 不能安全继续却又建议带假设继续——自相矛盾，拒绝。
        errors.append("can_continue_safely=false 时 recommendation 不得是 continue_with_assumption")
    if concern.get("continued_under_assumption") and not (concern.get("unverified_assumptions") or []):
        errors.append("continued_under_assumption=true 必须列出 unverified_assumptions")
    return errors


# ---- OrchestrationDecision（§阶段 6）-------------------------------------------


def validate_orchestration_decision(decision: Mapping) -> list[str]:
    """编排决策：唯一 Orchestrator 的可审计记录——为什么选某 Agent、为什么重试或阻塞。"""

    errors: list[str] = []
    if not isinstance(decision, Mapping):
        return ["orchestration_decision: 必须是 JSON 对象"]
    if not _is_id(decision.get("decision_id") or ""):
        errors.append("decision_id: 必填标识符")
    policy = decision.get("policy")
    if policy not in _DECISION_POLICIES:
        errors.append(f"policy: 必须是 {_DECISION_POLICIES} 之一（收到 {policy!r}）")
    if not isinstance(decision.get("basis"), list) or not decision["basis"]:
        errors.append("basis: 必须引用证据/事件（决策可回放的基础）")
    if not _iso(decision.get("created_at")):
        errors.append("created_at: 必须是 ISO-8601")
    expected = decision.get("expected_events")
    if not isinstance(expected, list) or not expected:
        errors.append("expected_events: 必须非空（决策必须声明它预期产生的事实事件）")
    stop_reason = decision.get("stop_reason")
    if stop_reason is not None and stop_reason not in RUN_STOP_REASONS:
        errors.append(f"stop_reason: 必须是 {RUN_STOP_REASONS} 之一")
    if policy == "stop" and stop_reason is None:
        errors.append("policy=stop 必须给出 stop_reason（诚实终止不是异常吞掉）")
    return errors


# ---- ACK 与通信预算（§6.5.3 / §6.7）--------------------------------------------


def ack_outcome_for(*, expires_at: str | None, now_iso: str, duplicate: bool, previous_outcome: str | None) -> tuple[str, str]:
    """ACK 结算：过期 → expired；重复 → **原结果**（幂等，不重新执行）；否则 received。

    返回 (outcome, reason)。重复且无原结果时返回 received 并提示补记——不静默丢。
    """

    if duplicate and previous_outcome:
        return previous_outcome, "duplicate: 返回原结果（幂等）"
    if duplicate:
        return "received", "duplicate: 无原结果可回放，按首次接收记账（请补记原处理结果）"
    if expires_at and _iso(expires_at) and expires_at < now_iso:
        return "expired", "ack 超过 expires_at（过期不是静默丢弃）"
    return "received", ""


def check_request_budget(
    *, run_active_requests: int, run_total_requests: int, redirects_for_request: int,
    limits: Mapping[str, int] | None = None,
) -> tuple[bool, str]:
    """通信预算（§6.5.3 防循环）：超限必须拒绝或升级，不允许无限互问。"""

    limits = limits or {}
    max_active = int(limits.get("max_concurrent_requests_per_run", 5))
    max_total = int(limits.get("max_requests_per_run", 40))
    max_redirects = int(limits.get("max_redirects_per_request", 3))
    if run_active_requests >= max_active:
        return False, f"运行内并发信息请求已达上限 {max_active}"
    if run_total_requests >= max_total:
        return False, f"运行内信息请求总量已达上限 {max_total}"
    if redirects_for_request >= max_redirects:
        return False, f"单请求转问已达上限 {max_redirects}（应升级人工或阻塞）"
    return True, ""


def decide_node_effect(blocking: str, response_status: str | None) -> tuple[str, str]:
    """required 信息请求如何影响节点（D0 验收：required 请求能使节点进入等待）。

    返回 (节点效果, 原因)。效果 ∈ RUNNING / WAITING / BLOCKED / NEEDS_DECISION。
    """

    if blocking == "required":
        if response_status is None:
            return "WAITING", "information_requested（等待可信回复）"
        if response_status == "answered":
            return "RUNNING", "回复可消费（是否批准仍由 Review/Gate 判定）"
        if response_status == "provisional":
            return "RUNNING", "带暂定假设继续（假设已记录，不作为 HOLDS 证据）"
        if response_status in {"unavailable", "rejected", "expired"}:
            return "BLOCKED", f"信息请求 {response_status}（转问或升级人工）"
    if blocking == "optional" and response_status in {"unavailable", "rejected", "expired"}:
        return "RUNNING", "optional 未获回复：记录假设与风险后继续"
    return "RUNNING", "无阻塞效果"

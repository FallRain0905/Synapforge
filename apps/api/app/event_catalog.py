"""项目事件目录（实施计划 W2.1，工作包 C）。

平台多 Agent 协作的项目级事件只有**一套**目录（规划 §7 红线：不新增第二套
事件协议）。事件名必须先注册在本目录里才允许发出；目录本身按"只增不改"
演进——新增事件、新增可选字段都可以，改名/删名/改语义不行，消费方必须
忽略自己不认识的事件与字段。四条契约规则详见 ``docs/AGENT_EVENT_CONTRACT.md``。

设计借鉴：deer-flow ``runtime/events/catalog.py``（固定分类 + additive 演进）
与 autogen ``teams/_group_chat/_events.py``（错误/终止也是一等事件）。

本模块纯逻辑、无 IO：A 在写 outbox/events 前调用 ``require_event_name``
与 ``validate_envelope``；B 的前端按契约消费。错误是事件不是异常——
执行失败、门禁拒绝、审核退回都走 ``project.*.failed/blocked/rejected``，
HTTP 错误码只属于传输层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Mapping

__all__ = [
    "CATALOG_VERSION",
    "EVENT_FAMILIES",
    "EventSpec",
    "UnknownEventError",
    "events_in_family",
    "families",
    "is_registered",
    "event_version",
    "require_event_name",
    "validate_envelope",
]


#: 目录整体版本。每次新增事件时 +1；消费方不得假设两个版本之间没有新事件。
CATALOG_VERSION = 3

#: 事件族（事件名第二段）。新族属于演进：information_request / feasibility_concern
#: 两个运行时协作族由多 Agent 协作计划 §6.5.3 评审加入（D0 协议冻结配套，v2）。
EVENT_FAMILIES = ("task", "run", "artifact", "handoff", "review", "gate", "agent",
                  "information_request", "feasibility_concern", "stage_report")

_EVENT_NAME_RE = re.compile(r"^project\.(?P<family>[a-z_]+)\.(?P<action>[a-z_]+)$")  # v2：族名允许下划线（information_request / feasibility_concern）


class UnknownEventError(ValueError):
    """事件名未注册——发出侧必须在编译期/写库前拦截，而不是发出去让消费方猜。"""


@dataclass(frozen=True)
class EventSpec:
    name: str
    version: int  # 单事件 schema 版本，从 1 起；字段只增不改时 +0，语义演进才 +1
    family: str
    description: str


def _e(name: str, description: str) -> EventSpec:
    return EventSpec(name=name, version=1, family=name.split(".")[1], description=description)


#: 首批注册事件（覆盖阶段 2 团队视图与阶段 3 推进器的最小需求；
#: 缺的事件走 additive 流程补注册，绝不现场发明事件名）。
EVENTS: dict[str, EventSpec] = {
    spec.name: spec
    for spec in (
        # ---- task ----
        _e("project.task.created", "任务已创建（含派发目标与依赖）"),
        _e("project.task.claimed", "执行体领取任务（含租约）"),
        _e("project.task.status_changed", "任务状态迁移（from/to）"),
        _e("project.task.failed", "任务失败（含 stop_reason 与重试计划）"),
        _e("project.task.retried", "任务重试（含尝试序号）"),
        # ---- run ----
        _e("project.run.started", "Run 开始"),
        _e("project.run.finished", "Run 结束（含 stop_reason 与用量）"),
        _e("project.run.failed", "Run 异常失败（错误是一等事件，不是传输层异常）"),
        # ---- artifact ----
        _e("project.artifact.uploaded", "成果物上传（含 receipt 哈希，见 RECEIPT_FORMAT.md）"),
        _e("project.artifact.version_created", "退回修订产生新版本"),
        _e("project.artifact.approved", "成果物人工批准"),
        _e("project.artifact.rejected", "成果物被退回（含原因）"),
        # ---- handoff ----
        _e("project.handoff.sent", "结构化交接发出"),
        _e("project.handoff.accepted", "下游接收交接"),
        _e("project.handoff.rejected", "交接被拒（含原因）"),
        # ---- review ----
        _e("project.review.requested", "请求复核"),
        _e("project.review.concluded", "复核给出结论（通过/退回/风险）"),
        # ---- gate ----
        _e("project.gate.evaluated", "门禁评估完成（含逐条验收叶子结果）"),
        _e("project.gate.passed", "门禁通过"),
        _e("project.gate.blocked", "门禁阻塞（含 UNVERIFIED 条目清单）"),
        _e("project.gate.escalated", "门禁升级人工介入"),
        # ---- stage_report ----
        _e("project.stage_report.submitted", "阶段报告提交（进入接收方审核，不是批准结论）"),
        # ---- agent ----
        _e("project.information_request.created", "运行时信息请求创建（含 blocking 级别与截止时间）"),
        _e("project.information_request.acked", "信息请求被接收方确认（received/rejected/unable）"),
        _e("project.information_request.answered", "信息请求获得回复（answered/provisional）"),
        _e("project.information_request.redirected", "信息请求转问（含转问序号）"),
        _e("project.information_request.expired", "信息请求超时（转问/升级/阻塞由编排决策）"),
        _e("project.information_request.consumed", "请求方确认消费回复并继续/阻塞当前节点"),
        _e("project.feasibility_concern.raised", "Agent 提出可行性异议（含证据与建议处置）"),
        _e("project.feasibility_concern.decided", "Orchestrator 对异议做出决策（含依据）"),
        _e("project.agent.joined", "Agent 接入项目"),
        _e("project.agent.left", "Agent 断开/退出"),
        _e("project.agent.lease_lost", "租约丢失/被接管（fencing 生效）"),
    )
}


def is_registered(name: str) -> bool:
    return name in EVENTS


def require_event_name(name: str) -> EventSpec:
    """未注册事件名直接拒绝（发出侧拦截），并给出最接近的合法名提示。"""
    spec = EVENTS.get(name)
    if spec is not None:
        return spec
    m = _EVENT_NAME_RE.match(str(name or ""))
    if m is None:
        raise UnknownEventError(f"event name {name!r} does not match 'project.<family>.<action>'")
    if m.group("family") not in EVENT_FAMILIES:
        raise UnknownEventError(
            f"unknown family {m.group('family')!r}; registered families: {', '.join(EVENT_FAMILIES)}"
        )
    raise UnknownEventError(f"event {name!r} is not registered; extend event_catalog via the additive process")


def event_version(name: str) -> int:
    return require_event_name(name).version


def families() -> tuple[str, ...]:
    return EVENT_FAMILIES


def events_in_family(family: str) -> List[EventSpec]:
    if family not in EVENT_FAMILIES:
        raise UnknownEventError(f"unknown family {family!r}")
    return [spec for spec in EVENTS.values() if spec.family == family]


# --------------------------------------------------------------------------
# 信封校验（A 写 outbox/events 前调用；B 按同一形状消费）
# --------------------------------------------------------------------------

#: 信封必填字段。``project_id`` / ``actor`` 推荐携带但由平台侧决定是否强制。
REQUIRED_ENVELOPE_FIELDS = ("event", "seq", "schema_version", "occurred_at", "payload")
RECOMMENDED_ENVELOPE_FIELDS = ("project_id", "organization_id", "actor")


def validate_envelope(envelope: Mapping) -> List[str]:
    """校验事件信封，返回问题清单（空列表 = 合法）。

    校验的是**结构与注册**，不校验业务语义（那是各服务的职责）。
    未注册事件名视为问题——发出侧必须在写库前修好。
    """
    problems: List[str] = []
    for field_name in REQUIRED_ENVELOPE_FIELDS:
        if field_name not in envelope:
            problems.append(f"missing required field: {field_name}")
    if problems:
        return problems

    try:
        require_event_name(envelope["event"])
    except UnknownEventError as exc:
        problems.append(str(exc))

    seq = envelope["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        problems.append("seq must be a positive integer (strictly increasing within its stream)")

    schema_version = envelope["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        problems.append("schema_version must be a positive integer")

    if not isinstance(envelope["payload"], dict):
        problems.append("payload must be an object")
    if not isinstance(envelope["occurred_at"], str) or not envelope["occurred_at"]:
        problems.append("occurred_at must be an ISO-8601 timestamp string")

    return problems

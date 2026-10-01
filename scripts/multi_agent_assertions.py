"""多智能体 e2e 断言库（W2.6 断言件，工作包 C）。

给 `scripts/verify_multi_agent_e2e.py`（A 的主脚本）用的可复用断言：
主脚本负责发起 HTTP 调用并拿到 JSON，本库只对 **payload 本身**做结构与
契约断言——纯函数、无 IO、无 HTTP，返回 ``(ok: bool, detail: str)``，
与主脚本的 ``check(label, condition, extra)`` 收集器直接对接::

    ok, extra = multi_agent_assertions.check_team_view(team)
    check("团队视图结构与计数一致", ok, extra)

覆盖四组契约：
1. 任务状态机（含规划 §7 红线：任务成功 ≠ 成果批准，两条审批各自独立）；
2. receipt 形状（RECEIPT_FORMAT §2/§3：16 位小写 hex、字段上限、软字段）；
3. 团队视图 / 生产路径聚合（project_team.py 的真实 payload 形状，2026-09-30 对齐；
   未知字段不臆测、未知状态 fail-closed）；
4. project.* 事件流（AGENT_EVENT_CONTRACT：目录形状名必须在目录、seq 严格递增、
   老轨名豁免但逐个点名——只增不改）。

本文件由 C 拥有；主脚本（A）只 import，不改本文件。
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

__all__ = [
    "KNOWN_TASK_STATUSES",
    "TASK_TRANSITIONS",
    "GATE_VERDICTS",
    "check_task_transition",
    "check_task_lifecycle",
    "check_receipt_shape",
    "check_team_view",
    "check_production_path",
    "check_artifact_version_bump",
    "check_artifact_feeds_task",
    "check_event_stream",
    "check_gate_evaluated_payload",
    "check_gate_blocked_payload",
    "check_stall_series",
    "check_ledger_snapshot",
]


# ==========================================================================
# 1. 任务状态机
# ==========================================================================

KNOWN_TASK_STATUSES = frozenset(
    {"READY", "CLAIMED", "RUNNING", "WAITING_REVIEW", "APPROVED", "FAILED", "NEEDS_REVISION", "CANCELLED"}
)

#: 剧本已用到的合法迁移（fail-closed：新迁移出现时这里会挡下来，由 A 有意识地扩充）。
TASK_TRANSITIONS = frozenset(
    {
        ("READY", "CLAIMED"),
        ("CLAIMED", "RUNNING"),
        ("CLAIMED", "WAITING_REVIEW"),  # 领取后直接交付（跳过显式 RUNNING）
        ("CLAIMED", "FAILED"),
        ("CLAIMED", "CANCELLED"),
        ("RUNNING", "WAITING_REVIEW"),
        ("RUNNING", "FAILED"),
        ("RUNNING", "CANCELLED"),
        ("WAITING_REVIEW", "APPROVED"),  # 任务级人工收口（≠成果物批准）
        ("WAITING_REVIEW", "NEEDS_REVISION"),
        ("READY", "CANCELLED"),
        ("FAILED", "READY"),  # 重试：负责人放回 READY
        ("NEEDS_REVISION", "READY"),
    }
)


def check_task_transition(prev: str, nxt: str) -> tuple[bool, str]:
    prev, nxt = str(prev).upper(), str(nxt).upper()
    if prev not in KNOWN_TASK_STATUSES:
        return False, f"unknown task status: {prev!r}"
    if nxt not in KNOWN_TASK_STATUSES:
        return False, f"unknown task status: {nxt!r}"
    if (prev, nxt) not in TASK_TRANSITIONS:
        return False, f"transition {prev}→{nxt} not in the known map (extend TASK_TRANSITIONS consciously if real)"
    return True, f"{prev}→{nxt}"


def check_task_lifecycle(steps: Sequence[str]) -> tuple[bool, str]:
    """把一串状态按顺序两两校验（空/单元素视为平凡通过）。"""
    for prev, nxt in zip(steps, steps[1:]):
        ok, detail = check_task_transition(prev, nxt)
        if not ok:
            return False, detail
    return True, "→".join(steps) if steps else "empty lifecycle"


# ==========================================================================
# 2. receipt 形状（RECEIPT_FORMAT §2/§3）
# ==========================================================================

_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_RECEIPT_NAME_MAX = 128


def check_receipt_shape(receipt: Mapping) -> tuple[bool, str]:
    if not isinstance(receipt, Mapping):
        return False, "receipt must be an object"
    version = receipt.get("receipt_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return False, f"receipt_version must be int >= 1 (0 means 'no receipt'), got {version!r}"
    for key in ("tool_name", "tool_call_id"):
        value = receipt.get(key)
        if not isinstance(value, str) or not value or len(value) > _RECEIPT_NAME_MAX:
            return False, f"{key} must be a non-empty string <= {_RECEIPT_NAME_MAX} chars"
    for key in ("args_hash", "output_hash"):
        value = receipt.get(key)
        if not isinstance(value, str) or not _HASH_RE.match(value):
            return False, f"{key} must be 16 lowercase hex chars, got {value!r}"
    output_bytes = receipt.get("output_bytes")
    if isinstance(output_bytes, bool) or not isinstance(output_bytes, int) or output_bytes < 0:
        return False, f"output_bytes must be int >= 0, got {output_bytes!r}"
    if "truncated" in receipt and not isinstance(receipt["truncated"], bool):
        return False, "truncated must be boolean when present"
    if "status" in receipt and receipt["status"] not in {"success", "failed"}:
        return False, f"status must be success|failed when present, got {receipt['status']!r}"
    return True, f"receipt v{version} by {receipt['tool_name']}"


# ==========================================================================
# 3. 团队视图 / 生产路径（project_team.py 的 payload 形状）
# ==========================================================================

_TEAM_AGENT_KEYS = ("agent_name", "device_id", "capabilities", "load", "current_tasks")


def check_team_view(team: Mapping, *, expect_agent_ids: Iterable[str] = ()) -> tuple[bool, str]:
    if not isinstance(team, Mapping):
        return False, "team payload must be an object"
    for key in ("agents", "agent_count", "online_count", "task_status_counts"):
        if key not in team:
            return False, f"missing team key: {key}"
    agents = team["agents"]
    if not isinstance(agents, list) or not isinstance(team["agent_count"], int):
        return False, "agents must be a list and agent_count an int"
    if team["agent_count"] != len(agents):
        return False, f"agent_count {team['agent_count']} != len(agents) {len(agents)}"
    online = [a for a in agents if isinstance(a, Mapping) and a.get("online") is True]
    if team["online_count"] != len(online):
        return False, f"online_count {team['online_count']} != online agents {len(online)}"

    for agent in agents:
        if not isinstance(agent, Mapping):
            return False, "agent entry must be an object"
        for key in _TEAM_AGENT_KEYS:
            if key not in agent:
                return False, f"agent entry missing key: {key}"
        if agent["load"] != len(agent["current_tasks"]):
            return False, f"agent {agent.get('agent_id')!r}: load {agent['load']} != len(current_tasks)"
        for key in ("current_tasks", "next_tasks", "waiting_tasks"):
            if not isinstance(agent[key], list) or not all(isinstance(item, Mapping) for item in agent[key]):
                return False, f"agent {agent.get('agent_id')!r}: {key} must be a list of objects"
        for handoff in agent.get("pending_handoffs", []):
            for key in ("handoff_id", "task_id", "status"):
                if key not in handoff:
                    return False, f"pending_handoffs entry missing key: {key}"

    status_counts = team["task_status_counts"]
    if not isinstance(status_counts, Mapping):
        return False, "task_status_counts must be an object"
    unknown = sorted(set(status_counts) - KNOWN_TASK_STATUSES)
    if unknown:
        return False, f"unknown task statuses in task_status_counts: {unknown}"

    missing = sorted(set(expect_agent_ids) - {a.get("agent_id") for a in agents if isinstance(a, Mapping)})
    if missing:
        return False, f"expected agents missing from team view: {missing}"
    return True, f"{len(agents)} agents ({len(online)} online), statuses={dict(status_counts)}"


_ARTIFACT_STATUSES_SEEN = frozenset({"APPROVED", "WAITING_REVIEW", "NEEDS_REVISION", "REJECTED"})


def check_production_path(payload: Mapping, *, expect_artifact_ids: Iterable[str] = ()) -> tuple[bool, str]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("nodes"), list):
        return False, "production-path payload must have a nodes list"
    nodes = payload["nodes"]
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            return False, "node must be an object"
        artifact_id = node.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return False, "node missing artifact_id"
        if artifact_id in seen:
            return False, f"duplicate node for artifact {artifact_id}"
        seen.add(artifact_id)
        version = node.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return False, f"artifact {artifact_id}: version must be int >= 1, got {version!r}"
        if not isinstance(node.get("status"), str) or not node["status"]:
            return False, f"artifact {artifact_id}: status must be a non-empty string"
        if node["status"] not in _ARTIFACT_STATUSES_SEEN:
            return False, (
                f"artifact {artifact_id}: unknown artifact status {node['status']!r} "
                "(extend _ARTIFACT_STATUSES_SEEN consciously if real)"
            )
        receipt = node.get("receipt")
        if receipt is not None:
            ok, detail = check_receipt_shape(receipt)
            if not ok:
                return False, f"artifact {artifact_id}: {detail}"
        downstream = node.get("downstream_tasks", [])
        if not isinstance(downstream, list) or not all(
            isinstance(item, Mapping) and isinstance(item.get("task_id"), str) and item["task_id"]
            for item in downstream
        ):
            return False, f"artifact {artifact_id}: downstream_tasks must be objects with task_id"
    missing = sorted(set(expect_artifact_ids) - seen)
    if missing:
        return False, f"expected artifacts missing from production path: {missing}"
    return True, f"{len(nodes)} artifact nodes"


def check_artifact_version_bump(old_version: int, new_version: int) -> tuple[bool, str]:
    """退回修订必须产生**恰好 +1** 的新版本（版本链不可跳号）。"""
    if new_version != old_version + 1:
        return False, f"expected version {old_version + 1}, got {new_version}"
    return True, f"v{old_version} → v{new_version}"


def check_artifact_feeds_task(payload: Mapping, artifact_id: str, task_id: str) -> tuple[bool, str]:
    """生产路径因果链：artifact 的 downstream_tasks 里必须出现指定任务。"""
    for node in payload.get("nodes", []) if isinstance(payload, Mapping) else []:
        if isinstance(node, Mapping) and node.get("artifact_id") == artifact_id:
            tasks = [item.get("task_id") for item in node.get("downstream_tasks", []) if isinstance(item, Mapping)]
            if task_id in tasks:
                return True, f"{artifact_id[:8]}… feeds {task_id[:8]}…"
            return False, f"artifact {artifact_id} does not feed task {task_id} (downstream: {tasks})"
    return False, f"artifact {artifact_id} not on the production path"


# ==========================================================================
# 4. project.* 事件流（AGENT_EVENT_CONTRACT）
# ==========================================================================


def _catalog() -> Mapping:
    try:
        from app.event_catalog import EVENTS  # noqa: PLC0415 —— e2e/测试环境都在 apps/api 路径上
    except ImportError as exc:  # pragma: no cover —— 环境坏了要响亮地失败，不是静默放过
        raise RuntimeError("event catalog unavailable: run with apps/api on sys.path") from exc
    return EVENTS


def check_event_stream(events: Sequence[Mapping], *, catalog: Mapping | None = None) -> tuple[bool, str]:
    """校验一串事件信封（AGENT_EVENT_CONTRACT §2/§3/规则 4）：

    - 目录形状的名字（``project.`` 前缀且 ≥3 段）必须在目录注册——未知即失败；
    - 老轨名（两段/无前缀）豁免，但逐个点名进 detail（只增不改，可观测）；
    - seq 为正整数且**跨列表严格递增**（契约规则 4）。
    """
    known = catalog if catalog is not None else _catalog()
    last_seq: int | None = None
    legacy: list[str] = []
    catalog_hits = 0
    for index, envelope in enumerate(events):
        if not isinstance(envelope, Mapping):
            return False, f"event #{index} is not an object"
        name = envelope.get("event")
        if not isinstance(name, str) or not name:
            return False, f"event #{index} missing event name"
        if name.startswith("project.") and name.count(".") >= 2:
            if name not in known:
                return False, f"event #{index}: {name!r} is catalog-shaped but not registered"
            catalog_hits += 1
        elif name not in known:
            legacy.append(name)
        seq = envelope.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            return False, f"event #{index} ({name}): seq must be a positive int, got {seq!r}"
        if last_seq is not None and seq <= last_seq:
            return False, f"event #{index} ({name}): seq {seq} not strictly increasing (last {last_seq})"
        last_seq = seq
    detail = f"{catalog_hits} catalog events, {len(legacy)} legacy"
    if legacy:
        detail += f" ({sorted(set(legacy))})"
    detail += f", seq {events[0].get('seq')}..{last_seq}" if events and last_seq is not None else ", empty"
    return True, detail


# ==========================================================================
# 5. 工作流引擎事件与账本（W3.2/W3.3 接线后的 e2e 断言；形状对齐
#    workflow_engine.py 的真实 wire 格式，2026-09-30）
# ==========================================================================

GATE_VERDICTS = frozenset({"HOLDS", "NOT_HOLDS", "UNVERIFIED"})


def check_gate_evaluated_payload(payload: Mapping) -> tuple[bool, str]:
    """``project.gate.evaluated`` payload 的一致性断言。

    wire 形状：``{task_id, node_id, verdict, leaves:[{criterion, family, verdict, detail}]}``
    （leaf 的 checked/holds 折叠进 verdict——acceptance.py 的 Kleene 三值）。
    一致性规则：verdict=HOLDS ⇔ 没有任何叶子 NOT_HOLDS/UNVERIFIED；
    verdict=NOT_HOLDS ⇔ 至少一个叶子 NOT_HOLDS；verdict=UNVERIFIED ⇔
    无 NOT_HOLDS 且至少一个 UNVERIFIED。
    """
    verdict = payload.get("verdict")
    if verdict not in GATE_VERDICTS:
        return False, f"gate verdict must be one of {sorted(GATE_VERDICTS)}, got {verdict!r}"
    for key in ("task_id", "node_id"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            return False, f"gate payload missing {key}"
    leaves = payload.get("leaves")
    if not isinstance(leaves, list) or not leaves:
        return False, "gate.evaluated must carry a non-empty leaves list"
    counts = {"HOLDS": 0, "NOT_HOLDS": 0, "UNVERIFIED": 0}
    for index, leaf in enumerate(leaves):
        if not isinstance(leaf, Mapping):
            return False, f"leaf #{index} is not an object"
        leaf_verdict = leaf.get("verdict")
        if leaf_verdict not in GATE_VERDICTS:
            return False, f"leaf #{index} verdict invalid: {leaf_verdict!r}"
        counts[leaf_verdict] += 1
        for key in ("criterion", "family"):
            if not isinstance(leaf.get(key), str) or not leaf[key]:
                return False, f"leaf #{index} missing {key}"
    if verdict == "HOLDS" and (counts["NOT_HOLDS"] or counts["UNVERIFIED"]):
        return False, f"HOLDS gate has failing leaves: {counts}"
    if verdict == "NOT_HOLDS" and counts["NOT_HOLDS"] == 0:
        return False, "NOT_HOLDS gate has no NOT_HOLDS leaf"
    if verdict == "UNVERIFIED" and (counts["NOT_HOLDS"] or counts["UNVERIFIED"] == 0):
        return False, f"UNVERIFIED gate inconsistent with leaves: {counts}"
    return True, f"{verdict}: {counts['HOLDS']}H/{counts['NOT_HOLDS']}F/{counts['UNVERIFIED']}U"


def check_gate_blocked_payload(payload: Mapping) -> tuple[bool, str]:
    """``project.gate.blocked`` payload：``unchecked[]`` 必须是字符串列表
    （硬失败触发的阻塞允许为空——NOT_HOLDS 而非 UNVERIFIED 也算 blocked），
    ``on_block`` ∈ {blocked, escalate_human}。"""
    on_block = payload.get("on_block")
    if on_block not in {"blocked", "escalate_human"}:
        return False, f"on_block must be blocked|escalate_human, got {on_block!r}"
    unchecked = payload.get("unchecked")
    if not isinstance(unchecked, list):
        return False, "unchecked must be a list (possibly empty for hard-fail blocks)"
    if not all(isinstance(item, str) and item for item in unchecked):
        return False, "unchecked entries must be non-empty strings"
    return True, f"blocked ({on_block}), {len(unchecked)} unchecked"


def check_stall_series(series: Sequence[int]) -> tuple[bool, str]:
    """账本 stall_count 序列合法性（progress_ledger 的记账规则的可观测投影）：
    每步只能 +1（不进步/在循环）或 -1（进步，地板 0）。"""
    if not series:
        return True, "empty series"
    for prev, nxt in zip(series, series[1:]):
        if nxt == prev + 1:
            continue
        if prev > 0 and nxt == prev - 1:
            continue
        if prev == 0 and nxt == 0:
            continue
        return False, f"illegal stall transition {prev}→{nxt} (only +1 or -1 with floor 0)"
    return True, f"stall {series[0]}..{series[-1]} over {len(series)} rounds"


def check_ledger_snapshot(data: Mapping) -> tuple[bool, str]:
    """账本 JSON 快照结构断言——直接复用 progress_ledger.ProgressLedger 的
    加载规则（单一权威，不在这里重写一遍结构清单）。"""
    try:
        from app.progress_ledger import ProgressLedger, LedgerUpdateError  # noqa: PLC0415

        ledger = ProgressLedger.from_json(data)
    except Exception as exc:  # LedgerUpdateError 或字段类型错——断言失败即结构非法
        return False, f"invalid ledger snapshot: {exc}"
    return True, f"round={ledger.round} stall={ledger.stall_count} done={ledger.done}"

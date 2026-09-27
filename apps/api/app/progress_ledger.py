"""工作流推进器的决策内核（实施计划 W3.2 纯逻辑部分，工作包 C）。

三个互不依赖的组件，推进器（A 侧 workflow_engine）按需组合：

1. **ProgressLedger**——Magentic-One 式进度账本（借鉴 autogen
   ``_magentic_one_orchestrator.py:300-440``）：每轮由 LLM 产出判定五件套，
   本模块负责**确定性记账**——结构校验、stall 计数（不进步 +1 / 在循环 +1 /
   否则衰减 -1 而非清零）、超限置 ``needs_replan``（重规划由引擎执行外层循环）。
   LLM 措辞不可信的部分一律 ``{answer, reason}`` 双字段，reason 只给人看。
2. **GoalTracker**——deer-flow 式 goal 续跑判停（``runtime/goal.py:333-391``）：
   只认类型化 blocker（白名单里只有 ``goal_not_met_yet`` 可续），判"没进展"
   用**最新可见产出的 SHA-256**——评估器的 reason 每次都被 LLM 改写，
   永远不能用来判停。
3. **resolve_dispatch**——派发回退链（借鉴 autogen selector
   ``_selector_group_chat.py:232-341`` 的无穷回退思想）：指定角色 → 能力匹配
   → 上一角色 → 首个可用 → 人工。链上绝不空转，落空就升级人。

本模块纯逻辑、无 IO：数据库、事件、LLM 调用全在引擎侧；这里只做规则。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "MAX_LEDGER_ENTRIES",
    "MAX_ENTRY_CHARS",
    "LedgerUpdateError",
    "LedgerEntry",
    "ProgressLedger",
    "GOAL_BLOCKERS",
    "CONTINUABLE_GOAL_BLOCKERS",
    "GOAL_NOT_MET_YET",
    "MAX_CONTINUATIONS",
    "MAX_NO_PROGRESS_CONTINUATIONS",
    "GoalState",
    "ContinuationDecision",
    "visible_output_signature",
    "decide_continuation",
    "GoalTracker",
    "normalize_role_name",
    "DispatchCandidate",
    "DispatchDecision",
    "resolve_dispatch",
]


# ==========================================================================
# 1. ProgressLedger —— Magentic-One 式账本
# ==========================================================================

MAX_LEDGER_ENTRIES = 20
MAX_ENTRY_CHARS = 500

#: 每轮账本判定的必填键（autogen 语义）。值必须是 ``{answer: bool, reason: str}``。
LEDGER_DECISION_KEYS = ("is_request_satisfied", "is_progress_being_made", "is_in_loop")
LEDGER_REQUIRED_KEYS = LEDGER_DECISION_KEYS + ("instruction_or_question", "next_speaker")


class LedgerUpdateError(ValueError):
    """LLM 产出的账本更新结构非法（引擎侧应带着纠正反馈重试，再失败再升级）。"""


@dataclass(frozen=True)
class LedgerEntry:
    answer: bool
    reason: str = ""

    @classmethod
    def parse(cls, value) -> "LedgerEntry":
        if not isinstance(value, Mapping) or not isinstance(value.get("answer"), bool):
            raise LedgerUpdateError("ledger decision must be {'answer': bool, 'reason': str}")
        reason = value.get("reason", "")
        if not isinstance(reason, str):
            raise LedgerUpdateError("ledger decision reason must be a string")
        return cls(answer=value["answer"], reason=reason[:MAX_ENTRY_CHARS])


@dataclass(frozen=True)
class ProgressLedger:
    """任务级账本。不可变对象：每轮 ``apply_round`` 返回新账本，便于按轮快照。"""

    task: str
    facts: tuple[str, ...] = ()
    plan: tuple[str, ...] = ()
    round: int = 0
    stall_count: int = 0
    done: bool = False
    needs_replan: bool = False
    last_next_speaker: str | None = None
    last_instruction: str = ""

    @classmethod
    def new(cls, task: str, *, facts: Iterable[str] = (), plan: Iterable[str] = ()) -> "ProgressLedger":
        return cls(
            task=task[:MAX_ENTRY_CHARS],
            facts=tuple(_bound_entries(facts)),
            plan=tuple(_bound_entries(plan)),
        )

    def apply_round(
        self,
        update: Mapping,
        *,
        participant_names: Sequence[str],
        max_stalls: int = 3,
    ) -> "ProgressLedger":
        """按 Magentic-One 规则推进一轮记账，返回新账本。

        ``update`` 是 LLM 产出的判定（必填键见 ``LEDGER_REQUIRED_KEYS``，
        判定键为 ``{answer, reason}``）；结构非法抛 ``LedgerUpdateError``
        ——引擎带纠正反馈重试，不静默猜。
        stall 计数规则与 autogen 逐字一致：不进步 +1；进步但在循环 +1；
        进步且不在循环则**衰减 -1**（``max(0, n-1)``，不清零——抖动也要付代价）。
        ``stall_count >= max_stalls`` 时置 ``needs_replan``，由引擎执行重规划。
        """
        for key in LEDGER_REQUIRED_KEYS:
            if key not in update:
                raise LedgerUpdateError(f"missing ledger key: {key}")
        satisfied = LedgerEntry.parse(update["is_request_satisfied"])
        progress = LedgerEntry.parse(update["is_progress_being_made"])
        in_loop = LedgerEntry.parse(update["is_in_loop"])

        instruction = update["instruction_or_question"]
        next_speaker = update["next_speaker"]
        if isinstance(next_speaker, Mapping):
            next_speaker = next_speaker.get("answer")
        if not satisfied.answer and next_speaker not in participant_names:
            raise LedgerUpdateError(
                f"next_speaker {next_speaker!r} is not a participant; participants: {sorted(participant_names)}"
            )
        if not isinstance(instruction, str):
            raise LedgerUpdateError("instruction_or_question must be a string")

        if not progress.answer:
            stall = self.stall_count + 1
        elif in_loop.answer:
            stall = self.stall_count + 1
        else:
            stall = max(0, self.stall_count - 1)

        return ProgressLedger(
            task=self.task,
            facts=self.facts,
            plan=self.plan,
            round=self.round + 1,
            stall_count=stall,
            done=satisfied.answer,
            needs_replan=(not satisfied.answer and stall >= max_stalls),
            last_next_speaker=None if satisfied.answer else next_speaker,
            last_instruction=instruction[:MAX_ENTRY_CHARS],
        )

    def with_facts(self, facts: Iterable[str]) -> "ProgressLedger":
        return ProgressLedger(**{**self.__dict__, "facts": tuple(_bound_entries(facts))})

    def with_plan(self, plan: Iterable[str]) -> "ProgressLedger":
        return ProgressLedger(**{**self.__dict__, "plan": tuple(_bound_entries(plan))})

    def to_json(self) -> dict:
        return {
            "task": self.task,
            "facts": list(self.facts),
            "plan": list(self.plan),
            "round": self.round,
            "stall_count": self.stall_count,
            "done": self.done,
            "needs_replan": self.needs_replan,
            "last_next_speaker": self.last_next_speaker,
            "last_instruction": self.last_instruction,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> "ProgressLedger":
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        for key in ("facts", "plan"):
            if key in payload:
                payload[key] = tuple(payload[key])
        try:
            return cls(**payload)
        except TypeError as exc:
            raise LedgerUpdateError(f"invalid ledger json: {exc}") from exc


def _bound_entries(entries: Iterable[str]) -> tuple[str, ...]:
    kept = [str(e)[:MAX_ENTRY_CHARS] for e in entries if str(e).strip()]
    return tuple(kept[-MAX_LEDGER_ENTRIES:])


# ==========================================================================
# 2. GoalTracker —— deer-flow 式 goal 续跑判停
# ==========================================================================

GOAL_NOT_MET_YET = "goal_not_met_yet"
#: 类型化 blocker。只有"目标还没达成"可自动续跑；缺证据/要人/失败/外部等待
#: 都必须停下来交回，绝不拿续跑预算硬闯。
GOAL_BLOCKERS = ("none", "missing_evidence", "needs_user_input", "run_failed", "external_wait", GOAL_NOT_MET_YET)
CONTINUABLE_GOAL_BLOCKERS = frozenset({GOAL_NOT_MET_YET})

MAX_CONTINUATIONS = 8
MAX_NO_PROGRESS_CONTINUATIONS = 2


def visible_output_signature(text: str) -> str:
    """对最新**可见**助手产出取稳定签名（deer-flow goal.py:346 语义）。

    判"没进展"看的是 agent 实际拿出了什么——评估器/思考文本每次都被 LLM
    改写，永远不重复，不能作为判停依据；可见产出不变（哪怕措辞换了）
    才算原地踏步。
    """
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GoalState:
    blocker: str = "none"
    last_signature: str = ""
    signature_repeat_count: int = 0
    continuation_count: int = 0

    def to_json(self) -> dict:
        return {
            "blocker": self.blocker,
            "last_signature": self.last_signature,
            "signature_repeat_count": self.signature_repeat_count,
            "continuation_count": self.continuation_count,
        }

    @classmethod
    def from_json(cls, data: Mapping) -> "GoalState":
        return cls(
            blocker=str(data.get("blocker", "none")),
            last_signature=str(data.get("last_signature", "")),
            signature_repeat_count=int(data.get("signature_repeat_count", 0)),
            continuation_count=int(data.get("continuation_count", 0)),
        )


@dataclass(frozen=True)
class ContinuationDecision:
    should_continue: bool
    reason: str


def decide_continuation(
    evaluation: Mapping,
    *,
    continuation_count: int,
    no_progress_count: int,
    max_continuations: int = MAX_CONTINUATIONS,
    max_no_progress: int = MAX_NO_PROGRESS_CONTINUATIONS,
) -> ContinuationDecision:
    """goal 判停四问（deer-flow goal.py:333-343 语义）：

    满足 → 停；blocker 不在可续白名单 → 停；续跑预算耗尽 → 停；
    无进展连续超限 → 停；否则续跑。
    ``no_progress_count`` 传"最新可见产出签名连续重复的次数"。
    """
    if evaluation.get("satisfied"):
        return ContinuationDecision(False, "goal satisfied")
    blocker = evaluation.get("blocker")
    if blocker not in CONTINUABLE_GOAL_BLOCKERS:
        return ContinuationDecision(False, f"blocker {blocker!r} is not continuable (needs human or upstream)")
    if continuation_count >= max_continuations:
        return ContinuationDecision(False, f"continuation budget exhausted ({continuation_count}/{max_continuations})")
    if no_progress_count >= max_no_progress:
        return ContinuationDecision(False, f"no visible progress for {no_progress_count} consecutive continuations")
    return ContinuationDecision(True, "goal_not_met_yet with fresh progress budget")


class GoalTracker:
    """goal 状态机：产出签名 + blocker 观察 + 判停判定，纯内存可序列化。"""

    def __init__(self, state: GoalState | None = None) -> None:
        self.state = state or GoalState()

    def observe_output(self, text: str) -> "GoalTracker":
        """记录一轮可见产出；与上轮签名相同则重复计数 +1，不同则归 1。"""
        signature = visible_output_signature(text)
        if signature == self.state.last_signature:
            repeat = self.state.signature_repeat_count + 1
        else:
            repeat = 1
        self.state = GoalState(
            blocker=self.state.blocker,
            last_signature=signature,
            signature_repeat_count=repeat,
            continuation_count=self.state.continuation_count,
        )
        return self

    def note_blocker(self, blocker: str) -> "GoalTracker":
        if blocker not in GOAL_BLOCKERS:
            raise ValueError(f"unknown blocker {blocker!r}; known: {GOAL_BLOCKERS}")
        if blocker != self.state.blocker:
            # blocker 变了，签名重复计数重新开始（deer-flow 按 (blocker, 签名) 对计数）。
            self.state = GoalState(
                blocker=blocker,
                last_signature=self.state.last_signature,
                signature_repeat_count=0,
                continuation_count=self.state.continuation_count,
            )
        else:
            self.state = GoalState(**{**self.state.to_json(), "blocker": blocker})
        return self

    def mark_continuation(self) -> "GoalTracker":
        self.state = GoalState(**{**self.state.to_json(), "continuation_count": self.state.continuation_count + 1})
        return self

    def decide(self, evaluation: Mapping, *, max_continuations: int = MAX_CONTINUATIONS,
               max_no_progress: int = MAX_NO_PROGRESS_CONTINUATIONS) -> ContinuationDecision:
        return decide_continuation(
            evaluation,
            continuation_count=self.state.continuation_count,
            no_progress_count=self.state.signature_repeat_count,
            max_continuations=max_continuations,
            max_no_progress=max_no_progress,
        )

    def to_json(self) -> dict:
        return self.state.to_json()

    @classmethod
    def from_json(cls, data: Mapping) -> "GoalTracker":
        return cls(GoalState.from_json(data))


# ==========================================================================
# 3. resolve_dispatch —— 派发回退链（selector 语义的确定性部分）
# ==========================================================================


def normalize_role_name(name: str) -> str:
    """角色名归一化（autogen ``_mentioned_agents`` 思路）：
    大小写、下划线/连字符与空格、首尾空白都视为等价，防止"选人失败"其实只是拼法差异。"""
    cleaned = str(name or "").strip().lower()
    for sep in ("_", "-", "　"):
        cleaned = cleaned.replace(sep, " ")
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class DispatchCandidate:
    role_id: str
    name: str
    capabilities: frozenset[str] = frozenset()
    available: bool = True


@dataclass(frozen=True)
class DispatchDecision:
    role_id: str | None
    #: requested | capability | previous | first | none
    source: str
    reason: str


def _caps_ok(candidate: DispatchCandidate, required: Iterable[str]) -> bool:
    return set(required) <= set(candidate.capabilities)


def resolve_dispatch(
    *,
    candidates: Sequence[DispatchCandidate],
    required_capabilities: Iterable[str] = (),
    requested_role: str | None = None,
    previous_role: str | None = None,
) -> DispatchDecision:
    """派发回退链：指定角色 → 能力匹配 → 上一角色 → 首个可用 → 人工（none）。

    - **requested**：点名角色按名字或 id 归一化匹配，且可用、能力齐；
    - **previous**：上一角色能力齐就优先续用（连续性高于声明顺序——
      它带着上一轮上下文，对应 autogen selector 回退 previous speaker；
      能力不齐**不**选它：回退为了连续性，不为绕过能力约束）；
    - **capability**：按候选声明顺序找第一个能力齐且可用的（顺序即模板声明序，
      模板作者用顺序表达偏好）；
    - **first**：首个可用候选，reason 注明能力豁免（autogen 回退到
      首个 participant 的对应物）；
    - **none**：没有任何可用候选——引擎据此升级人工，绝不空转。
    """
    required = frozenset(required_capabilities)
    available = [c for c in candidates if c.available]
    if not available:
        return DispatchDecision(None, "none", "no available candidate; escalate to human")

    if requested_role:
        wanted = normalize_role_name(requested_role)
        hit = next(
            (c for c in available if wanted in (normalize_role_name(c.name), normalize_role_name(c.role_id))),
            None,
        )
        if hit is not None and _caps_ok(hit, required):
            return DispatchDecision(hit.role_id, "requested", f"requested role {hit.name!r} matched")

    capable = next((c for c in available if _caps_ok(c, required)), None)

    if previous_role:
        wanted = normalize_role_name(previous_role)
        prev = next(
            (c for c in available if wanted in (normalize_role_name(c.name), normalize_role_name(c.role_id))),
            None,
        )
        if prev is not None and _caps_ok(prev, required):
            return DispatchDecision(prev.role_id, "previous", f"falling back to previous role {prev.name!r}")

    if capable is not None:
        return DispatchDecision(capable.role_id, "capability", f"capability match: {capable.name!r}")

    first = available[0]
    waived = " (capability waived)" if required and not _caps_ok(first, required) else ""
    return DispatchDecision(first.role_id, "first", f"first available: {first.name!r}{waived}")

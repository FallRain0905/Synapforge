"""最小本地 Agent 运行时（守护进程型）。

一轮的顺序：**先消费等到的回复 → 再回答别人问我的 → 最后推进我手上的活**。
这个顺序是有意的：先消费才能让 WAITING 的节点回到 RUNNING，否则会一直卡在
"我已经拿到答案但没人把它接回去"。

**在飞任务用租约持有，不重新领取。** `claim_next_task` 只领 READY/NEEDS_REVISION；
Agent 问完问题回来时任务已是 RUNNING，重新领是领不到的——正确做法是记住自己的
task_id 与 lease_token，靠 heartbeat 续租后继续推进。租约丢了就如实放弃，不假装还在跑。

执行体用 LLM 做决定；**协议层一行都不碰网络**——网络只出现在执行体调 LLM 那一步。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import artifacts, commands
from .db import open_store
from .llm import DEFAULT_MAX_TOKENS, LlmClient
from .paths import db_path

DECISION_ACTIONS = ("ask", "concern", "finish")

DECISION_PROTOCOL = """\
你是一个通过 synapforge 本地协作协议工作的 Agent。你必须只回复一个 JSON 对象，不要任何其他文字。

可用的动作与形状：

1. 需要另一个 Agent 提供信息才能继续（例如缺一个事实、一个接口约定、一个上游产物）：
{"action": "ask", "to": "<对方 agent_id>", "request_type": "fact|context|interface|example|decision|capability|artifact_version|clarification",
 "question": "<你要问什么>", "reason": "<没有它你无法安全完成哪一步>"}

2. 任务在原口径下无法安全完成（输入缺失、前提不成立、契约不兼容、目标不可达、能力不足、预算/时间不够、验收冲突、继续不安全）：
{"action": "concern", "concern_type": "missing_input|invalid_assumption|incompatible_contract|impossible_goal|insufficient_capability|budget_or_time|acceptance_conflict|unsafe_to_continue",
 "claim": "<一句话说明为什么无法按原口径完成>", "basis": ["<证据引用，至少一条>"]}

3. 做完了：
{"action": "finish", "summary": "<可读结果摘要>", "completed": ["<完成项>"],
 "evidence_refs": ["<证据引用>"],
 "artifacts": [{"path": "<工作区内的相对路径，例如 result.md>", "content": "<文件正文>"}]}

规则：
- 缺信息就先问，不要猜；能自己做完就直接 finish。
- 已经把等到的回复写在输入里时，说明信息已经到了，不要再重复问同一个问题。
- **产出必须写成文件**（artifacts），不要只在 summary 里描述——文件才会变成带溯源回执的成果物。
- 不要把"我猜的"写成事实；不确定就 ask。
- 只有真的无法安全继续时才 concern，concern 必须给至少一条 basis。
"""

ANSWER_PROTOCOL = """\
你是 synapforge 本地协作协议里的一个 Agent，收到另一个 Agent 的信息请求。
你必须只回复一个 JSON 对象：

{"status": "answered|provisional|unavailable|rejected",
 "summary": "<可读回答；回答不了就说清为什么>",
 "facts": [{"key": "<名>", "value": "<值>"}],
 "evidence_refs": ["<证据引用>"]}

规则：
- 你知道答案 → status=answered，facts 里给出具体值。
- 你不确定但可以给暂定意见 → status=provisional，并在 assumptions 里列出假设。
- 你没有这个信息 → status=unavailable，不要编造。
"""

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_json_reply(text: str) -> dict[str, Any]:
    """从模型回复里取出 JSON 对象。容忍 ```json 围栏与前后杂文。"""

    raw = (text or "").strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("agent_reply_not_json") from None
        parsed = json.loads(raw[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("agent_reply_not_object")
    return parsed


class LocalAgent:
    def __init__(self, agent_id: str, project_id: Any, *, role: str, llm: LlmClient,
                 peer_ids: list[str] | None = None, store_path: Any = None,
                 workspace: Any = None,
                 lease_seconds: int = commands.DEFAULT_LEASE_SECONDS) -> None:
        self.agent_id = agent_id
        self.project_id = str(project_id)
        self.role = role
        self.llm = llm
        self.peer_ids = list(peer_ids or [])
        self.lease_seconds = lease_seconds
        self._store_path = store_path
        self._store: Any = None
        # 每个 Agent 一个工作区：产物落在这里，差分才认得出来是谁写的
        self._workspace = Path(workspace) if workspace is not None else artifacts.agent_workspace(agent_id)
        # 在飞任务：进程内持有。跨进程恢复需要持久化，属后续工作（见计划 §8）。
        self._inflight: dict[str, str] | None = None

    @property
    def workspace(self) -> Path:
        return self._workspace

    # ---- 生命周期 ----------------------------------------------------------

    @property
    def store(self) -> Any:
        if self._store is None:
            self._store = open_store(self._store_path or db_path())
            commands.ensure_participant(self._store, project_id=self.project_id,
                                        agent_id=self.agent_id)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    @property
    def inflight(self) -> dict[str, str] | None:
        return self._inflight

    # ---- 循环 --------------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        record: dict[str, Any] = {"agent_id": self.agent_id, "consumed": [], "answered": [], "work": None}
        self._consume_answers(record)
        self._serve_requests(record)
        self._advance_work(record)
        return record

    def serve(self, *, interval_seconds: float = 2.0, max_rounds: int = 12,
              idle_rounds: int = 2) -> list[dict[str, Any]]:
        """有界常驻循环：连续 `idle_rounds` 轮无事可做即退出。"""

        history: list[dict[str, Any]] = []
        idle = 0
        for _ in range(max_rounds):
            record = self.run_once()
            history.append(record)
            busy = bool(record["consumed"] or record["answered"] or record["work"])
            idle = 0 if busy else idle + 1
            if idle >= idle_rounds:
                break
            if not busy:
                time.sleep(interval_seconds)
        return history

    # ---- 步骤一：消费等到的回复 ----------------------------------------------

    def _consume_answers(self, record: dict[str, Any]) -> None:
        """消费别人给我的回复：这是 WAITING → RUNNING 的回归点。"""

        box = commands.inbox(self.store, agent_id=self.agent_id, project_id=self.project_id)
        for item in box.get("to_consume") or []:
            request_id = item.get("request_id")
            if not request_id:
                continue
            result = commands.consume(self.store, request_id=request_id, agent_id=self.agent_id)
            record["consumed"].append({
                "request_id": str(request_id),
                "status": result.get("status"),
                "question": item.get("question"),
                "node_effect_result": result.get("node_effect_result"),
            })

    # ---- 步骤二：回答别人问我的 ----------------------------------------------

    def _serve_requests(self, record: dict[str, Any]) -> None:
        box = commands.inbox(self.store, agent_id=self.agent_id, project_id=self.project_id)
        for item in box.get("to_answer") or []:
            request_id = item.get("request_id")
            if not request_id:
                continue
            reply = self._answer_via_llm(item)
            result = commands.answer(self.store, request_id=request_id, agent_id=self.agent_id,
                                     response=reply)
            record["answered"].append({
                "request_id": str(request_id),
                "status": result.get("status"),
                "summary": reply.get("answer_summary"),
            })

    def _answer_via_llm(self, item: dict[str, Any]) -> dict[str, Any]:
        user = (
            f"你的角色：{self.role}\n"
            f"提问方 Agent：{item.get('requester_agent_id')}\n"
            f"request_type：{item.get('request_type')}\n"
            f"问题：{item.get('question')}\n"
            f"提问方给的理由：{item.get('reason')}\n"
        )
        try:
            raw = self.llm.complete(system=ANSWER_PROTOCOL, user=user, max_tokens=DEFAULT_MAX_TOKENS)
            parsed = parse_json_reply(raw)
        except Exception as exc:  # noqa: BLE001 — 执行体失败要如实上报 UNAVAILABLE，不许假装答了
            return {"status": "unavailable",
                    "answer_summary": f"executor failed: {type(exc).__name__}",
                    "facts": [], "artifact_refs": [], "evidence_refs": [], "assumptions": []}
        status = str(parsed.get("status") or "unavailable")
        if status not in ("answered", "provisional", "unavailable", "rejected"):
            status = "unavailable"

        def _list(key: str) -> list[Any]:
            value = parsed.get(key)
            return value if isinstance(value, list) else []

        return {
            "status": status,
            "answer_summary": str(parsed.get("summary") or parsed.get("answer_summary") or "")[:2000],
            "facts": _list("facts"),
            "artifact_refs": [],
            "evidence_refs": _list("evidence_refs"),
            "assumptions": _list("assumptions"),
        }

    # ---- 步骤三：推进手上的活 ------------------------------------------------

    def _advance_work(self, record: dict[str, Any]) -> None:
        if self._inflight is not None:
            self._resume_inflight(record)
            return
        claimed = commands.claim(self.store, project_id=self.project_id, agent_id=self.agent_id,
                                 lease_seconds=self.lease_seconds)
        if not claimed.get("claimed"):
            return
        task = claimed["task"]
        lease = claimed["lease"]
        self._inflight = {"task_id": str(task.get("id")),
                          "lease_token": str(lease.get("lease_token"))}
        commands.mark_running(self.store, task_id=self._inflight["task_id"],
                              agent_id=self.agent_id,
                              lease_token=self._inflight["lease_token"],
                              message=f"{self.agent_id} started")
        self._execute(task, record)

    def _resume_inflight(self, record: dict[str, Any]) -> None:
        assert self._inflight is not None
        try:
            commands.heartbeat(self.store, lease_token=self._inflight["lease_token"],
                               agent_id=self.agent_id, extend_seconds=self.lease_seconds)
        except Exception as exc:  # noqa: BLE001 — 租约丢了就如实放弃，不假装还在跑
            record["work"] = {"action": "lease_lost", "task_id": self._inflight["task_id"],
                              "error": f"{type(exc).__name__}: {exc}"}
            self._inflight = None
            return
        task = self.store.get_task(self._inflight["task_id"])
        self._execute(task.model_dump(mode="json"), record)

    def _execute(self, task: dict[str, Any], record: dict[str, Any]) -> None:
        self._workspace.mkdir(parents=True, exist_ok=True)
        # 执行前记快照：产出靠"执行前后差分"识别，不靠 Agent 自己报清单
        before, before_truncated = artifacts.snapshot(self._workspace)
        context = self._build_context(task)
        try:
            raw = self.llm.complete(system=DECISION_PROTOCOL, user=context, max_tokens=DEFAULT_MAX_TOKENS)
            decision = parse_json_reply(raw)
        except Exception as exc:  # noqa: BLE001 — 执行体失败如实记录，不伪造成功
            record["work"] = {"task_id": str(task.get("id")), "action": "executor_failed",
                              "error": f"{type(exc).__name__}: {exc}"}
            return
        action = str(decision.get("action") or "")
        if action == "ask":
            outcome = self._do_ask(task, decision)
        elif action == "concern":
            outcome = self._do_concern(task, decision)
        elif action == "finish":
            outcome = self._do_finish(task, decision, before, before_truncated)
        else:
            outcome = {"action": "unknown", "decision": decision}
        # 只有 ask 让任务保持在飞；其余动作都收尾
        if action != "ask":
            self._inflight = None
        record["work"] = {"task_id": str(task.get("id")), **outcome}

    def _build_context(self, task: dict[str, Any]) -> str:
        lines = [
            f"你的角色：{self.role}",
            f"任务标题：{task.get('title')}",
            f"任务说明：{task.get('description') or '（无）'}",
            f"可询问的 peer agent：{', '.join(self.peer_ids) if self.peer_ids else '（无）'}",
        ]
        if self._inflight is not None:
            arrived = self._answers_for_task(self._inflight["task_id"])
            if arrived:
                lines.append("已经等到并消费的回复：")
                for item in arrived:
                    lines.append(f"  - 问题：{item.get('question')}")
                    lines.append(f"    回答：{item.get('answer_summary')}")
                    for fact in item.get("facts") or []:
                        lines.append(f"    事实：{fact.get('key')} = {fact.get('value')}")
            else:
                lines.append("你上一轮提出了信息请求，但回复还没到。如果还需要就再确认一次，"
                             "否则按现有信息处理。")
        return "\n".join(lines)

    def _answers_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.store.db.execute(
            "SELECT * FROM information_requests WHERE requester_task_id = ?"
            " AND status = 'CONSUMER_ACKNOWLEDGED' ORDER BY created_at",
            (str(task_id),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append({
                "question": row["question"],
                "answer_summary": payload.get("answer_summary") or "",
                "facts": payload.get("facts") or [],
            })
        return out

    # ---- 动作实现 -----------------------------------------------------------

    def _do_ask(self, task: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        to = str(decision.get("to") or (self.peer_ids[0] if self.peer_ids else ""))
        if not to:
            return {"action": "ask", "error": "no_peer_configured"}
        if to == self.agent_id:
            return {"action": "ask", "error": "cannot_ask_self"}
        assert self._inflight is not None
        result = commands.ask(
            self.store, project_id=self.project_id, agent_id=self.agent_id,
            provider_agent_id=to,
            request_type=str(decision.get("request_type") or "fact"),
            question=str(decision.get("question") or ""),
            reason=str(decision.get("reason") or ""),
            blocking="required",
            task_id=self._inflight["task_id"],
        )
        return {"action": "ask", "request_id": result.get("request_id"), "to": to,
                "status": result.get("status"),
                "node_effect_result": result.get("node_effect_result")}

    def _do_concern(self, task: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        basis = decision.get("basis") if isinstance(decision.get("basis"), list) else []
        if not basis:
            basis = ["executor:self_report"]
        result = commands.raise_concern(
            self.store, project_id=self.project_id, agent_id=self.agent_id,
            concern_type=str(decision.get("concern_type") or "missing_input"),
            claim=str(decision.get("claim") or ""),
            basis=[str(b) for b in basis],
            recommendation="request_information",
            can_continue_safely=False,
            task_id=task.get("id"),
        )
        return {"action": "concern", "concern_id": result.get("concern_id"),
                "status": result.get("status"), "node_effect": result.get("node_effect")}

    def _do_finish(self, task: dict[str, Any], decision: dict[str, Any],
                   before: dict[str, tuple[int, int]], before_truncated: bool) -> dict[str, Any]:
        assert self._inflight is not None
        written, write_errors = self._write_declared_files(decision)
        published, publish_errors = self._publish_outputs(task, before, before_truncated)
        errors = write_errors + publish_errors
        summary = str(decision.get("summary") or "done")
        if errors:
            # 产出出问题必须如实写进摘要，不许静默当成"已完成且无产出"
            summary = f"{summary}\n---\n产出登记问题：{'; '.join(errors)}"
        try:
            result = commands.finish_task(
                self.store, task_id=task.get("id"), agent_id=self.agent_id,
                lease_token=self._inflight["lease_token"], success=True,
                summary=summary,
                output_artifact_ids=[item["id"] for item in published],
            )
        except Exception as exc:  # noqa: BLE001 — 收尾失败如实记录（例如租约已丢）
            return {"action": "finish_failed", "error": f"{type(exc).__name__}: {exc}",
                    "written": written, "published": [i["name"] for i in published]}
        return {"action": "finish", "accepted": result.get("accepted"),
                "message": result.get("message"),
                "written": written,
                "artifacts": [{"id": item["id"], "name": item["name"],
                               "output_hash": item.get("output_hash"),
                               "size_bytes": item.get("size_bytes")} for item in published],
                "artifact_errors": errors}

    def _write_declared_files(self, decision: dict[str, Any]) -> tuple[list[str], list[str]]:
        """把 finish 声明的产出写进工作区（差分认得出来它们是新产出）。"""

        declarations = decision.get("artifacts")
        written: list[str] = []
        errors: list[str] = []
        if not isinstance(declarations, list):
            return written, errors
        root = self._workspace.resolve()
        for item in declarations:
            if not isinstance(item, dict):
                errors.append("artifact entry is not an object")
                continue
            relative = str(item.get("path") or "").strip()
            content = item.get("content")
            if not relative or content is None:
                errors.append("artifact entry missing path/content")
                continue
            target = (root / relative).resolve()
            # 挡穿越：声明写的路径必须落在自己的工作区内
            if target != root and root not in target.parents:
                errors.append(f"{relative}: path escapes workspace")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 用 write_bytes 而不是 write_text：后者在 Windows 上会把 \n 翻成 \r\n，
                # 于是磁盘字节与作者给的正文不一致——而 receipt 的 output_hash 正是对
                # **文件内容字节**算的，翻译会让哈希不可复现。
                target.write_bytes(str(content).encode("utf-8"))
                written.append(str(target.relative_to(root)).replace("\\", "/"))
            except OSError as exc:
                errors.append(f"{relative}: {type(exc).__name__}")
        return written, errors

    def _publish_outputs(self, task: dict[str, Any], before: dict[str, tuple[int, int]],
                         before_truncated: bool) -> tuple[list[dict[str, Any]], list[str]]:
        """差分 → 逐个登记成果物（带 receipt）。失败只记录，不改变任务成败。"""

        after, after_truncated = artifacts.snapshot(self._workspace)
        changed = artifacts.diff(before, after, before_truncated=before_truncated,
                                 after_truncated=after_truncated)
        published: list[dict[str, Any]] = []
        errors: list[str] = []
        if changed.before_truncated or changed.after_truncated:
            errors.append("工作区文件数超过扫描上限，产出清单可能不完整")
        for relative in changed.changed:
            try:
                view = artifacts.write_artifact(
                    self.store, project_id=self.project_id, workspace=self._workspace,
                    relative_path=relative, created_by=self.agent_id,
                    task_id=task.get("id"))
                published.append(view)
            except Exception as exc:  # noqa: BLE001 — 单个产出失败不拖垮收尾
                errors.append(f"{relative}: {type(exc).__name__}: {exc}")
        return published, errors

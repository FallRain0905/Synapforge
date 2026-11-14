"""CLI 入口：`python -m synapforge <动词> ...`

默认输出 JSON（主要消费者是 Agent）；`--human` 给人看的紧凑文本。
错误也走 stdout 的 JSON 并返回非零退出码，便于 Agent 直接解析。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import commands
from .db import open_store
from .paths import db_path


def _emit(payload: Any, *, human: bool = False, renderer=None) -> None:
    if human and renderer is not None:
        print(renderer(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(code: str, errors: list[str] | None = None, detail: str | None = None) -> int:
    print(json.dumps({"ok": False, "code": code, "errors": errors or [], "detail": detail},
                     ensure_ascii=False, indent=2))
    return 1


def _human_status(payload: dict[str, Any]) -> str:
    lines = [f"state: {payload.get('db_path')}"]
    tasks = payload.get("tasks") or []
    if tasks:
        lines.append("tasks:")
        for task in tasks:
            lines.append(f"  [{task.get('status', '?'):<14}] {(task.get('title') or '')[:56]}"
                         f"  {str(task.get('id') or '')[:8]}")
    requests = payload.get("requests") or []
    if requests:
        lines.append("requests:")
        for req in requests:
            lines.append(f"  [{req.get('status', '?'):<22}] {req.get('requester_agent_id')}"
                         f" -> {req.get('provider_agent_id')}  {str(req.get('question') or '')[:44]}")
    concerns = payload.get("concerns") or []
    if concerns:
        lines.append("concerns:")
        for concern in concerns:
            lines.append(f"  [{concern.get('status', '?'):<15}] {concern.get('concern_type')}"
                         f"  {str(concern.get('claim') or '')[:44]}")
    events = payload.get("events") or []
    if events:
        lines.append("events (latest):")
        for event in events[-8:]:
            lines.append(f"  {event.get('sequence')!s:>5}  {event.get('event_type')}")
    return "\n".join(lines)


def _human_inbox(payload: dict[str, Any]) -> str:
    lines = [f"agent: {payload.get('agent_id')}  (成员 {payload.get('member_id')})"]
    sections = (
        ("派给我（还没领）", "assigned_to_me"),
        ("进行中", "in_flight_tasks"),
        ("要你回答", "to_answer"),
        ("等你消费", "to_consume"),
    )
    for label, key in sections:
        items = payload.get(key) or []
        if items:
            lines.append(f"{label} ({len(items)}):")
            for item in items:
                title = item.get("question") or item.get("title") or item.get("request_id") or ""
                lines.append(f"  [{str(item.get('status', '?')):<22}] {str(title)[:52]}")
    if len(lines) == 1:
        lines.append("（没有待办）")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    # --state / --human 放在共享 parent 上，子命令前后都能写。
    # 默认值必须用 SUPPRESS：否则子解析器会用自身的默认值覆盖掉写在子命令**之前**的值
    # （argparse 会把同一 dest 在子解析器里再解析一次）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=argparse.SUPPRESS,
                        help="状态库路径（默认 ~/.synapforge/state.db）")
    common.add_argument("--human", action="store_true", default=argparse.SUPPRESS,
                        help="人类可读输出")

    parser = argparse.ArgumentParser(prog="synapforge", description="本地多 Agent 协作协议 CLI",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="总览：项目/任务/请求/异议/最近事件", parents=[common])
    p.add_argument("--agent")
    p.add_argument("--project")
    p.add_argument("--events", type=int, default=20)

    p = sub.add_parser("inbox", help="待我处理的一切", parents=[common])
    p.add_argument("--agent", required=True)
    p.add_argument("--project", required=True)

    p = sub.add_parser("events", help="事件流（事实源）", parents=[common])
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--after", type=int, default=None)

    p = sub.add_parser("project-ensure", help="按名字取或建项目", parents=[common])
    p.add_argument("--name", required=True)
    p.add_argument("--task-mode", default="manual")

    p = sub.add_parser("agent-ensure", help="注册本地 Agent（幂等）", parents=[common])
    p.add_argument("--id", required=True)
    p.add_argument("--name", default=None)

    p = sub.add_parser("task-create", help="建任务", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--stage", default="general")
    p.add_argument("--assignee-agent", default=None,
                   help="指派给某个 Agent；给了之后别的 Agent 领不到这条任务")
    p.add_argument("--required-contract", action="append", default=None,
                   help="本任务必须遵守的契约 key（可重复）；收尾时校验绑定版本")

    p = sub.add_parser("claim", help="领取下一个可做任务（拿租约）", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--lease-seconds", type=int, default=commands.DEFAULT_LEASE_SECONDS)
    p.add_argument("--stage", action="append", default=None)

    p = sub.add_parser("heartbeat", help="续租", parents=[common])
    p.add_argument("--token", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--extend-seconds", type=int, default=commands.DEFAULT_LEASE_SECONDS)

    p = sub.add_parser("start", help="任务进入 RUNNING", parents=[common])
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--message", default="")

    p = sub.add_parser("report", help="提交 StageReport（JSON 文件）", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("finish", help="完成任务", parents=[common])
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--fail", action="store_true", help="标记失败而不是成功")
    p.add_argument("--artifact", action="append", default=None)

    p = sub.add_parser("ask", help="向另一个 Agent 请求信息", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--type", dest="request_type", default="fact")
    p.add_argument("--blocking", default="required", choices=["required", "optional", "conditional"])
    p.add_argument("--deadline-minutes", type=int, default=commands.DEFAULT_DEADLINE_MINUTES)
    p.add_argument("--task", default=None)

    p = sub.add_parser("answer", help="回答请求", parents=[common])
    p.add_argument("--request", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--status", default="answered",
                   choices=["answered", "provisional", "unavailable", "rejected"])
    p.add_argument("--evidence", action="append", default=None)
    p.add_argument("--fact", action="append", default=None, help="key=value，可重复")
    p.add_argument("--assumption", action="append", default=None)

    p = sub.add_parser("consume", help="请求方确认消费回复", parents=[common])
    p.add_argument("--request", required=True)
    p.add_argument("--agent", required=True)

    p = sub.add_parser("concern", help="提交可行性异议（诚实停止）", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--type", dest="concern_type", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--basis", action="append", required=True)
    p.add_argument("--recommendation", default="request_information")
    p.add_argument("--can-continue", action="store_true")
    p.add_argument("--task", default=None)

    p = sub.add_parser("decide", help="编排决策（对异议）", parents=[common])
    p.add_argument("--concern", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--basis", action="append", required=True)
    p.add_argument("--expected-event", action="append", required=True)
    p.add_argument("--stop-reason", default=None)

    p = sub.add_parser("handoff-create", help="创建交接", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--receiver", default="Team")
    p.add_argument("--completed", action="append", default=None)
    p.add_argument("--question", action="append", default=None)
    p.add_argument("--evidence", action="append", default=None)

    p = sub.add_parser("handoff-accept", help="接收交接", parents=[common])
    p.add_argument("--handoff", required=True)
    p.add_argument("--receiver", required=True)

    p = sub.add_parser("handoff-reject", help="退回交接", parents=[common])
    p.add_argument("--handoff", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("handoff-list", help="交接列表", parents=[common])
    p.add_argument("--project", required=True)

    p = sub.add_parser("artifact-list", help="成果物列表（带溯源）", parents=[common])
    p.add_argument("--project", required=True)

    p = sub.add_parser("artifact-read", help="读回成果物内容", parents=[common])
    p.add_argument("--artifact", required=True)

    p = sub.add_parser("artifact-publish", help="把工作区文件登记成成果物", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--path", required=True, help="工作区内的相对路径")
    p.add_argument("--agent", required=True)
    p.add_argument("--task", default=None)

    p = sub.add_parser("contract-publish", help="发布契约新版本（正文落成成果物）", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--key", required=True, help="契约标识，例如 api")
    p.add_argument("--body", default=None, help="JSON 字符串")
    p.add_argument("--file", default=None, help="或从文件读 JSON")
    p.add_argument("--agent", required=True)

    p = sub.add_parser("contract-show", help="读契约当前版本与正文", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("contract-bind", help="声明按当前版本实现", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--task", default=None)

    p = sub.add_parser("contract-status", help="契约同步状态", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--agent", required=True)

    p = sub.add_parser("ui", help="启动本地只读查看器（按需启动，关掉就没了）", parents=[common])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--agents", default=None,
                   help="只展示这些 Agent（逗号分隔），侧栏保持干净")

    p = sub.add_parser("studio", help="启动本地 Agent 集群控制台（可写，仅回环）", parents=[common])
    p.add_argument("--port", type=int, default=0, help="0 = 随机端口")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    return parser


def _facts(pairs: list[str] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        out.append({"key": key.strip(), "value": value.strip()})
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    human = bool(getattr(args, "human", False))
    state = getattr(args, "state", None)

    # `ui` 在**打开读写库之前**处理：查看器只读，绝不能顺手建库或播种。
    if args.command == "ui":
        from .viewer import serve

        only = [a.strip() for a in (args.agents or "").split(",") if a.strip()]
        serve(port=args.port, open_browser=not args.no_browser, agents=only or None)
        return 0

    if args.command == "studio":
        from .studio.server import serve

        serve(port=args.port, open_browser=not args.no_browser, state_path=state)
        return 0

    store = open_store(state or db_path())
    try:
        if args.command == "status":
            payload = commands.status(store, agent_id=args.agent, project_id=args.project,
                                      event_limit=args.events)
            _emit(payload, human=human, renderer=_human_status)
        elif args.command == "inbox":
            payload = commands.inbox(store, agent_id=args.agent, project_id=args.project)
            _emit(payload, human=human, renderer=_human_inbox)
        elif args.command == "events":
            _emit(commands.recent_events(store, project_id=args.project, limit=args.limit,
                                         after_sequence=args.after))
        elif args.command == "project-ensure":
            _emit(commands.ensure_project(store, args.name, task_mode=args.task_mode))
        elif args.command == "agent-ensure":
            _emit(commands.ensure_agent(store, args.id, display_name=args.name))
        elif args.command == "task-create":
            _emit(commands.create_task(store, project_id=args.project, title=args.title,
                                       description=args.description, stage=args.stage,
                                       assignee_agent_id=args.assignee_agent,
                                       required_contracts=args.required_contract))
        elif args.command == "contract-publish":
            body = None
            if args.file:
                with open(args.file, encoding="utf-8") as handle:
                    body = json.load(handle)
            elif args.body:
                body = json.loads(args.body)
            if body is None:
                return _fail("contract_body_required", ["--body 或 --file 必须给一个"])
            _emit(commands.publish_contract(store, project_id=args.project, key=args.key,
                                            body=body, agent_id=args.agent))
        elif args.command == "contract-show":
            _emit(commands.show_contract(store, project_id=args.project, key=args.key))
        elif args.command == "contract-bind":
            _emit(commands.bind_contract(store, project_id=args.project, key=args.key,
                                         agent_id=args.agent, task_id=args.task))
        elif args.command == "contract-status":
            _emit(commands.contract_status(store, project_id=args.project,
                                           agent_id=args.agent))
        elif args.command == "claim":
            _emit(commands.claim(store, project_id=args.project, agent_id=args.agent,
                                 lease_seconds=args.lease_seconds, stages=args.stage))
        elif args.command == "heartbeat":
            _emit(commands.heartbeat(store, lease_token=args.token, agent_id=args.agent,
                                     extend_seconds=args.extend_seconds))
        elif args.command == "start":
            _emit(commands.mark_running(store, task_id=args.task, agent_id=args.agent,
                                        lease_token=args.token, message=args.message))
        elif args.command == "report":
            with open(args.file, encoding="utf-8") as handle:
                report = json.load(handle)
            _emit(commands.submit_report(store, project_id=args.project, task_id=args.task,
                                         agent_id=args.agent, report=report))
        elif args.command == "finish":
            _emit(commands.finish_task(store, task_id=args.task, agent_id=args.agent,
                                       lease_token=args.token, success=not args.fail,
                                       summary=args.summary,
                                       output_artifact_ids=args.artifact))
        elif args.command == "ask":
            _emit(commands.ask(store, project_id=args.project, agent_id=args.agent,
                               provider_agent_id=args.to, request_type=args.request_type,
                               question=args.question, reason=args.reason, blocking=args.blocking,
                               deadline_minutes=args.deadline_minutes, task_id=args.task))
        elif args.command == "answer":
            _emit(commands.answer(store, request_id=args.request, agent_id=args.agent, response={
                "status": args.status,
                "answer_summary": args.summary,
                "facts": _facts(args.fact),
                "artifact_refs": [],
                "evidence_refs": list(args.evidence or []),
                "assumptions": list(args.assumption or []),
            }))
        elif args.command == "consume":
            _emit(commands.consume(store, request_id=args.request, agent_id=args.agent))
        elif args.command == "concern":
            _emit(commands.raise_concern(store, project_id=args.project, agent_id=args.agent,
                                         concern_type=args.concern_type, claim=args.claim,
                                         basis=args.basis, recommendation=args.recommendation,
                                         can_continue_safely=args.can_continue, task_id=args.task))
        elif args.command == "decide":
            _emit(commands.decide_concern(store, concern_id=args.concern, policy=args.policy,
                                          rationale=args.rationale, basis=args.basis,
                                          expected_events=args.expected_event,
                                          stop_reason=args.stop_reason))
        elif args.command == "handoff-create":
            _emit(commands.create_handoff(store, project_id=args.project, task_id=args.task,
                                          sender_agent_id=args.agent, objective=args.objective,
                                          receiver=args.receiver, completed=args.completed,
                                          open_questions=args.question, evidence_refs=args.evidence))
        elif args.command == "handoff-accept":
            _emit(commands.accept_handoff(store, handoff_id=args.handoff, receiver=args.receiver))
        elif args.command == "handoff-reject":
            _emit(commands.reject_handoff(store, handoff_id=args.handoff, actor=args.actor,
                                          reason=args.reason))
        elif args.command == "handoff-list":
            _emit(commands.list_handoffs(store, project_id=args.project))
        elif args.command == "artifact-list":
            _emit(commands.list_artifacts(store, project_id=args.project))
        elif args.command == "artifact-read":
            _emit(commands.read_artifact(store, artifact_id=args.artifact))
        elif args.command == "artifact-publish":
            _emit(commands.publish_artifact(store, project_id=args.project,
                                            workspace=args.workspace, relative_path=args.path,
                                            agent_id=args.agent, task_id=args.task))
        else:  # pragma: no cover — argparse 已保证
            return _fail("unknown_command")
        return 0
    except Exception as exc:  # noqa: BLE001 — 归一成稳定 code，Agent 按 code 分支
        code = getattr(exc, "code", None) or type(exc).__name__
        errors = getattr(exc, "errors", None)
        return _fail(str(code), errors, str(exc))
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

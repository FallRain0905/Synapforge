"""MCP 薄壳（stdio）：把同一批动词暴露成 MCP 工具。

**薄的意思是**：这里没有第二套实现。每个工具直接调用 `commands.<动词>`，
不变量仍在命令层强制一次（护栏二）。MCP 只负责协议翻译与错误归一。

传输：stdio，**换行分隔的 JSON-RPC 2.0**（MCP stdio 规范：每条消息一行，不得内嵌换行）。
stdout **只放协议消息**——日志一律走 stderr，否则会污染协议流。

工具数控制在 15：工具 schema 会进模型上下文，这是稀缺资源。平台有约 300 条路由，
但 Agent 真正需要的动词是这十几个——这正是"动词层"这一步的意义。
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import commands
from .db import open_store
from .paths import db_path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "synapforge"
SERVER_VERSION = "0.1.0"

_DEFAULT_LEASE = commands.DEFAULT_LEASE_SECONDS


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
def _s(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}

def _i(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **extra}

def _arr(desc: str, items: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "array", "description": desc, "items": items or {"type": "string"}}

PROJECT = _s("项目 id；省略则用服务启动时指定的默认项目")
AGENT = _s("调用方 Agent id；省略则用服务启动时指定的默认 Agent")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "synapforge_status",
        "description": "总览：项目、任务、信息请求、可行性异议与最近事件。先看这个再决定做什么。",
        "inputSchema": _schema({
            "project_id": PROJECT, "agent_id": AGENT,
            "event_limit": _i("返回多少条最近事件，默认 20"),
        }),
    },
    {
        "name": "synapforge_inbox",
        "description": "待我处理的一切：派给我还没领的任务（assigned_to_me）、我领了在跑的任务与租约令牌"
                       "（in_flight_tasks）、要我回答的信息请求、等我消费的回复、交接。",
        "inputSchema": _schema({"project_id": PROJECT, "agent_id": AGENT}, []),
    },
    {
        "name": "synapforge_task_create",
        "description": "新建一个任务（工作单元）。给了 assignee_agent_id 就指派给该 Agent，别的 Agent 领不到。",
        "inputSchema": _schema({
            "project_id": PROJECT, "title": _s("任务标题"),
            "description": _s("任务说明"), "stage": _s("阶段标签，默认 general"),
            "assignee_agent_id": _s("指派给哪个 Agent；省略则任何 Agent 都能领"),
        }, ["title"]),
    },
    {
        "name": "synapforge_claim",
        "description": "领取下一个可做任务并拿到租约。没有可做任务时返回 claimed=false，不是错误。",
        "inputSchema": _schema({
            "project_id": PROJECT, "agent_id": AGENT,
            "lease_seconds": _i(f"租约时长秒，默认 {_DEFAULT_LEASE}", minimum=30),
            "stages": _arr("只领这些阶段的任务"),
        }),
    },
    {
        "name": "synapforge_heartbeat",
        "description": "续租。长时间执行时必须定期调用，否则租约过期后写操作会被拒绝。",
        "inputSchema": _schema({
            "lease_token": _s("claim 返回的租约令牌"), "agent_id": AGENT,
            "extend_seconds": _i(f"延长秒数，默认 {_DEFAULT_LEASE}", minimum=30),
        }, ["lease_token"]),
    },
    {
        "name": "synapforge_start",
        "description": "把任务置为 RUNNING（已领取后、开始干活前调用）。",
        "inputSchema": _schema({
            "task_id": _s("任务 id"), "agent_id": AGENT,
            "lease_token": _s("租约令牌"), "message": _s("进度说明"),
        }, ["task_id", "lease_token"]),
    },
    {
        "name": "synapforge_report",
        "description": "提交阶段报告（结构化：状态、完成项、未完成项、阻塞项、证据、是否可以安全继续）。"
                       "报告不是业务状态本身——Agent 自报「我完成了」不等于系统验收结论。",
        "inputSchema": _schema({
            "project_id": PROJECT, "task_id": _s("任务 id"), "agent_id": AGENT,
            "report": {"type": "object", "description":
                       "status（completed|partial|blocked|failed|needs_review|needs_decision）、summary、"
                       "completed_items、incomplete_items、output_artifacts、evidence_refs、blockers、"
                       "tests{passed,failed,skipped}、can_continue_safely"},
        }, ["task_id", "report"]),
    },
    {
        "name": "synapforge_finish",
        "description": "完成任务并登记产出。产出必须写成工作区文件才会成为带溯源回执的成果物。",
        "inputSchema": _schema({
            "task_id": _s("任务 id"), "agent_id": AGENT, "lease_token": _s("租约令牌"),
            "summary": _s("结果摘要"), "success": {"type": "boolean", "description": "默认 true"},
            "output_artifact_ids": _arr("要关联的成果物 id"),
        }, ["task_id", "lease_token"]),
    },
    {
        "name": "synapforge_ask",
        "description": "向另一个 Agent 请求信息。blocking=required 时本节点会进入 WAITING，"
                       "直到对方回答且你消费后才回到 RUNNING——这是异步的，对方不必在线。",
        "inputSchema": _schema({
            "project_id": PROJECT, "agent_id": AGENT,
            "provider_agent_id": _s("被询问的 Agent id"),
            "question": _s("要问什么"),
            "reason": _s("没有这条信息你无法安全完成哪一步"),
            "request_type": _s("fact|context|interface|example|decision|capability|artifact_version|clarification"),
            "blocking": _s("required|optional|conditional，默认 required"),
            "deadline_minutes": _i("回应截止分钟数，默认 30"),
            "task_id": _s("关联任务 id"),
        }, ["provider_agent_id", "question", "reason"]),
    },
    {
        "name": "synapforge_answer",
        "description": "回答别人对你的信息请求。答不上来要用 unavailable，不要编造。",
        "inputSchema": _schema({
            "request_id": _s("请求 id"), "agent_id": AGENT,
            "summary": _s("可读回答"),
            "status": _s("answered|provisional|unavailable|rejected，默认 answered"),
            "facts": _arr("事实对：key/value 对象数组", {"type": "object"}),
            "evidence_refs": _arr("证据引用"),
        }, ["request_id", "summary"]),
    },
    {
        "name": "synapforge_consume",
        "description": "确认消费收到的回复。这一步才会把 WAITING 的节点恢复成 RUNNING。",
        "inputSchema": _schema({"request_id": _s("请求 id"), "agent_id": AGENT}, ["request_id"]),
    },
    {
        "name": "synapforge_concern",
        "description": "诚实停止：任务在原口径下无法安全完成时提出可行性异议（必须给证据）。"
                       "会被记录并让节点进入 BLOCKED，等编排决策——不要伪造成功。",
        "inputSchema": _schema({
            "project_id": PROJECT, "agent_id": AGENT,
            "concern_type": _s("missing_input|invalid_assumption|incompatible_contract|impossible_goal|"
                               "insufficient_capability|budget_or_time|acceptance_conflict|unsafe_to_continue"),
            "claim": _s("一句话说明为什么无法按原口径完成"),
            "basis": _arr("证据引用，至少一条"),
            "recommendation": _s("编排建议，默认 request_information"),
            "task_id": _s("关联任务 id"),
        }, ["concern_type", "claim", "basis"]),
    },
    {
        "name": "synapforge_handoff_create",
        "description": "正式交接：把生产上下文（成果、风险、未决问题、下一步）交给下游。",
        "inputSchema": _schema({
            "project_id": PROJECT, "task_id": _s("任务 id"), "agent_id": AGENT,
            "objective": _s("交接目标"), "receiver": _s("接收方，默认 Team"),
            "completed": _arr("已完成项"), "open_questions": _arr("未决问题"),
            "evidence_refs": _arr("证据引用"),
        }, ["task_id", "objective"]),
    },
    {
        "name": "synapforge_handoff_decide",
        "description": "接收或退回一个交接。",
        "inputSchema": _schema({
            "handoff_id": _s("交接 id"),
            "action": _s("accept 或 reject"),
            "actor": _s("接收方标识（accept 时传接收方，reject 时传退回者）"),
            "reason": _s("reject 时的退回原因"),
        }, ["handoff_id", "action", "actor"]),
    },
    {
        "name": "synapforge_say",
        "description": "在项目群聊里说一句话（人类和 Agent 共用同一条时间线）。用于确认收到、汇报进度、说明决定。",
        "inputSchema": _schema({
            "text": _s("要说的话"),
            "project_id": PROJECT,
            "agent_id": AGENT,
        }, ["text"]),
    },
    {
        "name": "synapforge_contract",
        "description": "版本化接口契约。publish=发布新版本（正文落成成果物，带 hash）；"
                       "show=读当前版本与正文；bind=声明「我按这一版实现」（只能绑当前版）；"
                       "status=看同步状态。任务声明了必需契约时，绑定不同步会被拒绝收尾。",
        "inputSchema": _schema({
            "action": _s("publish | show | bind | status"),
            "key": _s("契约标识，例如 api"),
            "body": {"type": "object", "description": "publish 时的契约正文（JSON 对象）"},
            "version": _i("bind 时的版本；省略=当前最新版"),
            "task_id": _s("bind 时关联的任务 id"),
            "project_id": PROJECT, "agent_id": AGENT,
        }, ["action"]),
    },
    {
        "name": "synapforge_artifact",
        "description": "成果物：列出、读回内容、或把工作区文件登记成带溯源回执的成果物。",
        "inputSchema": _schema({
            "action": _s("list | read | publish"),
            "project_id": PROJECT,
            "artifact_id": _s("read 时的成果物 id"),
            "workspace": _s("publish 时的工作区目录"),
            "relative_path": _s("publish 时工作区内的相对路径"),
            "agent_id": AGENT,
            "task_id": _s("publish 时关联的任务 id"),
        }, ["action"]),
    },
    {
        "name": "synapforge_submit_plan",
        "description": (
            "把讨论结果提交成结构化任务计划。这是**唯一**正确的提交方式："
            "不要用文字描述计划，直接调用这个工具。提交后用户在界面上确认并执行。"
        ),
        "inputSchema": _schema({
            "title": _s("计划标题（短）"),
            "objective": _s("计划目标（一句话说清要达成什么）"),
            "tasks": {
                "type": "array",
                "description": "任务列表；每个任务分配给一个执行者，各自并行执行",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": _s("任务标题（短，可识别）"),
                        "description": _s("具体要做什么、交付什么文件"),
                        "agent_name": _s("执行者名字；留空=由用户指定"),
                        "model": _s("执行模型；留空=用该 Agent 默认"),
                        "role_prompt": _s("给这个执行者的一句话补充要求"),
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
            "plan_id": _s("提交到哪个规划；省略则用服务启动时的默认规划"),
        }, ["tasks"]),
        # 只在规划会话里暴露：普通协作会话的工具预算不该被它挤占
        "scopes": ("plan",),
    },
]

# 普通协作会话暴露的工具（排除规划专用）
PLAN_ONLY_TOOLS = frozenset({"synapforge_submit_plan"})


def tools_for(*, plan_mode: bool = False) -> list[dict[str, Any]]:
    """按会话类型裁剪工具集：规划会话多一个 submit_plan，普通会话看不到它。"""

    out: list[dict[str, Any]] = []
    for tool in TOOLS:
        plan_only = tool["name"] in PLAN_ONLY_TOOLS
        if plan_only and not plan_mode:
            continue
        item = {key: value for key, value in tool.items() if key != "scopes"}
        out.append(item)
    return out


# ---- 工具分发 ---------------------------------------------------------------


class McpToolError(RuntimeError):
    def __init__(self, code: str, errors: list[str] | None = None, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.errors = list(errors or [])
        self.detail = detail


def _need(args: dict[str, Any], key: str) -> Any:
    value = args.get(key)
    if value in (None, ""):
        raise McpToolError("missing_argument", [f"{key}: 必填"])
    return value


class McpServer:
    def __init__(self, *, agent_id: str | None = None, project_id: str | None = None,
                 store_path: Any = None, plan_id: str | None = None) -> None:
        self.default_agent = agent_id
        self.default_project = project_id
        self.default_plan = plan_id
        self._store_path = store_path
        self._store: Any = None

    @property
    def store(self) -> Any:
        if self._store is None:
            self._store = open_store(self._store_path or db_path())
            # 启动时把**默认** Agent 接进默认项目：本地单机场景下它就是"我自己的 Agent"，
            # 零仪式可用。单次调用里覆盖的 agent/project 不做隐式授权——那会变成悄悄放权。
            if self.default_agent and self.default_project:
                commands.ensure_participant(self._store, project_id=self.default_project,
                                            agent_id=self.default_agent)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def _project(self, args: dict[str, Any]) -> str:
        value = args.get("project_id") or self.default_project
        if not value:
            raise McpToolError("project_required",
                               ["project_id: 未提供，且服务启动时没有默认项目"])
        return str(value)

    def _agent(self, args: dict[str, Any]) -> str:
        value = args.get("agent_id") or self.default_agent
        if not value:
            raise McpToolError("agent_required",
                               ["agent_id: 未提供，且服务启动时没有默认 Agent"])
        return str(value)

    # ---- 分发 --------------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> Any:
        handler = getattr(self, f"_tool_{name.removeprefix('synapforge_')}", None)
        if handler is None:
            raise McpToolError("unknown_tool", [f"{name}: 未注册的工具"])
        return handler(dict(args or {}))

    def _tool_status(self, args: dict[str, Any]) -> Any:
        return commands.status(self.store, agent_id=args.get("agent_id") or self.default_agent,
                               project_id=args.get("project_id") or self.default_project,
                               event_limit=int(args.get("event_limit") or 20))

    def _tool_inbox(self, args: dict[str, Any]) -> Any:
        return commands.inbox(self.store, agent_id=self._agent(args), project_id=self._project(args))

    def _tool_task_create(self, args: dict[str, Any]) -> Any:
        return commands.create_task(self.store, project_id=self._project(args),
                                    title=_need(args, "title"),
                                    description=str(args.get("description") or ""),
                                    stage=str(args.get("stage") or "general"),
                                    assignee_agent_id=args.get("assignee_agent_id"))

    def _tool_claim(self, args: dict[str, Any]) -> Any:
        return commands.claim(self.store, project_id=self._project(args),
                              agent_id=self._agent(args),
                              lease_seconds=int(args.get("lease_seconds") or _DEFAULT_LEASE),
                              stages=args.get("stages"))

    def _tool_heartbeat(self, args: dict[str, Any]) -> Any:
        return commands.heartbeat(self.store, lease_token=_need(args, "lease_token"),
                                  agent_id=self._agent(args),
                                  extend_seconds=int(args.get("extend_seconds") or _DEFAULT_LEASE))

    def _tool_start(self, args: dict[str, Any]) -> Any:
        return commands.mark_running(self.store, task_id=_need(args, "task_id"),
                                     agent_id=self._agent(args),
                                     lease_token=_need(args, "lease_token"),
                                     message=str(args.get("message") or ""))

    def _tool_report(self, args: dict[str, Any]) -> Any:
        report = args.get("report")
        if not isinstance(report, dict):
            raise McpToolError("missing_argument", ["report: 必须是对象"])
        return commands.submit_report(self.store, project_id=self._project(args),
                                      task_id=_need(args, "task_id"),
                                      agent_id=self._agent(args), report=report)

    def _tool_finish(self, args: dict[str, Any]) -> Any:
        return commands.finish_task(self.store, task_id=_need(args, "task_id"),
                                    agent_id=self._agent(args),
                                    lease_token=_need(args, "lease_token"),
                                    success=bool(args.get("success", True)),
                                    summary=str(args.get("summary") or ""),
                                    output_artifact_ids=args.get("output_artifact_ids"))

    def _tool_ask(self, args: dict[str, Any]) -> Any:
        return commands.ask(self.store, project_id=self._project(args),
                            agent_id=self._agent(args),
                            provider_agent_id=_need(args, "provider_agent_id"),
                            request_type=str(args.get("request_type") or "fact"),
                            question=_need(args, "question"),
                            reason=_need(args, "reason"),
                            blocking=str(args.get("blocking") or "required"),
                            deadline_minutes=int(args.get("deadline_minutes") or 30),
                            task_id=args.get("task_id"))

    def _tool_answer(self, args: dict[str, Any]) -> Any:
        return commands.answer(self.store, request_id=_need(args, "request_id"),
                               agent_id=self._agent(args), response={
                                   "status": str(args.get("status") or "answered"),
                                   "answer_summary": str(args.get("summary") or ""),
                                   "facts": args.get("facts") or [],
                                   "artifact_refs": [],
                                   "evidence_refs": args.get("evidence_refs") or [],
                                   "assumptions": args.get("assumptions") or [],
                               })

    def _tool_consume(self, args: dict[str, Any]) -> Any:
        return commands.consume(self.store, request_id=_need(args, "request_id"),
                                agent_id=self._agent(args))

    def _tool_concern(self, args: dict[str, Any]) -> Any:
        basis = args.get("basis")
        if not isinstance(basis, list) or not basis:
            raise McpToolError("missing_argument", ["basis: 必须是非空证据数组"])
        return commands.raise_concern(self.store, project_id=self._project(args),
                                      agent_id=self._agent(args),
                                      concern_type=_need(args, "concern_type"),
                                      claim=_need(args, "claim"), basis=basis,
                                      recommendation=str(args.get("recommendation") or "request_information"),
                                      can_continue_safely=bool(args.get("can_continue_safely", False)),
                                      task_id=args.get("task_id"))

    def _tool_handoff_create(self, args: dict[str, Any]) -> Any:
        return commands.create_handoff(self.store, project_id=self._project(args),
                                       task_id=_need(args, "task_id"),
                                       sender_agent_id=self._agent(args),
                                       objective=_need(args, "objective"),
                                       receiver=args.get("receiver") or "Team",
                                       completed=args.get("completed"),
                                       open_questions=args.get("open_questions"),
                                       evidence_refs=args.get("evidence_refs"))

    def _tool_handoff_decide(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action") or "")
        handoff_id = _need(args, "handoff_id")
        actor = _need(args, "actor")
        if action == "accept":
            return commands.accept_handoff(self.store, handoff_id=handoff_id, receiver=actor)
        if action == "reject":
            return commands.reject_handoff(self.store, handoff_id=handoff_id, actor=actor,
                                           reason=str(args.get("reason") or "未给出原因"))
        raise McpToolError("invalid_argument", ["action: 必须是 accept 或 reject"])

    def _tool_say(self, args: dict[str, Any]) -> Any:
        return commands.post_message(self.store, project_id=self._project(args),
                                     sender=self._agent(args), text=_need(args, "text"),
                                     sender_kind="agent")

    def _tool_contract(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action") or "")
        if action == "publish":
            body = args.get("body")
            if not isinstance(body, (dict, list)):
                raise McpToolError("invalid_argument", ["body: publish 需要 JSON 对象"])
            return commands.publish_contract(self.store, project_id=self._project(args),
                                             key=_need(args, "key"), body=body,
                                             agent_id=self._agent(args))
        if action == "show":
            return commands.show_contract(self.store, project_id=self._project(args),
                                          key=_need(args, "key"))
        if action == "bind":
            return commands.bind_contract(self.store, project_id=self._project(args),
                                          key=_need(args, "key"), agent_id=self._agent(args),
                                          version=args.get("version"),
                                          task_id=args.get("task_id"))
        if action == "status":
            return commands.contract_status(self.store, project_id=self._project(args),
                                            agent_id=self._agent(args))
        raise McpToolError("invalid_argument", ["action: 必须是 publish / show / bind / status"])

    def _tool_artifact(self, args: dict[str, Any]) -> Any:
        action = str(args.get("action") or "")
        if action == "list":
            return commands.list_artifacts(self.store, project_id=self._project(args))
        if action == "read":
            return commands.read_artifact(self.store, artifact_id=_need(args, "artifact_id"))
        if action == "publish":
            return commands.publish_artifact(
                self.store, project_id=self._project(args),
                workspace=_need(args, "workspace"), relative_path=_need(args, "relative_path"),
                agent_id=self._agent(args), task_id=args.get("task_id"))
        raise McpToolError("invalid_argument", ["action: 必须是 list / read / publish"])

    def _tool_submit_plan(self, args: dict[str, Any]) -> Any:
        """把计划写进 local_cluster_plans，用户在界面上确认。"""

        from .studio import plans as studio_plans

        plan_id = str(args.get("plan_id") or self.default_plan or "").strip()
        if not plan_id:
            raise McpToolError(
                "plan_required",
                ["plan_id: 未提供，且服务启动时没有默认规划（这是规划会话专用的工具）"])
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise McpToolError("missing_argument", ["tasks: 至少一个任务"])
        items = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            title = str(task.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title[:120],
                "description": str(task.get("description") or "").strip()[:4000],
                "agent_name": str(task.get("agent_name") or "").strip(),
                "model": str(task.get("model") or "").strip(),
                "role_prompt": str(task.get("role_prompt") or "").strip()[:500],
            })
        if not items:
            raise McpToolError("missing_argument", ["tasks: 每条至少要有 title"])
        result = studio_plans.submit_plan(
            self.store, plan_id, items,
            title=str(args.get("title") or "").strip(),
            objective=str(args.get("objective") or "").strip())
        return result


# ---- JSON-RPC / stdio -------------------------------------------------------


def _error(code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "error": payload}


INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def handle_message(server: McpServer, message: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息。通知（无 id）返回 None。"""

    method = str(message.get("method") or "")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method.startswith("notifications/"):
        return None  # 通知不回包

    try:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            visible = tools_for(plan_mode=bool(getattr(server, "default_plan", None)))
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": visible}}
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": INVALID_PARAMS, "message": "arguments: 必须是对象"}}
            try:
                result = server.call(name, arguments)
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": False,
                }}
            except McpToolError as error:
                # 工具层错误是**结果**不是协议错误：Agent 需要读到 code 才能分支处理
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(
                        {"ok": False, "code": error.code, "errors": error.errors,
                         "detail": error.detail}, ensure_ascii=False)}],
                    "isError": True,
                }}
            except Exception as error:  # noqa: BLE001 — 归一成稳定 code
                return {"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(
                        {"ok": False, "code": getattr(error, "code", type(error).__name__),
                         "errors": getattr(error, "errors", []),
                         "detail": str(error)}, ensure_ascii=False)}],
                    "isError": True,
                }}
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": METHOD_NOT_FOUND, "message": f"未知方法：{method}"}}
    except Exception as error:  # noqa: BLE001 — 协议层兜底，不让主循环死掉
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": INTERNAL_ERROR, "message": f"{type(error).__name__}: {error}"}}


def serve_stdio(server: McpServer, *, stdin: Any = None, stdout: Any = None) -> int:
    """换行分隔的 JSON-RPC 主循环。stdout 只放协议消息，日志走 stderr。"""

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    for line in input_stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(-32700, "Parse error")
        else:
            try:
                response = handle_message(server, message)
            except Exception as error:  # noqa: BLE001 — 兜底，主循环必须继续
                print(f"[mcp] handler crash: {type(error).__name__}: {error}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                response = _error(INTERNAL_ERROR, type(error).__name__)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()
    return 0


__all__ = ["McpServer", "McpToolError", "PROTOCOL_VERSION", "TOOLS", "tools_for",
           "handle_message", "serve_stdio"]

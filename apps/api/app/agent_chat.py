"""「我的智能体」对话存储（MY-AGENT M-1）。

与任务体系**完全分开**——用户拍板"对话就是单纯对话"：这里不建 Task、不进任务板、不走复核，
也不产生 Run/成果物噪声。一轮对话 = 一行 `agent_turns`（用户消息 + 执行体回复同一行），
执行中的过程事件另存 `agent_turn_events`（一行一行回传，供页面流式渲染）。

执行体怎么取活：**轮询**（`claim_turn`），复用任务领取那套鉴权（项目能力令牌 + `X-Project-Capability-Token`），
能力名用 `chat.run`——没有这个能力或没被授权的设备取不到聊天轮次。

多轮上下文交给 **opencode 自己的 session**（`--session <ses_…>`，已实测跨进程有效）：
执行体从事件流里读到 `sessionID` 后随完成上报，平台存进会话，下一轮再带出去。平台不拼历史、不存模型 key。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence
from uuid import UUID, uuid4

from . import stream_bridge
from .contracts import (
    AGENT_TURN_STOP_REASONS,
    AgentChatConversation,
    AgentChatInputFile,
    AgentChatConversationCreate,
    AgentChatConversationUpdate,
    AgentChatMessageCreate,
    AgentChatTurn,
    AgentChatTurnApproval,
    AgentChatTurnApprovalRequest,
    AgentChatTurnApprovalState,
    AgentChatTurnClaimResult,
    AgentChatTurnComplete,
    AgentChatTurnEvent,
    AgentChatTurnEventReport,
    AgentChatTurnOutput,
    MyAgentEndpoint,
    MyAgentRole,
)

# 对话默认命令模板（平台把模板也交给执行体，执行体只做 `{prompt}` 替换 + 在它前面插入 --session/-m）。
DEFAULT_COMMAND_TEMPLATE = ["opencode", "run", "--format", "json", "--auto", "{prompt}"]
# 一轮的默认租约：执行体崩了不会把会话卡死（过期可被重新领取）。
DEFAULT_CLAIM_LEASE_SECONDS = 1800

# ---- 实时桥（W1.3，消费工作包 C 的 stream_bridge）--------------------------------
#
# 事件权威仍是 `agent_turn_events` 与轮次行本身；桥只是 SSE 加速器（断线重连从桥重放，
# 桥被裁剪就给 gap 让客户端回权威存储重同步）。两条流的游标口径：
#   * turn:{id}     seq == agent_turn_events.sequence（执行体上报的幂等键）——SSE 的
#                   `id:` 帧、桥的游标、权威存储三处一致，重连从任意一侧重放都不错位；
#   * conversation:{id}  seq 由桥自增（会话流只承载"轮次生命周期"信号：
#                   turn.created / turn.finished / turn.cancelled），gap 后客户端
#                   全量重取轮次列表即可。轮次内容不镜像到会话流（省内存、避免两套 seq）。
#
# 注意：`validate_envelope`（event_catalog）只认 `project.*` 注册名，适用于第三期
# project 事件写 outbox 的路径；轮次事件按契约文档是"沿用既有通道"的姊妹协议，
# 不进那套目录——这里的守卫是结构性的（seq 正整数、事件名非空、payload 是对象）。

_BRIDGE = stream_bridge.StreamBridge()


def bridge() -> stream_bridge.StreamBridge:
    """当前实时桥（测试可整体替换 `agent_chat._BRIDGE`）。"""

    return _BRIDGE


def turn_topic(turn_id: UUID) -> str:
    return f"turn:{turn_id}"


def conversation_topic(conversation_id: UUID) -> str:
    return f"conversation:{conversation_id}"


def _publish_turn_event_to_bridge(turn_id: UUID, sequence: int, event_type: str, payload: Mapping) -> None:
    """结构守卫后镜像一条轮次事件到桥（seq 用权威存储的 sequence，游标两处一致）。"""

    if int(sequence) < 1 or not str(event_type or "").strip() or not isinstance(payload, Mapping):
        return  # 守卫不过就少一条加速器缓存，绝不让它污染流或抛错打断落库路径
    try:
        _BRIDGE.publish(turn_topic(turn_id), str(event_type), dict(payload), seq=int(sequence))
    except ValueError:
        # 桥侧拒绝（seq 非递增等）只影响加速器，不影响权威存储——落库已成功，流上客户端会走 gap 重同步。
        pass


def _notify_conversation(conversation_id: UUID, action: str, data: dict[str, Any]) -> None:
    """会话流的轮次生命周期信号（turn.created/finished/cancelled；错误是事件，不是传输层异常）。"""

    try:
        _BRIDGE.publish(conversation_topic(conversation_id), action, data)
    except ValueError:
        pass


class AgentChatError(RuntimeError):
    """稳定错误族：会话归属、轮次状态与领取问题。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return fallback
    return loaded if isinstance(loaded, type(fallback)) else fallback


def ensure_schema(store: Any) -> None:
    """建表（幂等）。启动时调一次即可，函数内也做防御性调用。"""

    if getattr(store, "_agent_chat_schema_ready", False):
        return
    # 老库补列（CREATE TABLE IF NOT EXISTS 不会给已有表加新列）
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(agent_turns)")}
    if columns and "artifact_ids" not in columns:
        store.db.execute("ALTER TABLE agent_turns ADD COLUMN artifact_ids TEXT NOT NULL DEFAULT '[]'")
    if columns and "role" not in columns:
        # M-6：这一轮实际用的角色（谁跑的就记谁，供页面如实展示历史）
        store.db.execute("ALTER TABLE agent_turns ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if columns and "outputs" not in columns:
        # 2026-09-24：这一轮产出的文件（成果物，待审）——页面据此给出「下载 / 转入云盘」
        store.db.execute("ALTER TABLE agent_turns ADD COLUMN outputs TEXT NOT NULL DEFAULT '[]'")
    if columns and "variant" not in columns:
        store.db.execute("ALTER TABLE agent_turns ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
    if columns and "stop_reason" not in columns:
        # W1.1：为什么结束（status 说怎么结束，这列说为什么）。落库口径见 _normalize_stop_reason。
        store.db.execute("ALTER TABLE agent_turns ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''")
    conversation_columns = {row[1] for row in store.db.execute("PRAGMA table_info(agent_conversations)")}
    if conversation_columns and "role" not in conversation_columns:
        # M-6：会话上选的角色（空 = 默认，不传 `--agent`）
        store.db.execute("ALTER TABLE agent_conversations ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if conversation_columns and "variant" not in conversation_columns:
        store.db.execute("ALTER TABLE agent_conversations ADD COLUMN variant TEXT NOT NULL DEFAULT ''")

    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_conversations (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id),
            device_id TEXT NOT NULL REFERENCES devices(device_id),
            agent_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            variant TEXT NOT NULL DEFAULT '',
            command_template TEXT NOT NULL DEFAULT '[]',
            session_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_conversations_member ON agent_conversations(member_id, updated_at);
        CREATE TABLE IF NOT EXISTS agent_turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
            seq INTEGER NOT NULL,
            status TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            variant TEXT NOT NULL DEFAULT '',
            session_key TEXT,
            usage TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            claimed_by TEXT,
            artifact_ids TEXT NOT NULL DEFAULT '[]',
            outputs TEXT NOT NULL DEFAULT '[]',
            stop_reason TEXT NOT NULL DEFAULT '',
            claim_expires_at TEXT,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_turns_conversation ON agent_turns(conversation_id, seq);
        CREATE INDEX IF NOT EXISTS idx_agent_turns_status ON agent_turns(status, created_at);
        CREATE TABLE IF NOT EXISTS agent_turn_events (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES agent_turns(id),
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_turn_events_turn ON agent_turn_events(turn_id, sequence);
        CREATE TABLE IF NOT EXISTS agent_turn_approvals (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES agent_turns(id),
            conversation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT '',
            patterns TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            decided_by TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_turn_approvals_turn ON agent_turn_approvals(turn_id, status);
        CREATE INDEX IF NOT EXISTS idx_agent_turn_approvals_conversation ON agent_turn_approvals(conversation_id, created_at);
        """
    )
    store.db.commit()
    store._agent_chat_schema_ready = True


# ---- 读模型 ----------------------------------------------------------------


def _conversation(row: Any, turn_count: int = 0) -> AgentChatConversation:
    return AgentChatConversation(
        id=UUID(str(row["id"])),
        member_id=str(row["member_id"]),
        project_id=UUID(str(row["project_id"])),
        device_id=str(row["device_id"]),
        agent_id=str(row["agent_id"]) if row["agent_id"] else None,
        title=str(row["title"] or ""),
        model=str(row["model"] or ""),
        role=str(row["role"] or "") if "role" in row.keys() else "",
        variant=str(row["variant"] or "") if "variant" in row.keys() else "",
        session_key=str(row["session_key"]) if row["session_key"] else None,
        turn_count=turn_count,
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        archived_at=_parse_time(row["archived_at"]),
    )


def _normalize_stop_reason(value: str | None) -> str:
    """终止原因归一化：契约集合内的小写原样收，认不出的存 `unknown`，不编。

    与 `complete_turn` 的约定：执行体**留空** = 老执行体没这个字段，由调用方按 success 兜底；
    **非空但集合外** = 执行体报了我们不认识的值——照实存 unknown，两回事，不能混。
    """

    text = str(value or "").strip().lower()
    return text if text in AGENT_TURN_STOP_REASONS else "unknown"


def _turn(row: Any) -> AgentChatTurn:
    return AgentChatTurn(
        id=UUID(str(row["id"])),
        conversation_id=UUID(str(row["conversation_id"])),
        seq=int(row["seq"]),
        status=str(row["status"]),
        prompt=str(row["prompt"] or ""),
        content=str(row["content"] or ""),
        model=str(row["model"] or ""),
        role=str(row["role"] or "") if "role" in row.keys() else "",
        variant=str(row["variant"] or "") if "variant" in row.keys() else "",
        session_key=str(row["session_key"]) if row["session_key"] else None,
        usage=_json(row["usage"], {}),
        error=str(row["error"] or ""),
        claimed_by=str(row["claimed_by"]) if row["claimed_by"] else None,
        artifacts=_artifact_files(row["artifact_ids"]),
        outputs=_turn_outputs(row["outputs"]) if "outputs" in row.keys() else [],
        stop_reason=str(row["stop_reason"]) if "stop_reason" in row.keys() else "",
        created_at=_parse_time(row["created_at"]),
        completed_at=_parse_time(row["completed_at"]),
    )


def _artifact_files(raw: Any) -> list[AgentChatInputFile]:
    """轮次里的输入文件：新数据是 `[{artifact_id, name}]`；老的裸 id 列表也能读（名字留空）。"""

    files: list[AgentChatInputFile] = []
    for item in _json(raw, []):
        if isinstance(item, dict) and item.get("artifact_id"):
            files.append(AgentChatInputFile(artifact_id=UUID(str(item["artifact_id"])), name=str(item.get("name") or "")))
        elif isinstance(item, str) and item:
            files.append(AgentChatInputFile(artifact_id=UUID(item), name=""))
    return files


def _turn_event(row: Any) -> AgentChatTurnEvent:
    return AgentChatTurnEvent(
        id=UUID(str(row["id"])),
        turn_id=UUID(str(row["turn_id"])),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_json(row["payload"], {}),
        created_at=_parse_time(row["created_at"]),
    )


# ---- 我的智能体：权限请求（待批准卡片，M-5c S-3）--------------------------------

# 决策 → 状态：once/always 批准、reject 拒绝（与 opencode 实测的三档一一对应）
APPROVAL_STATUS_BY_DECISION = {"once": "APPROVED", "always": "APPROVED", "reject": "DENIED"}


def request_approval(store: Any, turn_id: UUID, agent_id: str, data: AgentChatTurnApprovalRequest) -> AgentChatTurnApproval:
    """执行体上报一条权限请求 → 建一张**待批准**卡片（同一个 request id 重报不产生第二张）。"""

    ensure_schema(store)
    row = store.db.execute("SELECT * FROM agent_turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        raise AgentChatError("turn_not_found")
    if row["claimed_by"] and str(row["claimed_by"]) != agent_id:
        raise AgentChatError("turn_claimed_by_other_agent")
    existing = store.db.execute("SELECT * FROM agent_turn_approvals WHERE id = ?", (data.request_id,)).fetchone()
    if existing is not None:
        return _approval(existing)
    store.db.execute(
        "INSERT INTO agent_turn_approvals (id, turn_id, conversation_id, status, permission, patterns, summary, tool, call_id, decision, created_at)"
        " VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, '', ?)",
        (
            data.request_id,
            str(turn_id),
            str(row["conversation_id"]),
            data.permission,
            json.dumps(list(data.patterns or []), ensure_ascii=False),
            data.summary,
            data.tool,
            data.call_id,
            _now(),
        ),
    )
    store.db.commit()
    return get_approval(store, turn_id, data.request_id)


def get_approval(store: Any, turn_id: UUID, request_id: str) -> AgentChatTurnApproval:
    ensure_schema(store)
    row = store.db.execute(
        "SELECT * FROM agent_turn_approvals WHERE id = ? AND turn_id = ?", (request_id, str(turn_id))
    ).fetchone()
    if row is None:
        raise AgentChatError("approval_not_found")
    return _approval(row)


def approval_state(store: Any, turn_id: UUID, request_id: str, agent_id: str) -> AgentChatTurnApprovalState:
    """执行体轮询：只看 `status` / `decision`（其余字段它不需要）。"""

    ensure_schema(store)
    row = store.db.execute(
        "SELECT a.*, t.claimed_by AS claimed_by FROM agent_turn_approvals a JOIN agent_turns t ON t.id = a.turn_id"
        " WHERE a.id = ? AND a.turn_id = ?",
        (request_id, str(turn_id)),
    ).fetchone()
    if row is None:
        raise AgentChatError("approval_not_found")
    if row["claimed_by"] and str(row["claimed_by"]) != agent_id:
        raise AgentChatError("turn_claimed_by_other_agent")
    return AgentChatTurnApprovalState(status=str(row["status"]), decision=str(row["decision"] or ""))


def decide_approval(store: Any, turn_id: UUID, request_id: str, member_id: str, decision: str) -> AgentChatTurnApproval:
    """人做决定。**只认第一次**：已定的卡片再点不会改写结局（幂等，与平台其余口径一致）。"""

    ensure_schema(store)
    owned = store.db.execute(
        "SELECT 1 FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id"
        " WHERE t.id = ? AND c.member_id = ?",
        (str(turn_id), member_id),
    ).fetchone()
    if owned is None:
        raise AgentChatError("turn_not_found")
    status = APPROVAL_STATUS_BY_DECISION.get(str(decision))
    if status is None:
        raise AgentChatError("approval_decision_invalid")
    row = store.db.execute(
        "SELECT * FROM agent_turn_approvals WHERE id = ? AND turn_id = ?", (request_id, str(turn_id))
    ).fetchone()
    if row is None:
        raise AgentChatError("approval_not_found")
    if str(row["status"]) != "PENDING":
        return _approval(row)
    store.db.execute(
        "UPDATE agent_turn_approvals SET status = ?, decision = ?, decided_by = ?, decided_at = ? WHERE id = ?",
        (status, str(decision), member_id, _now(), request_id),
    )
    store.db.commit()
    return get_approval(store, turn_id, request_id)


def expire_approval(store: Any, turn_id: UUID, request_id: str, agent_id: str) -> AgentChatTurnApproval:
    """执行体等不到人批（超时）→ 标 EXPIRED。

    为什么由执行体来标：它才知道"我不再等了"。标完它按 `reject` 回复 opencode——
    也就是说**没人批 = 不执行**，这与 `--auto` 时代相反，是刻意的（不假装有人同意）。
    """

    ensure_schema(store)
    state = approval_state(store, turn_id, request_id, agent_id)
    if state.status != "PENDING":
        return get_approval(store, turn_id, request_id)
    store.db.execute(
        "UPDATE agent_turn_approvals SET status = 'EXPIRED', decided_at = ? WHERE id = ?", (_now(), request_id)
    )
    store.db.commit()
    return get_approval(store, turn_id, request_id)


def list_turn_approvals(store: Any, turn_id: UUID, member_id: str) -> list[AgentChatTurnApproval]:
    ensure_schema(store)
    owned = store.db.execute(
        "SELECT 1 FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id"
        " WHERE t.id = ? AND c.member_id = ?",
        (str(turn_id), member_id),
    ).fetchone()
    if owned is None:
        raise AgentChatError("turn_not_found")
    rows = store.db.execute(
        "SELECT * FROM agent_turn_approvals WHERE turn_id = ? ORDER BY created_at ASC LIMIT 50", (str(turn_id),)
    ).fetchall()
    return [_approval(row) for row in rows]


def _approval(row: Any) -> AgentChatTurnApproval:
    patterns = _json(row["patterns"], [])
    return AgentChatTurnApproval(
        id=str(row["id"]),
        turn_id=UUID(str(row["turn_id"])),
        conversation_id=UUID(str(row["conversation_id"])),
        status=str(row["status"]),
        permission=str(row["permission"] or ""),
        patterns=[str(item) for item in patterns] if isinstance(patterns, list) else [],
        summary=str(row["summary"] or ""),
        tool=str(row["tool"] or ""),
        call_id=str(row["call_id"] or ""),
        decision=str(row["decision"] or ""),
        decided_by=str(row["decided_by"]) if row["decided_by"] else None,
        created_at=_parse_time(row["created_at"]),
        decided_at=_parse_time(row["decided_at"]),
    )


# ---- 我的智能体：可用执行体 --------------------------------------------------


def _turn_outputs(raw: Any) -> list[AgentChatTurnOutput]:
    """这一轮产出清单：解析失败当空（历史行/手改库都不该让整轮读不出来）。"""

    items = _json(raw, [])
    if not isinstance(items, list):
        return []
    outputs: list[AgentChatTurnOutput] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("artifact_id"):
            continue
        try:
            outputs.append(AgentChatTurnOutput(**item))
        except Exception:  # noqa: BLE001 - 单条坏数据跳过，不拖垮整轮
            continue
    return outputs


def list_my_agents(store: Any, member_id: str) -> list[MyAgentEndpoint]:
    """我名下、能跑对话的执行体（设备 → Agent → 授权项目 → 可用模型）。

    模型来源两处，按可信度排序：设备最近一次心跳自报的 `resource_summary["models"]`（执行体真探测到的），
    其次 Agent 注册时自报的 `model_name`（静态值，只当兜底）。**没有就如实留空**，不编。
    """

    ensure_schema(store)
    rows = store.db.execute(
        "SELECT d.device_id, d.device_name, d.agent_id, d.platform, d.status, d.last_seen, "
        "       a.display_name AS agent_name, a.model_name, "
        "       r.resource_summary, "
        "       g.project_id AS project_id, p.name AS project_name "
        "FROM devices d "
        "LEFT JOIN agents a ON a.agent_id = d.agent_id "
        "LEFT JOIN device_runtime_state r ON r.device_id = d.device_id "
        "LEFT JOIN device_project_grants g ON g.device_id = d.device_id AND g.revoked_at IS NULL "
        "LEFT JOIN projects p ON p.id = g.project_id "
        "WHERE d.owner_member_id = ? AND d.status = 'active' "
        "ORDER BY d.device_id",
        (member_id,),
    ).fetchall()
    endpoints: list[MyAgentEndpoint] = []
    seen: set[tuple[str, str]] = set()
    last_seen_threshold = datetime.now(UTC) - timedelta(seconds=180)
    for row in rows:
        project_id = str(row["project_id"] or "")
        if not project_id:
            # 没有授权项目的设备跑不了对话（能力令牌按项目签发）——如实不列出来
            continue
        key = (str(row["device_id"]), project_id)
        if key in seen:
            continue
        seen.add(key)
        summary = _json(row["resource_summary"], {})
        models = [str(item) for item in (summary.get("models") or []) if str(item).strip()]
        default_model = str(summary.get("default_model") or "")
        registered_model = str(row["model_name"] or "")
        if not default_model and registered_model and registered_model != "unspecified":
            # 注册时自报的模型名只当兜底；`unspecified` 是注册默认占位值，**不是模型**（显示它等于显示假数据）
            default_model = registered_model
        if default_model and default_model not in models:
            models = [default_model, *models]
        # 角色（M-6）：同样来自执行体心跳的真探测（`opencode agent list` + 角色文件的说明）。
        # 探不到就是空列表——页面据此**只显示「默认」**，不显示点不动的假选项。
        roles: list[MyAgentRole] = []
        for item in summary.get("roles") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                roles.append(
                    MyAgentRole(
                        name=name,
                        description=str(item.get("description") or "").strip(),
                        executes=bool(item.get("executes")),
                        # R-4：指纹/摘要/漂移照原样带给页面（平台只做展示，不改写执行体报的哈希）
                        sha256=str(item.get("sha256") or ""),
                        bytes=int(item.get("bytes") or 0),
                        modified_at=str(item.get("modified_at") or ""),
                        rules=[str(one) for one in (item.get("rules") or [])][:3],
                        drifted=bool(item.get("drifted")),
                        installed_at=str(item.get("installed_at") or ""),
                    )
                )
        last_seen = _parse_time(row["last_seen"])
        endpoints.append(
            MyAgentEndpoint(
                device_id=str(row["device_id"]),
                device_name=str(row["device_name"] or row["agent_name"] or ""),
                agent_id=str(row["agent_id"]) if row["agent_id"] else None,
                platform=str(row["platform"] or "unknown"),
                status=str(row["status"] or "unknown"),
                online=bool(last_seen and last_seen >= last_seen_threshold),
                project_id=UUID(project_id),
                project_name=str(row["project_name"] or ""),
                executor=str(summary.get("executor") or ""),
                models=models,
                default_model=default_model,
                roles=roles,
                conversation_count=store.db.execute(
                    "SELECT COUNT(*) AS c FROM agent_conversations WHERE member_id = ? AND device_id = ? AND archived_at IS NULL",
                    (member_id, str(row["device_id"])),
                ).fetchone()["c"],
            )
    )
    return endpoints


# ---- 会话 ------------------------------------------------------------------


def create_conversation(store: Any, member_id: str, data: AgentChatConversationCreate) -> AgentChatConversation:
    ensure_schema(store)
    device = store.db.execute(
        "SELECT device_id, agent_id, owner_member_id, status FROM devices WHERE device_id = ?", (data.device_id,)
    ).fetchone()
    if device is None:
        raise AgentChatError("device_not_found")
    if str(device["owner_member_id"]) != member_id:
        raise AgentChatError("device_not_owned_by_member")
    if str(device["status"]) != "active":
        raise AgentChatError("device_not_active")
    grants = store.db.execute(
        "SELECT project_id, capabilities FROM device_project_grants "
        "WHERE device_id = ? AND project_id = ? AND revoked_at IS NULL AND expires_at > ? "
        "ORDER BY created_at DESC",
        (data.device_id, str(data.project_id), _now()),
    ).fetchall()
    if not grants:
        raise AgentChatError("device_project_grant_missing")
    # 同一设备同项目可能有多条历史授权（轮换过令牌）：要挑**确实带 chat.run** 的那条，
    # 不能取 fetchone() 的第一条——那是旧授权，会把"能聊"误判成"不能聊"（踩过）。
    if not any("chat.run" in _json(row["capabilities"], []) for row in grants):
        # 没这个能力的设备取不到聊天轮次——在这里就说清楚，别让它变成"发出去没人接"
        raise AgentChatError("device_grant_missing_chat_capability")

    model = data.model.strip()
    if not model:
        runtime = store.db.execute(
            "SELECT resource_summary FROM device_runtime_state WHERE device_id = ?", (data.device_id,)
        ).fetchone()
        summary = _json(runtime["resource_summary"], {}) if runtime else {}
        model = str(summary.get("default_model") or "")
        if not model:
            # 与 `list_my_agents` 同一口径：心跳里没有就用注册时自报的（`unspecified` 是占位值，不是模型）。
            # 不这么兜的话会出现"页面显示默认模型、建出来的会话却是空模型"的不一致（实测踩到）。
            registered = store.db.execute(
                "SELECT model_name FROM agents WHERE agent_id = ?", (str(device["agent_id"] or ""),)
            ).fetchone()
            registered_model = str(registered["model_name"] or "") if registered else ""
            if registered_model and registered_model != "unspecified":
                model = registered_model
    template = list(data.command_template) or list(DEFAULT_COMMAND_TEMPLATE)
    if not any("{prompt}" in str(item) for item in template):
        raise AgentChatError("command_template_requires_prompt_placeholder")

    conversation_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO agent_conversations (id, member_id, project_id, device_id, agent_id, title, model, role, variant, command_template, session_key, created_at, updated_at, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)",
        (
            conversation_id,
            member_id,
            str(data.project_id),
            data.device_id,
            str(device["agent_id"]) if device["agent_id"] else None,
            data.title.strip(),
            model,
            data.role.strip(),
            data.variant,
            json.dumps(template, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    store.db.commit()
    return get_conversation(store, UUID(conversation_id), member_id)


def list_conversations(store: Any, member_id: str, *, limit: int = 100) -> list[AgentChatConversation]:
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM agent_turns t WHERE t.conversation_id = c.id) AS turn_count "
        "FROM agent_conversations c WHERE c.member_id = ? AND c.archived_at IS NULL "
        "ORDER BY c.updated_at DESC LIMIT ?",
        (member_id, int(limit)),
    ).fetchall()
    return [_conversation(row, int(row["turn_count"])) for row in rows]


def get_conversation(store: Any, conversation_id: UUID, member_id: str) -> AgentChatConversation:
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM agent_conversations WHERE id = ?", (str(conversation_id),)).fetchone()
    if row is None:
        raise AgentChatError("conversation_not_found")
    if str(row["member_id"]) != member_id:
        raise AgentChatError("conversation_not_owned_by_member")
    count = store.db.execute(
        "SELECT COUNT(*) AS c FROM agent_turns WHERE conversation_id = ?", (str(conversation_id),)
    ).fetchone()["c"]
    return _conversation(row, int(count))


def conversation_project_id(store: Any, conversation_id: UUID) -> str:
    ensure_schema(store)
    row = store.db.execute("SELECT project_id FROM agent_conversations WHERE id = ?", (str(conversation_id),)).fetchone()
    if row is None:
        raise AgentChatError("conversation_not_found")
    return str(row["project_id"])


def update_conversation(
    store: Any, conversation_id: UUID, member_id: str, data: AgentChatConversationUpdate
) -> AgentChatConversation:
    """改会话设置：**角色 / 模型**（M-6 起可在会话中途换，只影响**下一轮**）。

    为什么不改历史：轮次在建立时就把"这轮用的模型与角色"写在自己身上了——回头改会话设置去覆盖历史，
    会让界面上"这轮是谁跑的"变成谎话。所以这里只改会话，不动已有轮次。
    """

    ensure_schema(store)
    get_conversation(store, conversation_id, member_id)  # 归属校验
    updates: list[str] = []
    values: list[Any] = []
    if data.role is not None:
        updates.append("role = ?")
        values.append(data.role.strip())
    if data.model is not None:
        updates.append("model = ?")
        values.append(data.model.strip())
    if data.variant is not None:
        updates.append("variant = ?")
        values.append(data.variant)
    if data.title is not None:
        updates.append("title = ?")
        values.append(data.title.strip())
    if not updates:
        return get_conversation(store, conversation_id, member_id)
    updates.append("updated_at = ?")
    values.append(_now())
    values.append(str(conversation_id))
    store.db.execute(f"UPDATE agent_conversations SET {', '.join(updates)} WHERE id = ?", tuple(values))
    store.db.commit()
    return get_conversation(store, conversation_id, member_id)


def delete_conversation(store: Any, conversation_id: UUID, member_id: str) -> None:
    get_conversation(store, conversation_id, member_id)  # 归属校验
    # 桥清理要的 turn id 先取出来（删完行就查不到了）。
    turn_rows = store.db.execute(
        "SELECT id FROM agent_turns WHERE conversation_id = ?", (str(conversation_id),)
    ).fetchall()
    # 先清"挂在轮次上的东西"再删轮次：**漏一张表就是一整个会话删不掉**（S-3 的审批表踩过——
    # `agent_turn_approvals.turn_id` 有外键，不先删它，DELETE agent_turns 直接违反约束）。
    store.db.execute(
        "DELETE FROM agent_turn_approvals WHERE turn_id IN (SELECT id FROM agent_turns WHERE conversation_id = ?)",
        (str(conversation_id),),
    )
    store.db.execute(
        "DELETE FROM agent_turn_events WHERE turn_id IN (SELECT id FROM agent_turns WHERE conversation_id = ?)",
        (str(conversation_id),),
    )
    store.db.execute("DELETE FROM agent_turns WHERE conversation_id = ?", (str(conversation_id),))
    store.db.execute("DELETE FROM agent_conversations WHERE id = ?", (str(conversation_id),))
    store.db.commit()
    # 会话没了，实时桥上的话题一并清走（墓碑语义：阻塞中的订阅者收到 END，
    # 需要历史的客户端回权威存储——桥不装它不知道的事）。
    for row in turn_rows:
        _BRIDGE.cleanup(turn_topic(UUID(str(row["id"]))))
    _BRIDGE.cleanup(conversation_topic(conversation_id))


def list_turns(store: Any, conversation_id: UUID, member_id: str, *, limit: int = 200) -> list[AgentChatTurn]:
    ensure_schema(store)
    get_conversation(store, conversation_id, member_id)
    rows = store.db.execute(
        "SELECT * FROM agent_turns WHERE conversation_id = ? ORDER BY seq ASC LIMIT ?",
        (str(conversation_id), int(limit)),
    ).fetchall()
    return [_turn(row) for row in rows]


def list_turn_events(store: Any, turn_id: UUID, member_id: str) -> list[AgentChatTurnEvent]:
    ensure_schema(store)
    owned = store.db.execute(
        "SELECT 1 FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id "
        "WHERE t.id = ? AND c.member_id = ?",
        (str(turn_id), member_id),
    ).fetchone()
    if owned is None:
        raise AgentChatError("turn_not_found")
    rows = store.db.execute(
        "SELECT * FROM agent_turn_events WHERE turn_id = ? ORDER BY sequence ASC LIMIT 500", (str(turn_id),)
    ).fetchall()
    return [_turn_event(row) for row in rows]


# ---- 发消息与轮次 -----------------------------------------------------------


def append_message(store: Any, conversation_id: UUID, member_id: str, data: AgentChatMessageCreate) -> AgentChatTurn:
    """发一条用户消息 → 建一条 PENDING 轮次（执行体会把它取走）。"""

    ensure_schema(store)
    conversation_row = store.db.execute(
        "SELECT * FROM agent_conversations WHERE id = ?", (str(conversation_id),)
    ).fetchone()
    if conversation_row is None or str(conversation_row["member_id"]) != member_id:
        raise AgentChatError("conversation_not_found")
    if conversation_row["archived_at"]:
        raise AgentChatError("conversation_archived")
    attachments: list[dict[str, str]] = []
    for artifact_id in data.artifact_ids:
        # 附件必须是**本项目**的成果物：跨项目引用要在这里挡住（否则等于绕过项目边界读别人的文件）
        meta = store.db.execute("SELECT project_id, name FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if meta is None or str(meta["project_id"]) != str(conversation_row["project_id"]):
            raise AgentChatError("artifact_not_in_project")
        # 名字在这里就存下来：列表页要显示它，claim 时执行体也要它（省掉两条路径各自的查询）
        attachments.append({"artifact_id": str(artifact_id), "name": str(meta["name"] or "")})

    inflight = store.db.execute(
        "SELECT COUNT(*) AS c FROM agent_turns WHERE conversation_id = ? AND status IN ('PENDING', 'CLAIMED')",
        (str(conversation_id),),
    ).fetchone()["c"]
    if inflight:
        # 同一会话一次只允许一轮在跑：内核是串行的，排队反而让人看不懂（要排队就等这轮结束）
        raise AgentChatError("conversation_busy")

    next_seq = int(
        store.db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_turns WHERE conversation_id = ?", (str(conversation_id),)
        ).fetchone()[0]
    )
    turn_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO agent_turns (id, conversation_id, seq, status, prompt, content, model, role, variant, session_key, usage, error, claimed_by, artifact_ids, claim_expires_at, created_at, claimed_at, completed_at) "
        "VALUES (?, ?, ?, 'PENDING', ?, '', ?, ?, ?, NULL, '{}', '', NULL, ?, NULL, ?, NULL, NULL)",
        (
            turn_id,
            str(conversation_id),
            next_seq,
            data.content,
            str(conversation_row["model"] or ""),
            # 角色在**建这一轮**时就定下来（会话上后续改了角色只影响下一轮）——历史里"这轮谁跑的"才如实
            str(conversation_row["role"] or ""),
            # 强度同样在建轮次时冻结；下一轮才读取会话上的新设置。
            str(conversation_row["variant"] or ""),
            json.dumps(attachments, ensure_ascii=False),
            timestamp,
        ),
    )
    title = str(conversation_row["title"] or "").strip()
    if not title:
        # 第一句话就是标题（截 40 字），省得每建一个会话都要用户起名
        title = data.content.strip().splitlines()[0][:40]
    store.db.execute(
        "UPDATE agent_conversations SET updated_at = ?, title = ? WHERE id = ?",
        (timestamp, title, str(conversation_id)),
    )
    store.db.commit()
    # 会话流信号：有新轮次了（订阅方据此刷新轮次列表/打开新轮的事件流）
    _notify_conversation(conversation_id, "turn.created", {"turn_id": turn_id, "seq": next_seq})
    return get_turn(store, UUID(turn_id))


def get_turn(store: Any, turn_id: UUID) -> AgentChatTurn:
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM agent_turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        raise AgentChatError("turn_not_found")
    return _turn(row)


def claim_turn(store: Any, agent_id: str, project_id: UUID, lease_seconds: int) -> AgentChatTurnClaimResult | None:
    """执行体取走一条待办轮次（没有就返回 None——轮询空手是常态）。"""

    ensure_schema(store)
    device = store.db.execute("SELECT device_id FROM devices WHERE agent_id = ? AND status = 'active'", (agent_id,)).fetchone()
    if device is None:
        raise AgentChatError("agent_device_not_found")
    device_id = str(device["device_id"])
    timestamp = _now()
    row = store.db.execute(
        "SELECT t.*, c.project_id AS project_id, c.device_id AS device_id, c.command_template AS command_template, c.session_key AS conversation_session "
        "FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id "
        "WHERE c.device_id = ? AND c.project_id = ? AND c.archived_at IS NULL "
        "  AND (t.status = 'PENDING' OR (t.status = 'CLAIMED' AND (t.claim_expires_at IS NULL OR t.claim_expires_at < ?))) "
        "ORDER BY t.created_at ASC LIMIT 1",
        (device_id, str(project_id), timestamp),
    ).fetchone()
    if row is None:
        return None
    expires_at = (datetime.now(UTC) + timedelta(seconds=int(lease_seconds))).isoformat()
    store.db.execute(
        "UPDATE agent_turns SET status = 'CLAIMED', claimed_by = ?, claimed_at = ?, claim_expires_at = ? WHERE id = ?",
        (agent_id, timestamp, expires_at, str(row["id"])),
    )
    store.db.commit()
    turn = get_turn(store, UUID(str(row["id"])))
    # 会话句柄要**带下去**：轮次行本身没有（它是上一轮的产物），句柄存在会话上——
    # 漏了这一步，第二轮就退化成"没有上下文的新会话"（这条被测试抓过）。
    conversation_session = str(row["conversation_session"]) if row["conversation_session"] else None
    if conversation_session and not turn.session_key:
        turn = turn.model_copy(update={"session_key": conversation_session})
    # 输入文件：给执行体"要下什么"的元数据（名字给人看、id 用来下载）。
    # 成果物在这期间被删掉就跳过——不因为少一个附件整轮失败，执行体侧会把缺失写进提示词。
    input_files = _artifact_files(row["artifact_ids"])
    return AgentChatTurnClaimResult(
        turn=turn,
        conversation_id=UUID(str(row["conversation_id"])),
        project_id=UUID(str(row["project_id"])),
        workspace_hint=str(row["device_id"]),
        command_template=_json(row["command_template"], list(DEFAULT_COMMAND_TEMPLATE)),
        worker_events="opencode",
        input_files=input_files,
    )


def complete_turn(store: Any, turn_id: UUID, agent_id: str, data: AgentChatTurnComplete) -> AgentChatTurn:
    ensure_schema(store)
    row = store.db.execute("SELECT * FROM agent_turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        raise AgentChatError("turn_not_found")
    if row["claimed_by"] and str(row["claimed_by"]) != agent_id:
        raise AgentChatError("turn_claimed_by_other_agent")
    if str(row["status"]) in {"DONE", "CANCELLED"}:
        # 幂等：同一轮重复完成不再改写（网络重试不该把结果覆盖成空的）
        return _turn(row)
    timestamp = _now()
    session_key = (data.session_key or "").strip() or (str(row["session_key"]) if row["session_key"] else None)
    # 为什么结束（W1.1）：执行体报了就用它的（归一化后）；留空（老执行体）按结局兜底，
    # 但兜底**不掩盖失败**——success=False 又没报原因时是 failed，不是 completed。
    reported = str(data.stop_reason or "").strip()
    if reported:
        stop_reason = _normalize_stop_reason(reported)
    else:
        stop_reason = "completed" if data.success else "failed"
    store.db.execute(
        "UPDATE agent_turns SET status = ?, content = ?, session_key = ?, usage = ?, error = ?, outputs = ?, stop_reason = ?, completed_at = ? WHERE id = ?",
        (
            "DONE" if data.success else "FAILED",
            data.content,
            session_key,
            json.dumps(data.usage or {}, ensure_ascii=False),
            data.error,
            # 产出清单原样存下（id + 名字 + 大小）：页面读列表时不必再逐条查成果物
            json.dumps([item.model_dump(mode="json") for item in (data.outputs or [])], ensure_ascii=False),
            stop_reason,
            timestamp,
            str(turn_id),
        ),
    )
    if session_key:
        # 会话句柄向上冒泡：下一轮就带它续上下文
        store.db.execute(
            "UPDATE agent_conversations SET session_key = ?, updated_at = ? WHERE id = ?",
            (session_key, timestamp, str(row["conversation_id"])),
        )
    store.db.commit()
    # 流收尾：轮次话题宣布结束（订阅者收到 END 后回权威行取最终 content/stop_reason），
    # 会话流广播生命周期事件（订阅方据此刷新轮次列表）。
    _BRIDGE.publish_end(turn_topic(turn_id))
    _notify_conversation(
        UUID(str(row["conversation_id"])),
        "turn.finished",
        {"turn_id": str(turn_id), "status": "DONE" if data.success else "FAILED", "stop_reason": stop_reason},
    )
    return get_turn(store, turn_id)


def record_turn_event(store: Any, turn_id: UUID, agent_id: str, data: AgentChatTurnEventReport) -> AgentChatTurnEvent:
    ensure_schema(store)
    row = store.db.execute("SELECT claimed_by FROM agent_turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        raise AgentChatError("turn_not_found")
    if row["claimed_by"] and str(row["claimed_by"]) != agent_id:
        raise AgentChatError("turn_claimed_by_other_agent")
    existing = store.db.execute(
        "SELECT * FROM agent_turn_events WHERE turn_id = ? AND sequence = ?", (str(turn_id), int(data.sequence))
    ).fetchone()
    if existing is not None:
        # 序列号是幂等键：重传同一序号不该产生重复卡片
        return _turn_event(existing)
    event_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO agent_turn_events (id, turn_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, str(turn_id), int(data.sequence), data.event_type, json.dumps(data.payload or {}, ensure_ascii=False), timestamp),
    )
    store.db.commit()
    # 镜像到实时桥（加速器）：seq 用权威存储的 sequence，SSE 的 id: 帧/桥游标/权威存储三处一致。
    # 注意顺序：先落库后上桥——桥丢了无所谓（gap 重同步），权威存储丢了才是事故。
    _publish_turn_event_to_bridge(turn_id, int(data.sequence), data.event_type, dict(data.payload or {}))
    return _turn_event(
        store.db.execute("SELECT * FROM agent_turn_events WHERE id = ?", (event_id,)).fetchone()
    )


def cancel_turn(store: Any, turn_id: UUID, member_id: str, reason: str = "") -> AgentChatTurn:
    """取消一轮（只有本人、且还没跑完时允许）。

    诚实说明：M-1 只是把状态改成 CANCELLED，**执行体仍会把这一轮跑完**（平台没有到 Agent 的中断通道，
    见 MY_AGENT_CONSOLE_PLAN §7）。界面上要如实标注，别假装能中断。
    """

    ensure_schema(store)
    owned = store.db.execute(
        "SELECT t.* FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id "
        "WHERE t.id = ? AND c.member_id = ?",
        (str(turn_id), member_id),
    ).fetchone()
    if owned is None:
        raise AgentChatError("turn_not_found")
    if str(owned["status"]) in {"DONE", "FAILED", "CANCELLED"}:
        raise AgentChatError("turn_already_finished")
    timestamp = _now()
    store.db.execute(
        "UPDATE agent_turns SET status = 'CANCELLED', stop_reason = 'cancelled', error = ?, completed_at = ? WHERE id = ?",
        (reason or "cancelled_by_member", timestamp, str(turn_id)),
    )
    store.db.commit()
    # 流收尾（与 complete_turn 同口径）：轮次话题结束，会话流广播取消。
    _BRIDGE.publish_end(turn_topic(turn_id))
    _notify_conversation(
        UUID(str(owned["conversation_id"])),
        "turn.cancelled",
        {"turn_id": str(turn_id), "status": "CANCELLED", "stop_reason": "cancelled"},
    )
    return get_turn(store, turn_id)


def ensure_turn_owned(store: Any, turn_id: UUID, member_id: str) -> None:
    """SSE 订阅前的可见性门：轮次必须属于该成员的会话（与 list_turn_events 同口径，但不搬数据）。"""

    ensure_schema(store)
    owned = store.db.execute(
        "SELECT 1 FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id "
        "WHERE t.id = ? AND c.member_id = ?",
        (str(turn_id), member_id),
    ).fetchone()
    if owned is None:
        raise AgentChatError("turn_not_found")


def turn_project_id(store: Any, turn_id: UUID) -> str:
    ensure_schema(store)
    row = store.db.execute(
        "SELECT c.project_id FROM agent_turns t JOIN agent_conversations c ON c.id = t.conversation_id WHERE t.id = ?",
        (str(turn_id),),
    ).fetchone()
    if row is None:
        raise AgentChatError("turn_not_found")
    return str(row["project_id"])


__all__ = [
    "AgentChatError",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_COMMAND_TEMPLATE",
    "append_message",
    "bridge",
    "cancel_turn",
    "claim_turn",
    "complete_turn",
    "conversation_topic",
    "conversation_project_id",
    "create_conversation",
    "delete_conversation",
    "ensure_schema",
    "ensure_turn_owned",
    "get_conversation",
    "get_turn",
    "list_conversations",
    "list_my_agents",
    "list_turn_events",
    "list_turns",
    "record_turn_event",
    "turn_project_id",
    "turn_topic",
]
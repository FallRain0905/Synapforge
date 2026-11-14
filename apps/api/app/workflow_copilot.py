"""工作流 Copilot（会话式工作流搭建助手）。

定位（与平台既有边界一致）：
- AI 通过 ``ai_chat.chat_routed``（渠道优先、成员自配兜底、渠道接住后**不静默回落**）
  产出**内部围栏**的 workflow 定义草稿；除该围栏外不接受任何自然语言作为草稿来源。
- 草稿必须过 ``workflow_service.validate_definition``；校验失败时把逐条错误回喂给
  模型做**有界自动修复**（默认 2 次），修不好就如实失败——绝不静默放宽。
- 预览复用 ``workflow_service.preview_definition``（零副作用），发布复用
  ``workflow_service.create_workflow``（键冲突/校验语义与其完全一致），运行复用
  ``workflow_service.start_workflow_run``——本模块不写第二个预览/物化实现。
- **AI 永远不能发布/运行**：publish 与 run 只存在于需要人工 ``confirmed=true`` 的
  HTTP 路由（main.py），且必须携带最新修订号（stale 拒绝）；发布幂等
  （published_* 回执一致时返回既有 workflow），运行在服务端再做项目权限检查。

数据落库（dev SQLite 由本模块 ensure_schema；生产 PostgreSQL 走迁移 041）：
  workflow_copilot_sessions / workflow_copilot_turns / workflow_copilot_decisions
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from . import ai_chat, knowledge_base, llm_channels, workflow_service

__all__ = [
    "WorkflowCopilotError",
    "ensure_schema",
    "create_session",
    "get_session",
    "list_sessions",
    "list_turns",
    "run_turn",
    "save_draft",
    "preview_draft",
    "publish_draft",
    "run_published",
    "draft_fingerprint",
    "SYSTEM_PROMPT",
    "MAX_AUTO_REPAIR_ROUNDS",
]

# 有界自动修复：首答 + 至多 2 轮修复（超出 = 如实失败，不烧第三把火）
MAX_AUTO_REPAIR_ROUNDS = 2

# AI 输出里唯一被承认的草稿来源：```workflow-draft 围栏（内部协议，不对外承诺）
_DRAFT_FENCE_RE = re.compile(r"```workflow-draft\s*\n([\s\S]*?)```", re.IGNORECASE)
# synapforge-choice 围栏：助手用它向用户提问（与 agent-response.tsx 同一协议）
_CHOICE_FENCE_RE = re.compile(r"```synapforge-choice\s*\n([\s\S]*?)```", re.IGNORECASE)

_DRAFT_TEMPLATE = """你是数学建模平台的工作流搭建助手。你**只能**通过输出一个 workflow 定义草稿来帮助用户，不接受自己编造流程以外的任务。

工作流定义 JSON Schema（严格按此形状，字段名一字不差）：
{
  "schema_version": 1,
  "workflow": {"key": "小写字母数字连字符-2到64位", "name": "中文名", "description": "一句话", "inputs": [{"name": "topic", "required": true, "hint": "提示"}]},
  "stages": [{"id": "outline", "title": "大纲", "goal": "目标（可选）"}],
  "role_bindings": [{"id": "writer", "role_name": "写作", "description": "职责", "capability_requirements": ["files.write"], "prompt_overrides": {"system": "角色提示词"}}],
  "handoff_contracts": [{"id": "a-to-b", "from_role": "writer", "to_role": "writer", "required_fields": ["字段"], "artifact_refs": ["产物名"]}],
  "gate_policies": [{"id": "outline-gate", "spec": ["file:outputs/outline.md non-empty", {"any": ["artifact:document approved", "review:复核 concluded"]}], "on_block": "blocked 或 escalate_human"}],
  "delivery_adapters": [{"id": "docx_export", "kind": "docx_export", "config": {}}],
  "nodes": [
    {
      "id": "write_outline", "stage_id": "outline", "title": "写大纲", "goal": "目标",
      "depends_on": [], "mode": "auto 或 hybrid 或 manual",
      "role_binding": "writer（auto/hybrid 必填；manual 节点必须没有此字段）",
      "prompt": {"task_template": "可用 {{input.输入名}} 插值"},
      "inputs": [{"from_output": "上游产物名"} 或 {"input": "工作流输入名"}],
      "outputs": [{"name": "outline_doc", "artifact_type": "document|figure|code|result_table|review_report|paper_source|compiled_pdf|submission_bundle", "path": "outputs/xx.md（工作区相对路径，禁绝对路径与 ..）"}],
      "gate_policy": "outline-gate（可选）", "budget": {"max_seconds": 3600, "max_attempts": 2, "max_tokens": 200000},
      "retry_policy": {"backoff_seconds": 30}, "on_fail": "retry 或 handoff:角色id 或 escalate_human",
      "human_intervention": "none 或 before 或 after 或 approval_gate", "delivery_adapter": "docx_export（可选）"
    }
  ]
}
硬规则：
1. 只输出一个 ```workflow-draft 围栏，围栏内只有一个 JSON 对象，围栏外可以写一句话解释。
2. 需要用户做选择时，在围栏之外追加一个 ```synapforge-choice 围栏（{"id": "...", "title": "...", "questions": [{"id", "label", "type": "single|multiple|text", "required": true, "options": [{"value", "label", "description"}]}]}）。
3. depends_on 必须无环且全部可解析；所有 id 唯一；from_output 必须指向某节点输出名。
4. gate spec 的字符串必须形如 file:<path> exists|non-empty、file_written:<path>、tests_passed:<cmd>、artifact:<类型> approved、review:<角色> concluded。
5. 不要把用户的问题复述成定义以外的东西；不确定的信息先问（用 synapforge-choice），不要猜。
"""

SYSTEM_PROMPT = _DRAFT_TEMPLATE

# synapforge-choice 的封闭形状（服务端校验，前端 agent-choice.ts 已有同款判定）
_QUESTION_TYPES = {"single", "multiple", "text"}


class WorkflowCopilotError(RuntimeError):
    """稳定错误族（HTTP 层映射状态码）。``errors`` 逐条列出，绝不静默丢弃。"""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        super().__init__(code if not errors else f"{code}:{' | '.join(errors[:3])}")
        self.code = code
        self.errors = errors or []


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def ensure_schema(store: Any) -> None:
    """dev SQLite 建表（幂等）；生产 PostgreSQL 走迁移 041（含严格 RLS）。"""

    if getattr(store, "_workflow_copilot_schema_ready", False):
        return
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_copilot_sessions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            project_id TEXT,
            member_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            draft TEXT NOT NULL DEFAULT '{}',
            draft_revision INTEGER NOT NULL DEFAULT 0,
            draft_hash TEXT NOT NULL DEFAULT '',
            draft_valid INTEGER NOT NULL DEFAULT 0,
            published_workflow_id TEXT,
            published_revision INTEGER NOT NULL DEFAULT 0,
            published_hash TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS workflow_copilot_sessions_member_idx
            ON workflow_copilot_sessions(organization_id, member_id, updated_at);
        CREATE TABLE IF NOT EXISTS workflow_copilot_turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES workflow_copilot_sessions(id),
            organization_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            prompt TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            via TEXT NOT NULL DEFAULT '',
            usage TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            errors TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS workflow_copilot_turns_session_idx
            ON workflow_copilot_turns(session_id, seq);
        CREATE TABLE IF NOT EXISTS workflow_copilot_decisions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES workflow_copilot_sessions(id),
            turn_id TEXT NOT NULL REFERENCES workflow_copilot_turns(id),
            organization_id TEXT NOT NULL,
            choice_id TEXT NOT NULL DEFAULT '',
            answers TEXT NOT NULL DEFAULT '{}',
            questions TEXT NOT NULL DEFAULT '[]',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_copilot_decisions_turn_idx
            ON workflow_copilot_decisions(turn_id);
        """
    )
    store.db.commit()
    store._workflow_copilot_schema_ready = True


# ---- 视图 -------------------------------------------------------------------


def _session_view(row: Any, *, with_draft: bool = False) -> dict[str, Any]:
    view = {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "project_id": str(row["project_id"]) if row["project_id"] else None,
        "member_id": str(row["member_id"]),
        "title": str(row["title"] or ""),
        "status": str(row["status"]),
        "draft_revision": int(row["draft_revision"]),
        "draft_valid": bool(row["draft_valid"]),
        "published_workflow_id": str(row["published_workflow_id"]) if row["published_workflow_id"] else None,
        "published_revision": int(row["published_revision"] or 0),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if with_draft:
        view["draft"] = json.loads(row["draft"] or "{}")
        view["draft_hash"] = str(row["draft_hash"] or "")
        view["validation_errors"] = _validate_cached(json.loads(row["draft"] or "{}")) if row["draft_valid"] == 0 and row["draft_hash"] else []
    return view


def _turn_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "seq": int(row["seq"]),
        "status": str(row["status"]),
        "prompt": str(row["prompt"] or ""),
        "content": str(row["content"] or ""),
        "model": str(row["model"] or ""),
        "via": str(row["via"] or ""),
        "usage": json.loads(row["usage"] or "{}"),
        "error": str(row["error"] or ""),
        "errors": json.loads(row["errors"] or "[]"),
        "created_at": str(row["created_at"]),
        "completed_at": str(row["completed_at"]) if row["completed_at"] else None,
    }


def draft_fingerprint(definition: dict[str, Any]) -> str:
    """草稿指纹（规范化 JSON 的 sha256 前 16 位）：stale 判定与发布幂等都用它。"""

    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_cached(definition: Any) -> list[str]:
    return workflow_service.validate_definition(definition) if isinstance(definition, dict) and definition else []


# ---- 会话（owner + org/project 作用域） --------------------------------------


def create_session(store: Any, *, organization_id: str, member_id: str, title: str = "", project_id: str | None = None) -> dict[str, Any]:
    """新建会话：owner = member_id，org 隔离；绑项目时要求成员在项目里（复用 store 授权）。"""

    ensure_schema(store)
    organization_id, member_id = str(organization_id), str(member_id)
    if project_id:
        from uuid import UUID

        store.authorize_member(UUID(str(project_id)), member_id, "project.view")
    session_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO workflow_copilot_sessions (id, organization_id, project_id, member_id, title, status, draft, draft_revision, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'active', '{}', 0, ?, ?, ?)",
        (session_id, organization_id, str(project_id) if project_id else None, member_id, str(title or ""), member_id, timestamp, timestamp),
    )
    store.db.commit()
    row = store.db.execute("SELECT * FROM workflow_copilot_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session_view(row, with_draft=True)


def _own_session(store: Any, session_id: str, member_id: str) -> Any:
    """会话存在 + 归属校验（org 一致且 owner 才可读写；共享只读由 org 内列表看）。"""

    ensure_schema(store)
    row = store.db.execute("SELECT * FROM workflow_copilot_sessions WHERE id = ?", (str(session_id),)).fetchone()
    if row is None:
        raise WorkflowCopilotError("copilot_session_not_found")
    return row


def get_session(store: Any, session_id: str, member_id: str) -> dict[str, Any]:
    """会话详情（含草稿与轮次）；org 隔离：非本组织一律 404。"""

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    view = _session_view(row, with_draft=True)
    view["turns"] = list_turns(store, str(row["id"]), member_id)
    return view


def list_sessions(store: Any, member_id: str, *, project_id: str | None = None) -> list[dict[str, Any]]:
    """恢复会话列表：owner 本人 + 本组织；可选按项目过滤。"""

    ensure_schema(store)
    member = store.get_member(str(member_id))
    sql = "SELECT * FROM workflow_copilot_sessions WHERE organization_id = ? AND member_id = ?"
    params: list[Any] = [str(member.organization_id), str(member_id)]
    if project_id:
        sql += " AND project_id = ?"
        params.append(str(project_id))
    sql += " ORDER BY updated_at DESC LIMIT 50"
    return [_session_view(row) for row in store.db.execute(sql, params).fetchall()]


def list_turns(store: Any, session_id: str, member_id: str) -> list[dict[str, Any]]:
    """会话轮次（含每轮的 usage/via——计费口径如实展示）。"""

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    rows = store.db.execute(
        "SELECT * FROM workflow_copilot_turns WHERE session_id = ? ORDER BY seq", (str(row["id"]),)
    ).fetchall()
    return [_turn_view(item) for item in rows]


# ---- synapforge-choice 服务端校验 -------------------------------------------


def _parse_choice_block(content: str) -> dict[str, Any] | None:
    """从助手回复里取 synapforge-choice 围栏并做**封闭形状**校验；不合法 = 没有。"""

    match = _CHOICE_FENCE_RE.search(content or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not str(parsed.get("id") or "").strip() or not str(parsed.get("title") or "").strip():
        return None
    questions = parsed.get("questions")
    if not isinstance(questions, list) or not questions or len(questions) > 20:
        return None
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            return None
        qid = str(question.get("id") or "")
        if not qid or qid in seen or str(question.get("label") or "").strip() == "":
            return None
        if str(question.get("type") or "") not in _QUESTION_TYPES:
            return None
        seen.add(qid)
        options = question.get("options")
        if str(question["type"]) == "text":
            if options:
                return None
            question["options"] = []
            continue
        if not isinstance(options, list) or not options or len(options) > 12:
            return None
        values: set[str] = set()
        for option in options:
            if not isinstance(option, dict) or not str(option.get("value") or "").strip() or not str(option.get("label") or "").strip():
                return None
            if str(option["value"]) in values:
                return None
            values.add(str(option["value"]))
        question["options"] = [{"value": str(o["value"]), "label": str(o["label"]), "description": str(o.get("description") or "")} for o in options]
    return {"id": str(parsed["id"]), "title": str(parsed["title"]), "description": str(parsed.get("description") or ""), "questions": questions}


def _answer_questions(choice: dict[str, Any], answers: dict[str, Any]) -> tuple[bool, list[str]]:
    """结构化回答 vs 上一轮问题集合：未知问题/未知选项一律拒绝（机器可读，不猜）。"""

    errors: list[str] = []
    valid_ids = {str(q["id"]) for q in choice["questions"]}
    for key in answers:
        if str(key) not in valid_ids:
            errors.append(f"structured_answer: 未知问题 {key!r}（本轮没有问过）")
    for question in choice["questions"]:
        qid, qtype = str(question["id"]), str(question["type"])
        if qid not in answers:
            if question.get("required"):
                errors.append(f"structured_answer:{qid}: 必答未答")
            continue
        value = answers[qid]
        if qtype == "text":
            if not isinstance(value, str) or not value.strip():
                errors.append(f"structured_answer:{qid}: 文本回答必须是非空字符串")
            continue
        selected = value if isinstance(value, list) else [value]
        if not selected:
            if question.get("required"):
                errors.append(f"structured_answer:{qid}: 必答未答")
            continue
        allowed = {str(o["value"]) for o in question["options"]}
        for item in selected:
            if str(item) not in allowed:
                errors.append(f"structured_answer:{qid}: 未知选项 {item!r}")
    return not errors, errors


# ---- 草稿提取 / 自动修复 -----------------------------------------------------


def _extract_draft(content: str) -> dict[str, Any] | None:
    """从回复里提取**内部围栏**草稿；没有围栏或 JSON 不合法 = None（不接受自然语言草稿）。"""

    matches = _DRAFT_FENCE_RE.findall(content or "")
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _record_questions(store: Any, session_id: str, turn_id: str, organization_id: str, member_id: str, choice: dict[str, Any] | None) -> None:
    """把本轮问出的问题存到 decisions 表（占位 answers）——回答校验的对账来源。"""

    store.db.execute("DELETE FROM workflow_copilot_decisions WHERE turn_id = ?", (turn_id,))
    if choice is None:
        return
    store.db.execute(
        "INSERT INTO workflow_copilot_decisions (id, session_id, turn_id, organization_id, choice_id, answers, questions, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)",
        (
            str(uuid4()), str(session_id), str(turn_id), str(organization_id),
            str(choice["id"]), json.dumps(choice.get("questions") or [], ensure_ascii=False),
            str(member_id), _now(),
        ),
    )


def run_turn(
    store: Any,
    session_id: str,
    *,
    member_id: str,
    message: str,
    structured_answer: dict[str, Any] | None = None,
    model: str | None = None,
    choice_id: str | None = None,
) -> dict[str, Any]:
    """跑一轮：记录问题→（可选）校验结构化回答→调 LLM（chat_routed 计费）→
    提取围栏草稿→有界自动修复→落库。

    结构化回答只有通过服务端校验（choice 存在、问题对得上、选项对得上）才会：
    a) 落一条机器可读 decision；b) 作为确认上下文进下一轮对话。自由文本不进决策。
    """

    ensure_schema(store)
    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    organization_id = str(row["organization_id"])
    if organization_id != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")

    timestamp = _now()
    seq = int(store.db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM workflow_copilot_turns WHERE session_id = ?", (str(row["id"]),)
    ).fetchone()[0])
    turn_id = str(uuid4())
    store.db.execute(
        "INSERT INTO workflow_copilot_turns (id, session_id, organization_id, seq, status, prompt, created_by, created_at)"
        " VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
        (turn_id, str(row["id"]), organization_id, seq, str(message), str(member_id), timestamp),
    )
    store.db.commit()

    try:
        # ---- 上一轮未决问题：必须先答（结构化 + 服务端校验）才能继续 -----------
        pending = store.db.execute(
            "SELECT d.* FROM workflow_copilot_decisions d"
            " JOIN workflow_copilot_turns t ON t.id = d.turn_id"
            " WHERE d.session_id = ? AND d.answers = '{}' AND t.id != ? ORDER BY t.seq DESC LIMIT 1",
            (str(row["id"]), turn_id),
        ).fetchone()
        answered_context = ""
        if pending is not None and structured_answer:
            if choice_id is not None and str(choice_id) != str(pending["choice_id"]):
                raise WorkflowCopilotError("copilot_choice_invalid", ["structured_answer: choice_id 与上一轮问题不匹配"])
            questions = json.loads(pending["questions"] or "[]")
            choice = {"id": str(pending["choice_id"]), "title": "pending", "questions": questions}
            ok, errors = _answer_questions(choice, structured_answer)
            if not ok:
                raise WorkflowCopilotError("copilot_choice_invalid", errors)
            store.db.execute(
                "UPDATE workflow_copilot_decisions SET answers = ? WHERE id = ?",
                (json.dumps(structured_answer, ensure_ascii=False), str(pending["id"])),
            )
            answered_context = json.dumps({"choice_id": pending["choice_id"], "answers": structured_answer}, ensure_ascii=False)
            store.db.commit()
        elif pending is not None and not structured_answer:
            raise WorkflowCopilotError("copilot_choice_required", ["上一轮助手有未回答的选择问题，请先通过结构化回答提交"])

        # ---- 组装对话（系统提示 + 历史 + 本轮输入 + 结构化回答上下文）----------
        history = store.db.execute(
            "SELECT prompt, content, status FROM workflow_copilot_turns WHERE session_id = ? AND id != ? ORDER BY seq",
            (str(row["id"]), turn_id),
        ).fetchall()
        messages: list[dict[str, str]] = []
        for item in history:
            if item["prompt"]:
                messages.append({"role": "user", "content": str(item["prompt"])})
            if item["content"]:
                messages.append({"role": "assistant", "content": str(item["content"])})
        user_content = str(message)
        if answered_context:
            user_content += "\n\n[用户已通过结构化选择回答]\n" + answered_context
        messages.append({"role": "user", "content": user_content})

        settings = knowledge_base.get_ai_settings(store, str(member_id))
        current_draft = json.loads(row["draft"] or "{}") if row["draft"] and row["draft"] != "{}" else None
        repair_errors: list[str] = []

        # ---- 调用 + 有界自动修复 ------------------------------------------------
        content = ""
        via = ""
        usage: dict[str, Any] = {}
        used_model = str(model or "")
        final_errors: list[str] = []
        extracted: dict[str, Any] | None = None
        extracted_valid = False  # 只有**通过校验**的草稿才算数（修复耗尽 ≠ 接受半成品）
        attempts = 0
        while attempts <= MAX_AUTO_REPAIR_ROUNDS:
            attempts += 1
            call_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if current_draft is not None and repair_errors:
                call_messages.append({"role": "assistant", "content": "```workflow-draft\n" + json.dumps(current_draft, ensure_ascii=False) + "\n```"})
                call_messages.append({"role": "user", "content": "上面草稿未通过服务端校验，错误逐条如下。请输出修正后的完整草稿（仍用 ```workflow-draft 围栏）：" + "\n".join(repair_errors)})
            call_messages.extend(messages)
            try:
                outcome, via = ai_chat.chat_routed(
                    store,
                    member_id=str(member_id),
                    organization_id=organization_id,
                    messages=call_messages,
                    settings=settings,
                    model=model,
                    temperature=0.2,
                )
            except ai_chat.AiChatError as error:
                raise WorkflowCopilotError(error.code, [str(error)]) from error
            content = str(outcome.get("content") or "")
            usage = outcome.get("usage") or {}
            used_model = str(outcome.get("model") or used_model)
            extracted = _extract_draft(content)
            if extracted is None:
                # 没有合法围栏：如果模型在提问（choice 合法）就接受为"提问轮"，
                # 否则要求模型重出草稿（这也计入修复轮次，防止无限聊）
                choice = _parse_choice_block(content)
                if choice is not None:
                    break
                repair_errors = ["输出里没有找到 ```workflow-draft 围栏（或围栏内 JSON 不合法）。必须按系统提示重新输出完整草稿定义，或者用 ```synapforge-choice 围栏提出选择问题。"]
                continue
            errors_now = workflow_service.validate_definition(extracted)
            if not errors_now:
                repair_errors = []
                final_errors = []
                extracted_valid = True
                break
            repair_errors = errors_now
            final_errors = errors_now
            current_draft = extracted

        choice = _parse_choice_block(content)
        if extracted_valid:
            current_draft = extracted

        if not extracted_valid and choice is None:
            # 修复轮耗尽仍没有合法草稿/问题：如实失败（**不落**半成品草稿）
            store.db.execute(
                "UPDATE workflow_copilot_turns SET status = 'failed', content = ?, error = ?, errors = ?, completed_at = ? WHERE id = ?",
                (content[:8000], "copilot_draft_unparseable", json.dumps(final_errors or repair_errors or ["没有可用输出"], ensure_ascii=False), _now(), turn_id),
            )
            store.db.execute("UPDATE workflow_copilot_sessions SET updated_at = ? WHERE id = ?", (_now(), str(row["id"])))
            store.db.commit()
            raise WorkflowCopilotError("copilot_draft_unparseable", final_errors or ["模型输出既没有合法的 workflow-draft 围栏，也没有合法的 synapforge-choice 围栏"])

        # ---- 草稿落会话行（修订号 + 哈希推进）-----------------------------------
        if current_draft is not None:
            revision = int(row["draft_revision"]) + 1
            fingerprint = draft_fingerprint(current_draft)
            valid = 0 if workflow_service.validate_definition(current_draft) else 1
            store.db.execute(
                "UPDATE workflow_copilot_sessions SET draft = ?, draft_revision = ?, draft_hash = ?, draft_valid = ?, updated_at = ? WHERE id = ?",
                (json.dumps(current_draft, ensure_ascii=False), revision, fingerprint, valid, _now(), str(row["id"])),
            )
        _record_questions(store, str(row["id"]), turn_id, organization_id, str(member_id), choice)
        store.db.execute(
            "UPDATE workflow_copilot_turns SET status = 'completed', content = ?, model = ?, via = ?, usage = ?, completed_at = ? WHERE id = ?",
            (content[:8000], used_model, str(via), json.dumps(usage, ensure_ascii=False), _now(), turn_id),
        )
        store.db.commit()
    except WorkflowCopilotError:
        raise
    except Exception as error:  # noqa: BLE001 - 单轮失败如实记账，不让会话悬在 running
        store.db.execute(
            "UPDATE workflow_copilot_turns SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
            (str(error)[:500], _now(), turn_id),
        )
        store.db.commit()
        raise

    updated = store.db.execute("SELECT * FROM workflow_copilot_sessions WHERE id = ?", (str(row["id"]),)).fetchone()
    turn_row = store.db.execute("SELECT * FROM workflow_copilot_turns WHERE id = ?", (turn_id,)).fetchone()
    definition = json.loads(updated["draft"] or "{}") if updated["draft"] and updated["draft"] != "{}" else None
    return {
        "turn": _turn_view(turn_row),
        "session": _session_view(updated),
        "draft": definition,
        "draft_revision": int(updated["draft_revision"]),
        "draft_valid": bool(updated["draft_valid"]),
        "validation_errors": _validate_cached(definition) if definition else [],
    }


# ---- 草稿显式保存 / 预览 -----------------------------------------------------


def save_draft(store: Any, session_id: str, *, member_id: str, definition: dict[str, Any]) -> dict[str, Any]:
    """人工编辑草稿：显式动作，立即重校验并推进修订号（与 AI 轮次共用 stale 基线）。"""

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    if not isinstance(definition, dict) or not definition:
        raise WorkflowCopilotError("copilot_draft_invalid", ["definition: 必须是非空 JSON 对象"])
    errors = workflow_service.validate_definition(definition)
    revision = int(row["draft_revision"]) + 1
    store.db.execute(
        "UPDATE workflow_copilot_sessions SET draft = ?, draft_revision = ?, draft_hash = ?, draft_valid = ?, updated_at = ? WHERE id = ?",
        (json.dumps(definition, ensure_ascii=False), revision, draft_fingerprint(definition), 0 if errors else 1, _now(), str(row["id"])),
    )
    store.db.commit()
    updated = store.db.execute("SELECT * FROM workflow_copilot_sessions WHERE id = ?", (str(row["id"]),)).fetchone()
    return {"session": _session_view(updated, with_draft=True), "validation_errors": errors}


def preview_draft(store: Any, session_id: str, member_id: str) -> dict[str, Any]:
    """草稿试运行：复用 workflow_service.preview_definition，**零副作用**（不建任务/运行/事件）。"""

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    definition = json.loads(row["draft"] or "{}") if row["draft"] and row["draft"] != "{}" else None
    if not definition:
        raise WorkflowCopilotError("copilot_draft_empty", ["会话还没有草稿：先完成一轮对话或保存一版编辑"])
    preview = workflow_service.preview_definition(definition, {})
    return {
        "definition": definition,
        "draft_revision": int(row["draft_revision"]),
        "draft_hash": str(row["draft_hash"] or ""),
        "preview": preview,
    }


# ---- 发布（人工 confirmed + stale 检查 + 幂等） -------------------------------


def publish_draft(
    store: Any,
    session_id: str,
    *,
    member_id: str,
    confirmed: bool,
    draft_revision: int,
) -> dict[str, Any]:
    """把当前草稿发布为新工作流包（复用 workflow_service.create_workflow）。

    - confirmed != true → 拒绝（copilot_publish_not_confirmed）；
    - draft_revision != 会话当前修订 → 拒绝（copilot_draft_stale，防过期提交）；
    - 草稿未过校验 → 拒绝（copilot_draft_invalid，逐条错误）；
    - 已发布且 published_hash == 当前草稿哈希 → 幂等返回既有 workflow；
    - 会话可选绑定 project_id：绑了就要求发布者在该项目里（写权限）。
    """

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    if not confirmed:
        raise WorkflowCopilotError("copilot_publish_not_confirmed", ["发布必须由人工显式确认（confirmed=true）"])
    if int(draft_revision) != int(row["draft_revision"]):
        raise WorkflowCopilotError(
            "copilot_draft_stale",
            [f"提交修订 {draft_revision} 已过期（当前 {int(row['draft_revision'])}）：刷新草稿后重试"],
        )
    definition = json.loads(row["draft"] or "{}") if row["draft"] and row["draft"] != "{}" else None
    if not definition:
        raise WorkflowCopilotError("copilot_draft_empty", ["会话还没有草稿"])
    errors = workflow_service.validate_definition(definition)
    if errors:
        raise WorkflowCopilotError("copilot_draft_invalid", errors)
    fingerprint = draft_fingerprint(definition)
    if row["published_workflow_id"] and str(row["published_hash"] or "") == fingerprint:
        # 幂等：同一草稿重复发布返回既有包（不建第二个 key 冲突的包）
        existing = workflow_service.get_workflow(store, row["published_workflow_id"], str(row["organization_id"]))
        return {"workflow": existing, "session": _session_view(row), "idempotent": True}
    if row["project_id"]:
        store.authorize_member(row["project_id"], str(member_id), "project.write")

    try:
        workflow = workflow_service.create_workflow(store, str(row["organization_id"]), str(member_id), definition)
    except workflow_service.WorkflowError as error:
        # key 冲突等：如实透传（与既有工作流语义一致），不静默改 key
        raise WorkflowCopilotError(error.code, error.errors) from error
    store.db.execute(
        "UPDATE workflow_copilot_sessions SET published_workflow_id = ?, published_revision = ?, published_hash = ?, updated_at = ? WHERE id = ?",
        (str(workflow["id"]), int(row["draft_revision"]), fingerprint, _now(), str(row["id"])),
    )
    store.db.commit()
    updated = store.db.execute("SELECT * FROM workflow_copilot_sessions WHERE id = ?", (str(row["id"]),)).fetchone()
    return {"workflow": workflow, "session": _session_view(updated), "idempotent": False}


def run_published(
    store: Any,
    session_id: str,
    *,
    member_id: str,
    project_id: str,
    confirmed: bool,
    published_revision: int | None,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """把已发布版本应用到项目：**与发布分开的第二次显式确认** + 服务端项目权限检查。

    复用 workflow_service.start_workflow_run（物化的是任务骨架）；AI 永远到不了这里。
    """

    row = _own_session(store, session_id, member_id)
    member = store.get_member(str(member_id))
    if str(row["organization_id"]) != str(member.organization_id):
        raise WorkflowCopilotError("copilot_session_not_found")
    if not confirmed:
        raise WorkflowCopilotError("copilot_run_not_confirmed", ["运行必须由人工再次显式确认（confirmed=true）"])
    workflow_id = row["published_workflow_id"]
    if not workflow_id:
        raise WorkflowCopilotError("copilot_not_published", ["会话还没有已发布的版本：先走发布确认"])
    if published_revision is not None and int(published_revision) != int(row["published_revision"]):
        raise WorkflowCopilotError(
            "copilot_draft_stale",
            [f"提交的发布修订 {published_revision} 已过期（当前 {int(row['published_revision'])}）"],
        )
    # 项目权限：**服务端显式检查**（store.authorize_member），不依赖中间件路径推断
    store.authorize_member(project_id, str(member_id), "project.write")
    try:
        result = workflow_service.start_workflow_run(
            store, project_id, str(row["organization_id"]), str(member_id), workflow_id, inputs or {}
        )
    except workflow_service.WorkflowError as error:
        raise WorkflowCopilotError(error.code, error.errors) from error
    store.db.execute(
        "UPDATE workflow_copilot_sessions SET updated_at = ? WHERE id = ?", (_now(), str(row["id"]))
    )
    store.db.commit()
    return result


# ---- 渠道可见模型（/ask 同口径，供前端下拉） ----------------------------------


def available_models(store: Any, member_id: str) -> list[str]:
    try:
        member = store.get_member(str(member_id))
        return llm_channels.available_models(store, organization_id=str(member.organization_id))
    except Exception:  # noqa: BLE001 - 模型列表读不到不阻塞页面
        return []

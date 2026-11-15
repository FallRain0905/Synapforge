"""外部 Agent 会话发现与导入。

**为什么只读**：ZCode / Claude Code / Codex 的会话库属于别的应用，我们只读，
不写、不迁移、不锁库。ZCode 的库有 900MB+，所以一律 `mode=ro` 打开并设
`query_only`，避免任何一次误写把它改成我们不懂的状态。

**为什么是"上下文导入"而不是"续接"**：ZCode 是 Electron GUI，没有命令行入口
（`~/.zcode/cli` 只有数据目录，没有可执行文件），所以它无法像 Claude Code
那样用 `--resume` 续上同一个会话。导入的真实语义是：把那段会话的完整记录
读出来，建成一个平台 Agent，由本机**可用**的执行体带着这份上下文接着做。
界面上必须如实标注「上下文导入」，不能假装是 ZCode 本尊在跑。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_TURNS = 400          # 导入时最多保留多少轮
MAX_CHARS_PER_PART = 4000  # 单条消息截断，避免一条超长工具输出吃掉整个上下文
MAX_TOTAL_CHARS = 120_000  # 整份 transcript 上限

# 这些 part 是框架注入的噪声（环境说明、模型自述、工具路由提示），
# 导进来只会让新 Agent 以为自己还在那个运行时里。
_NOISE_PREFIXES = (
    "# Environment\n",
    "You are powered by the model named",
    "Available agent types for the Agent tool:",
    "The following skills are available for use with the Skill tool:",
)


def _zcode_db(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / ".zcode" / "cli" / "db" / "db.sqlite"


def _open_ro(path: Path) -> sqlite3.Connection:
    """只读打开。

    `mode=ro` 在 WAL 库上仍然需要一个能写 `-shm` 的目录；如果别的进程正持有它，
    打开可能失败。这里把 immutable 留空（ZCode 可能在写），失败就让调用方兜住。
    """

    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=4.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=1")
    except sqlite3.Error:
        pass
    return connection


def _stamp(ms: Any) -> str:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    # ZCode 存的是毫秒时间戳
    if value > 10_000_000_000:
        value = value // 1000
    return datetime.fromtimestamp(value, UTC).isoformat()


# 会话里出现这些东西的概率不低（真人口述密码、贴 key）。导入会把它们
# 原样写进工作区文件，所以先数一遍，导入结果里带出去让界面提示用户。
_SECRET_HINTS = (
    re.compile(r"(?i)(密码|口令|password|passwd)\s*[:：=]?\s*\S{4,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:：=]\s*\S{8,}"),
)


def scan_secrets(turns: list[dict[str, Any]]) -> int:
    """数一数这段会话里疑似凭据的地方（只计数，不改内容）。"""

    hits = 0
    for turn in turns:
        text = str(turn.get("text") or "")
        for pattern in _SECRET_HINTS:
            hits += len(pattern.findall(text))
    return hits


def zcode_available(home: Path | None = None) -> bool:
    return _zcode_db(home).exists()


def list_zcode_sessions(*, home: Path | None = None, query: str = "",
                        limit: int = 300) -> list[dict[str, Any]]:
    """列出 ZCode 的会话（标题 / 工作目录 / 消息数 / 更新时间）。

    查询按 `time_updated` 倒序，并用子查询数消息数——实测 279 个会话 0.04s，
    可以每次打开页面就扫一遍，不需要缓存。
    """

    path = _zcode_db(home)
    if not path.exists():
        return []
    needle = str(query or "").strip().lower()
    sql = """
        SELECT s.id, COALESCE(s.title, '') AS title,
               COALESCE(s.directory, '') AS directory,
               COALESCE(s.time_created, 0) AS created,
               COALESCE(s.time_updated, 0) AS updated,
               COALESCE(s.parent_id, '') AS parent_id,
               (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS messages
        FROM session s
        WHERE COALESCE(s.time_archived, 0) = 0
        {} 
        ORDER BY s.time_updated DESC
        LIMIT ?
    """.replace("{}", "AND (LOWER(COALESCE(s.title,'')) LIKE ? OR LOWER(COALESCE(s.directory,'')) LIKE ?)" if needle else "")
    params: list[Any] = []
    if needle:
        params += [f"%{needle}%", f"%{needle}%"]
    params.append(int(limit))

    try:
        connection = _open_ro(path)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row["id"])
        if not sid or sid.startswith("sess_subagent_"):
            # 子 agent 的会话是主会话的下属，混在列表里会让用户以为那是独立工作
            continue
        title = str(row["title"] or "").strip()
        out.append({
            "source": "zcode",
            "session_id": sid,
            "title": title or "(未命名会话)",
            "directory": str(row["directory"] or ""),
            "messages": int(row["messages"] or 0),
            "created_at": _stamp(row["created"]),
            "updated_at": _stamp(row["updated"]),
            "resumable": False,
            "note": "ZCode 没有命令行入口，导入的是会话上下文",
        })
    return out


def _part_text(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("type") != "text":
        return ""
    text = str(data.get("text") or "")
    if not text.strip():
        return ""
    # 合成消息（框架注入）不算用户真的说过的话
    if data.get("synthetic") or (data.get("metadata") or {}).get("visibility") == "model-only":
        return ""
    if any(text.startswith(prefix) for prefix in _NOISE_PREFIXES):
        return ""
    return text


def _message_role(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    role = str(data.get("role") or "")
    if role not in ("user", "assistant"):
        return ""
    origin = str((data.get("semantics") or {}).get("origin") or "")
    # `origin=real_user` 才是人打的；`tool`/`agent` 产生的 user 消息是回灌
    if role == "user" and origin and origin != "real_user":
        return ""
    return role


def zcode_transcript(*, session_id: str, home: Path | None = None,
                     max_turns: int = MAX_TURNS) -> list[dict[str, Any]]:
    """取一段会话的对话记录（只保留真人说的和助手回复的正文）。"""

    path = _zcode_db(home)
    if not path.exists() or not session_id:
        return []
    try:
        connection = _open_ro(path)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            """
            SELECT m.data AS message, p.data AS part, m.time_created AS created, m.sequence AS mseq
            FROM message m JOIN part p ON p.message_id = m.id
            WHERE m.session_id = ?
            ORDER BY m.time_created ASC, m.sequence ASC, p.sequence ASC
            """, (str(session_id),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()

    turns: list[dict[str, Any]] = []
    for row in rows:
        role = _message_role(str(row["message"] or ""))
        if not role:
            continue
        text = _part_text(str(row["part"] or ""))
        if not text:
            continue
        text = text[:MAX_CHARS_PER_PART]
        # 同一轮的多个 part 合并，别把一句话拆成好几条
        if turns and turns[-1]["role"] == role:
            turns[-1]["text"] += "\n" + text
        else:
            turns.append({"role": role, "text": text, "at": _stamp(row["created"])})
        if len(turns) >= max_turns:
            break

    total = 0
    trimmed: list[dict[str, Any]] = []
    for turn in turns:
        total += len(turn["text"])
        if total > MAX_TOTAL_CHARS:
            break
        trimmed.append(turn)
    return trimmed


def zcode_session_title(*, session_id: str, home: Path | None = None) -> str:
    path = _zcode_db(home)
    if not path.exists():
        return ""
    try:
        connection = _open_ro(path)
    except sqlite3.Error:
        return ""
    try:
        row = connection.execute("SELECT COALESCE(title,'') AS title FROM session WHERE id = ?",
                                 (str(session_id),)).fetchone()
        return str(row["title"] or "").strip() if row else ""
    except sqlite3.Error:
        return ""
    finally:
        connection.close()


def _guess_name(title: str, directory: str, session_id: str) -> str:
    """导入后的 Agent 名：优先用会话标题（用户认这个），并压到 60 字符内。"""

    base = str(title or "").strip() or Path(directory).name or session_id[:12]
    base = " ".join(base.split())
    return (base or "导入的 Agent")[:60]


def _transcript_markdown(*, title: str, session_id: str, directory: str,
                        updated_at: str, turns: list[dict[str, Any]]) -> str:
    lines = [
        f"# 导入的 ZCode 会话：{title}",
        "",
        f"- 来源：ZCode（会话 `{session_id}`）",
        f"- 原始工作目录：`{directory}`" if directory else "",
        f"- 最后更新：{updated_at}" if updated_at else "",
        f"- 导入的对话轮数：{len(turns)}",
        "",
        "> 这段记录是**只读导入**的上下文。ZCode 是桌面应用，没有命令行入口，",
        "> 所以无法续接原进程；下面的对话是它当时做过什么的原始记录。",
        "",
        "---",
        "",
    ]
    for turn in turns:
        label = "用户" if turn["role"] == "user" else "ZCode"
        lines.append(f"## {label}")
        lines.append("")
        lines.append(turn["text"].strip())
        lines.append("")
    return "\n".join(line for line in lines if line is not None)


def import_zcode_session(store: Any, *, session_id: str, home: Path | None = None,
                         name: str = "", workspace: str = "",
                         backend: str = "claude", provider_id: str = "",
                         model: str = "") -> dict[str, Any]:
    """把一段 ZCode 会话导入成一个平台 Agent。

    落地方式：会话正文写成工作区里的 `IMPORTED-SESSION.md`，角色卡里点明
    "先读它，再接着干"。这样后续每一次任务都不需要重新灌全文，也不会因为
    上下文被截断而丢失早期结论。
    """

    from . import agents as studio_agents

    if not str(session_id or "").strip():
        raise ValueError("session_id 必填")
    turns = zcode_transcript(session_id=str(session_id), home=home)
    if not turns:
        raise ValueError("这段会话没有可导入的对话内容（可能只有工具调用）")

    title = zcode_session_title(session_id=str(session_id), home=home)
    sessions = {item["session_id"]: item for item in
                list_zcode_sessions(home=home, limit=2000)}
    meta = sessions.get(str(session_id)) or {}
    directory = str(meta.get("directory") or "")
    updated_at = str(meta.get("updated_at") or "")
    label = str(name or "").strip() or _guess_name(title, directory, str(session_id))

    prompt_lines = [
        f"你在接手一段此前在 ZCode 里做的工作：「{title or label}」。",
        "",
        f"完整对话记录已放在工作区的 `IMPORTED-SESSION.md`（{len(turns)} 轮）。",
        "开工前先读它，弄清楚已经做到哪一步、当时为什么那么决定，再继续推进；",
        "不要重复已经完成的结论。",
    ]
    if directory:
        prompt_lines.append(f"原来的工作目录是 `{directory}`，需要参考历史文件时去那里看。")
    prompt_lines += [
        "",
        "注意：你不是 ZCode 进程本身，你是从这个上下文继续工作的平台 Agent。",
        "汇报时说清「哪些是之前就有的结论、哪些是你这轮新增的」。",
    ]

    created = studio_agents.create_agent(
        store, name=label, model=str(model or ""),
        system_prompt="\n".join(prompt_lines),
        workspace=str(workspace or ""), backend=str(backend or "claude"),
        provider_id=str(provider_id or ""))

    # 把 transcript 写进工作区（create_agent 已经建好目录）
    target = Path(created["workspace"]) / "IMPORTED-SESSION.md"
    target.write_text(_transcript_markdown(
        title=title or label, session_id=str(session_id), directory=directory,
        updated_at=updated_at, turns=turns), encoding="utf-8")

    return {
        "agent": created,
        "source": "zcode",
        "session_id": str(session_id),
        "turns": len(turns),
        "context_file": str(target),
        "resumable": False,
        "secret_hits": scan_secrets(turns),
    }

"""个人云盘领域服务（FM-1）：目录树、下载、改名/移动/复制、回收站、配额与对象清理。

为什么另起一个模块而不是继续加在 `personal_drive.py` 里：那一版是「一成员一堆平铺文件」的
最小实现（`personal_drive_files` 表、运行时建表、没有目录/回收站/下载端点）。文件管理要把它当
正经云盘用，就必须有树、有约束、有审计。这里把**新模型**做成唯一真源，`personal_drive.py`
退化成一层壳（旧接口的响应形状与文件 ID 保持不变，内部改走本模块）。

## 三条容易做错的地方，设计上都刻意选了保守那边

**1. 名字唯一（`name_key`）**：同成员、同父目录下不允许两个存活节点同名。判定用归一化键——
NFKC + 大小写折叠 + 连续空白折叠 + 去首尾空白。为什么按大小写折叠：Windows/macOS 的文件系统
大小写不敏感，`Data.csv` 与 `data.csv` 在用户机器上是同一个名字；平台这边若判成两个，导出落地时
才会撞车——那时已经太晚（计划 §5.1 要求同父 `name_key` 唯一）。

**2. 配额按"物理对象"算，不按节点算**：`used_bytes` 是**去重后**的字节数（同一 `storage_key`
只算一份），而**软删除的节点仍然计入**（回收站占着空间，计划 §5.1/§7.3）。彻底清除才释放。
这样"同 hash 上传两份同名不同名的文件"只收一份的费，而删除必须走"进回收站 → 彻底清除"两步。

**3. 对象删除**：先落库、后删对象。`purge` 先把节点标 `purged_at`、把 `storage_key` 写进
`drive_object_cleanup` 并提交，**再**去碰对象存储；删失败就留在队列里等重试。
反过来（先删对象再删库）会在中间态丢追踪记录——那是计划 §7.3 明令禁止的。

## 与旧接口的兼容

- 旧错误码（`drive_quota_exceeded` / `drive_file_not_found` / `drive_file_referenced_by_project`）
  继续由旧路由返回：本模块用计划 §6.5 的 `file_*` 码，旧路由做一次映射（`legacy_code()`）。
- 旧表 `personal_drive_files` **不删**，`backfill_legacy()` 把老行一次性、幂等地搬进节点树
  （同 id、同 storage_key、同 content_hash、同 created_at），搬完它就是只读备份。
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

# 与 personal_drive 保持同一个默认值（那份常量是"业务口径"，这里只读不复制）
DEFAULT_QUOTA_BYTES = 200 * 1024 * 1024
MAX_NAME_LENGTH = 240
MAX_DEPTH = 32
MAX_COPY_NODES = 500
ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"}
SORTS = {"name", "created", "updated", "size"}

# 计划 §6.5 的稳定错误码；旧接口要的 `drive_*` 码用 legacy_code() 映射
LEGACY_CODES = {
    "file_node_not_found": "drive_file_not_found",
    "file_quota_exceeded": "drive_quota_exceeded",
    "file_referenced_by_project": "drive_file_referenced_by_project",
}


class DriveError(RuntimeError):
    """稳定错误族：文件管理这条链路上的失败都能被调用方按 `code` 分流。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


def legacy_code(code: str) -> str:
    """新码 → 旧接口的历史码（旧客户端/旧测试不因这轮重构改口径）。"""

    return LEGACY_CODES.get(code, code)


@dataclass(frozen=True)
class DriveActor:
    """操作者上下文：**所有**权限判定都从它出发，不再依赖中间件。

    云盘是成员私有资产，服务层每一步都要回答"这个成员能不能动这个节点"。带上组织是为了
    跨组织也能挡住（同 id 的伪造请求、组织迁移后的历史数据）。
    """

    member_id: str
    organization_id: str


def actor_for(store: Any, member_id: str) -> DriveActor:
    """从成员 id 解析操作者上下文（查不到成员时**拒绝**，不编一个组织出来）。"""

    row = store.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (str(member_id),)).fetchone()
    if row is None:
        raise DriveError("drive_actor_unknown", str(member_id))
    return DriveActor(member_id=str(member_id), organization_id=str(row["organization_id"]))


# ---- 名字与路径 -------------------------------------------------------------


def name_key(name: str) -> str:
    """名字的归一化键：NFKC + 大小写折叠 + 空白折叠 + 去首尾空白。"""

    normalized = unicodedata.normalize("NFKC", str(name or ""))
    collapsed = " ".join(normalized.split())
    return collapsed.casefold()


def validate_name(name: str) -> str:
    """校验并返回清洗过的名字；不合法就**拒绝**（不静默改成另一个名字）。"""

    cleaned = str(name or "").strip()
    if not cleaned:
        raise DriveError("file_name_required")
    if cleaned in {".", ".."}:
        raise DriveError("file_name_invalid", "dot_segment")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise DriveError("file_name_invalid", "too_long")
    for character in ("/", "\\", "\x00"):
        if character in cleaned:
            raise DriveError("file_name_invalid", "path_separator")
    if any(ord(character) < 32 for character in cleaned):
        raise DriveError("file_name_invalid", "control_character")
    # Windows 保留名与尾随点/空格：平台这边先拦住，免得导出到用户机器上才炸
    stem = cleaned.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise DriveError("file_name_invalid", "reserved_name")
    if cleaned.endswith((".", " ")):
        raise DriveError("file_name_invalid", "trailing_dot_or_space")
    return cleaned


def _suffix_of(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_archive_name(name: str) -> bool:
    return _suffix_of(name) in ARCHIVE_SUFFIXES


# ---- 表结构（SQLite 侧；PostgreSQL 见 migrations/029_drive_nodes.sql） --------

_SQLITE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS drive_nodes (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        owner_member_id TEXT NOT NULL,
        parent_id TEXT REFERENCES drive_nodes(id),
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        storage_key TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT,
        mime_type TEXT,
        is_archive INTEGER NOT NULL DEFAULT 0,
        source_artifact_id TEXT,
        revision INTEGER NOT NULL DEFAULT 1,
        scan_status TEXT NOT NULL DEFAULT 'clean',
        is_root INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        trashed_with TEXT,
        purged_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS drive_nodes_sibling_name_idx
        ON drive_nodes(owner_member_id, parent_id, name_key)
        WHERE deleted_at IS NULL AND is_root = 0
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS drive_nodes_root_idx
        ON drive_nodes(owner_member_id) WHERE is_root = 1
    """,
    "CREATE INDEX IF NOT EXISTS drive_nodes_parent_idx ON drive_nodes(owner_member_id, parent_id, kind, name_key)",
    "CREATE INDEX IF NOT EXISTS drive_nodes_trash_idx ON drive_nodes(owner_member_id, trashed_with)",
    "CREATE INDEX IF NOT EXISTS drive_nodes_artifact_idx ON drive_nodes(source_artifact_id)",
    "CREATE INDEX IF NOT EXISTS drive_nodes_hash_idx ON drive_nodes(owner_member_id, content_hash)",
    """
    CREATE TABLE IF NOT EXISTS drive_project_refs (
        id TEXT PRIMARY KEY,
        drive_node_id TEXT NOT NULL REFERENCES drive_nodes(id),
        project_id TEXT NOT NULL,
        artifact_id TEXT,
        artifact_key TEXT NOT NULL DEFAULT '',
        imported_by TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        legacy_import INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS drive_project_refs_unique_idx ON drive_project_refs(drive_node_id, project_id, artifact_key)",
    "CREATE INDEX IF NOT EXISTS drive_project_refs_project_idx ON drive_project_refs(project_id)",
    """
    CREATE TABLE IF NOT EXISTS drive_object_cleanup (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        owner_member_id TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        enqueued_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS drive_object_cleanup_status_idx ON drive_object_cleanup(status, enqueued_at)",
    """
    CREATE TABLE IF NOT EXISTS drive_audit (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        node_id TEXT,
        parent_id TEXT,
        action TEXT NOT NULL,
        capability TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT 'allow',
        reason TEXT NOT NULL DEFAULT '',
        name_hash TEXT,
        content_hash TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        revision INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS drive_audit_node_idx ON drive_audit(node_id, created_at)",
    "CREATE INDEX IF NOT EXISTS drive_audit_member_idx ON drive_audit(member_id, created_at)",
)


def _schema_ready(store: Any) -> bool:
    """表在不在：一次 `sqlite_master` 查询（比每次跑十几条 DDL 便宜）。"""

    try:
        return store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'drive_nodes'").fetchone() is not None
    except Exception:  # noqa: BLE001 - 非 SQLite 后端（PG）由迁移建表，这里当"已就绪"
        return True


def ensure_schema(store: Any) -> None:
    """建表（幂等）。SQLite 侧在这里，PostgreSQL 侧在 `029_drive_nodes.sql`。

    每次调用先做一次存在性检查：测试与旧接口都会拿**新建的 Store** 直接调服务，
    不能假设"启动时建过表了"。
    """

    if _schema_ready(store):
        return
    for statement in _SQLITE_SCHEMA:
        store.db.execute(statement)
    store.db.commit()


# 进程内的写锁：**不能**靠 SQLite 自己。
#
# 踩过的坑（并发上传测试直接抓出来的）：Store 只有**一条**连接，`commit()`/`rollback()` 是全局的。
# 一个线程在事务里 `rollback()` 会把另一个线程**尚未提交**的插入一起丢掉——表现是"并发上传后
# 有的文件凭空消失、配额算少了"。uvicorn 单 worker 也会用线程池跑同步路由，所以这是真并发。
#
# 用一把进程内的可重入锁把"读配额 → 插入 → 提交"串起来：
#   * 配额判定与写入因此是原子的（先算后插不会被插队）；
#   * 事务边界的 commit/rollback 不会跨线程互相抵消。
# 代价是写操作串行（含对象写入那几毫秒~几十毫秒）。多进程/多实例要靠 PostgreSQL 侧的事务隔离
# 与 advisory lock，那是 FM-6 的事（见交接的偏差一节）。
_DRIVE_LOCK = threading.RLock()


@contextmanager
def _transaction(store: Any) -> Iterator[None]:
    """一个写事务：进程内互斥 + 出错回滚。"""

    with _DRIVE_LOCK:
        try:
            yield
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise


def _with_schema(function: Any) -> Any:
    """公开入口统一先保证表存在（测试与旧接口会拿新建的 Store 直接调服务）。"""

    @functools.wraps(function)
    def wrapper(store: Any, *args: Any, **kwargs: Any) -> Any:
        ensure_schema(store)
        return function(store, *args, **kwargs)

    return wrapper


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---- 节点读写 ---------------------------------------------------------------


def _field(row: Any, name: str, default: Any = None) -> Any:
    """取字段的统一写法：`sqlite3.Row` 没有 `.get()`（`dict` 有），两种都得能吃。"""

    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _row_to_node(row: Any) -> dict[str, Any]:
    """库行 → 对外节点（**不**含 storage_key：那是内部键，没必要发给浏览器）。"""

    value = dict(row)
    is_root = bool(value.get("is_root"))
    return {
        "id": str(value["id"]),
        "parent_id": str(value["parent_id"]) if value.get("parent_id") else None,
        "kind": str(value["kind"]),
        # 根目录在库里没有名字（它不是一个"文件"）；对外给个可显示的称呼，免得面包屑是空的
        "name": str(value["name"]) or ("个人云盘" if is_root else ""),
        "size_bytes": int(value.get("size_bytes") or 0),
        "content_hash": value.get("content_hash"),
        "mime_type": value.get("mime_type"),
        "is_archive": bool(value.get("is_archive")),
        "source_artifact_id": value.get("source_artifact_id"),
        "revision": int(value.get("revision") or 1),
        "scan_status": str(value.get("scan_status") or "clean"),
        "is_root": bool(value.get("is_root")),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "deleted_at": value.get("deleted_at"),
        "trashed_with": value.get("trashed_with"),
        "purged_at": value.get("purged_at"),
    }


def _raw(store: Any, node_id: str) -> Any:
    return store.db.execute("SELECT * FROM drive_nodes WHERE id = ?", (str(node_id),)).fetchone()


def _require_node(store: Any, actor: DriveActor, node_id: str, *, include_deleted: bool = False) -> Any:
    """取节点并做归属校验：别人的、别的组织的、已清除的一律当**不存在**（404 口径）。"""

    row = _raw(store, node_id)
    if row is None or str(row["owner_member_id"]) != actor.member_id:
        raise DriveError("file_node_not_found")
    if str(row["organization_id"]) != actor.organization_id:
        # 组织不一致：不区分"别人的组织"，一律按不存在处理（避免探测出别组织的节点 id 是否存在）
        raise DriveError("file_node_not_found")
    if str(_field(row, "scan_status", "")) == "purged" or _field(row, "purged_at"):
        raise DriveError("file_node_not_found")
    if _field(row, "deleted_at") and not include_deleted:
        raise DriveError("file_node_not_found")
    return row


@_with_schema
def root_node(store: Any, actor: DriveActor) -> dict[str, Any]:
    """成员根目录：没有就建（每个成员**只有一个**，靠部分唯一索引兜底）。"""

    row = store.db.execute(
        "SELECT * FROM drive_nodes WHERE owner_member_id = ? AND is_root = 1",
        (actor.member_id,),
    ).fetchone()
    if row is not None:
        return _row_to_node(row)
    node_id = str(uuid4())
    timestamp = _now()
    # 选择与插入都放在事务里（= 拿到写锁）：不然两个线程会同时判"没有根"，
    # 第二个插入撞上"每成员一个根"的唯一索引（并发上传测试就是这么抓到的）。
    with _transaction(store):
        existing = store.db.execute(
            "SELECT * FROM drive_nodes WHERE owner_member_id = ? AND is_root = 1", (actor.member_id,)
        ).fetchone()
        if existing is not None:
            return _row_to_node(existing)
        store.db.execute(
            """
            INSERT INTO drive_nodes
                (id, organization_id, owner_member_id, parent_id, kind, name, name_key, storage_key,
                 size_bytes, content_hash, mime_type, is_archive, source_artifact_id, revision,
                 scan_status, is_root, created_at, updated_at)
            VALUES (?, ?, ?, NULL, 'directory', '', '', NULL, 0, NULL, NULL, 0, NULL, 1, 'clean', 1, ?, ?)
            """,
            (node_id, actor.organization_id, actor.member_id, timestamp, timestamp),
        )
    return _row_to_node(_raw(store, node_id))


def resolve_parent(store: Any, actor: DriveActor, parent_id: str | None) -> Any:
    """父目录：None/空 → 根；必须是当前成员的**存活目录**。"""

    if not parent_id:
        return _raw(store, root_node(store, actor)["id"])
    row = _require_node(store, actor, parent_id)
    if str(row["kind"]) != "directory":
        raise DriveError("file_parent_invalid", "not_a_directory")
    return row


@_with_schema
def get_node(store: Any, actor: DriveActor, node_id: str) -> dict[str, Any]:
    return _row_to_node(_require_node(store, actor, node_id))


@_with_schema
def breadcrumb(store: Any, actor: DriveActor, node_id: str | None = None) -> list[dict[str, Any]]:
    """从根到该节点的路径（含根）。面包屑用它；也用来做深度与环的判定。"""

    chain: list[dict[str, Any]] = []
    current = str(node_id) if node_id else root_node(store, actor)["id"]
    seen: set[str] = set()
    while current:
        if current in seen:  # 数据损坏时的兜底：不进入死循环
            break
        seen.add(current)
        row = _require_node(store, actor, current)
        # 走 `_row_to_node` 而不是手搓字典：根目录的显示名这种口径只该有一处
        chain.append(_row_to_node(row))
        if row["is_root"]:
            break
        current = str(row["parent_id"]) if row["parent_id"] else ""
    return list(reversed(chain))


def _depth_of(store: Any, actor: DriveActor, node_id: str) -> int:
    return len(breadcrumb(store, actor, node_id))


def _sort_clause(sort: str) -> str:
    if sort not in SORTS:
        raise DriveError("file_sort_invalid", sort)
    if sort == "created":
        return "created_at DESC, name_key ASC"
    if sort == "updated":
        return "updated_at DESC, name_key ASC"
    if sort == "size":
        return "size_bytes DESC, name_key ASC"
    return "kind DESC, name_key ASC"


def _encode_cursor(node: dict[str, Any], sort: str = "name") -> str:
    payload = {"s": sort, "k": node.get("name"), "id": node["id"]}
    return base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    padded = str(cursor).replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        payload = json.loads(base64.b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - 游标坏了就从头开始，不报 500
        raise DriveError("file_cursor_invalid") from error
    return payload if isinstance(payload, dict) else None


@_with_schema
def list_children(
    store: Any,
    actor: DriveActor,
    parent_id: str | None = None,
    *,
    query: str = "",
    sort: str = "name",
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """列一个目录（分页）。根目录用 `parent_id=None` 表达，不要求调用方知道根 id。"""

    parent = resolve_parent(store, actor, parent_id)
    size = max(1, min(int(limit or 200), 500))
    clauses = ["owner_member_id = ?", "parent_id = ?", "deleted_at IS NULL", "purged_at IS NULL"]
    params: list[Any] = [actor.member_id, str(parent["id"])]
    text = str(query or "").strip()
    if text:
        clauses.append("name_key LIKE ?")
        params.append(f"%{name_key(text)}%")
    order = _sort_clause(sort)
    clause_sql = " AND ".join(clauses)
    total = store.db.execute(f"SELECT COUNT(*) AS c FROM drive_nodes WHERE {clause_sql}", tuple(params)).fetchone()["c"]
    page_params = list(params)
    if cursor:
        decoded = _decode_cursor(cursor)
        # 游标只对 name 排序成立（键是 name_key）：排序字段变了就从头发一页，
        # 宁可让调用方自己发现"页码重置"，也不要按错的键分页导致重复/漏项
        if decoded and str(decoded.get("s") or "name") != sort:
            raise DriveError("file_cursor_invalid", f"sort_changed:{sort}")
        if decoded:
            clauses.append("(name_key, id) > (?, ?)")
            page_params.extend([name_key(str(decoded.get("k") or "")), str(decoded.get("id") or "")])
            clause_sql = " AND ".join(clauses)
    rows = store.db.execute(
        f"SELECT * FROM drive_nodes WHERE {clause_sql} ORDER BY {order}, id ASC LIMIT ?",
        (*page_params, size + 1),
    ).fetchall()
    has_more = len(rows) > size
    nodes = [_row_to_node(row) for row in rows[:size]]
    return {
        "parent": _row_to_node(parent),
        "nodes": nodes,
        "total": int(total),
        "next_cursor": _encode_cursor(nodes[-1], sort) if has_more and nodes else None,
        "truncated": bool(has_more),
    }


# ---- 审计 -------------------------------------------------------------------


def audit(
    store: Any,
    actor: DriveActor,
    action: str,
    *,
    node: Any | None = None,
    capability: str = "",
    decision: str = "allow",
    reason: str = "",
    size_bytes: int = 0,
    content_hash: str | None = None,
) -> None:
    """写一条云盘审计。**只记哈希不记名字**：文件名本身可能是敏感信息（与计划 §5.9 一致）。"""

    node_id = None
    parent_id = None
    name_hash = None
    revision = None
    if node is not None:
        node_id = str(node["id"])
        parent_id = str(node["parent_id"]) if node["parent_id"] else None
        name_hash = hashlib.sha256(str(node["name"]).encode("utf-8")).hexdigest()[:32] if node["name"] else None
        revision = int(node["revision"]) if node["revision"] is not None else None
    store.db.execute(
        """
        INSERT INTO drive_audit
            (id, organization_id, member_id, node_id, parent_id, action, capability, decision, reason,
             name_hash, content_hash, size_bytes, revision, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            actor.organization_id,
            actor.member_id,
            node_id,
            parent_id,
            action,
            capability,
            decision,
            reason,
            name_hash,
            content_hash,
            int(size_bytes),
            revision,
            _now(),
        ),
    )


@_with_schema
def node_audit(store: Any, node_id: str, organization_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """某个节点的**全部**审计（含 Agent 读取）：按节点 + 组织过滤，不按成员。

    为什么不能只看自己的行：Agent 读的是成员的私有文件——"谁在什么时候通过哪个 Agent 读了我的文件"
    恰恰是成员最需要看到的那部分，按 member_id 过滤会把它整段藏掉。
    """

    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM drive_audit WHERE node_id = ? AND organization_id = ? ORDER BY created_at DESC LIMIT ?",
        (str(node_id), str(organization_id), max(1, min(int(limit or 100), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


@_with_schema
def audit_log(store: Any, actor: DriveActor, node_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["organization_id = ?", "member_id = ?"]
    params: list[Any] = [actor.organization_id, actor.member_id]
    if node_id:
        clauses.append("node_id = ?")
        params.append(str(node_id))
    rows = store.db.execute(
        f"SELECT * FROM drive_audit WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


# ---- 配额 -------------------------------------------------------------------


def quota_bytes(store: Any = None) -> int:
    """配额上限：环境变量 > `personal_drive.DRIVE_QUOTA_BYTES` > 默认。

    动态读 `personal_drive` 的那份常量（而不是 `from ... import`）：测试会 monkeypatch 它，
    复制一份就会让"改配额再上传"的用例失去意义。
    """

    override = os.environ.get("PLATFORM_DRIVE_QUOTA_BYTES")
    if override and override.strip().isdigit():
        return int(override.strip())
    try:
        from . import personal_drive

        return int(getattr(personal_drive, "DRIVE_QUOTA_BYTES", DEFAULT_QUOTA_BYTES))
    except Exception:  # noqa: BLE001 - 独立跑（脚本/测试）时退回默认
        return DEFAULT_QUOTA_BYTES


@_with_schema
def usage(store: Any, actor: DriveActor) -> dict[str, Any]:
    """用量：`used_bytes` 是**去重后**的物理占用（同 storage_key 只算一份），软删除仍计入。"""

    row = store.db.execute(
        """
        SELECT COALESCE(SUM(size), 0) AS bytes FROM (
            SELECT storage_key, MAX(size_bytes) AS size
            FROM drive_nodes
            WHERE owner_member_id = ? AND storage_key IS NOT NULL AND purged_at IS NULL
            GROUP BY storage_key
        )
        """,
        (actor.member_id,),
    ).fetchone()
    used = int(row["bytes"] or 0)
    live = store.db.execute(
        "SELECT COUNT(*) AS c FROM drive_nodes WHERE owner_member_id = ? AND kind = 'file' AND deleted_at IS NULL AND purged_at IS NULL",
        (actor.member_id,),
    ).fetchone()["c"]
    trashed = store.db.execute(
        "SELECT COUNT(*) AS c FROM drive_nodes WHERE owner_member_id = ? AND deleted_at IS NOT NULL AND purged_at IS NULL AND kind = 'file'",
        (actor.member_id,),
    ).fetchone()["c"]
    quota = quota_bytes(store)
    return {
        "owner": actor.member_id,
        "quota_bytes": quota,
        "used_bytes": used,
        "free_bytes": max(0, quota - used),
        "file_count": int(live),
        "trashed_count": int(trashed),
        "used_percent": round(used / quota * 100, 2) if quota else 0.0,
    }


def _assert_quota(store: Any, actor: DriveActor, extra_bytes: int) -> None:
    """在**同一事务内**判定配额（先算后插，避免并发超卖）。"""

    current = usage(store, actor)
    if current["used_bytes"] + max(0, int(extra_bytes)) > current["quota_bytes"]:
        raise DriveError("file_quota_exceeded", f"{current['used_bytes'] + extra_bytes}/{current['quota_bytes']}")


# ---- 写操作 -----------------------------------------------------------------


def _sibling_conflict(store: Any, actor: DriveActor, parent_id: str, key: str, *, exclude_id: str | None = None) -> Any:
    clauses = ["owner_member_id = ?", "parent_id = ?", "name_key = ?", "deleted_at IS NULL", "purged_at IS NULL"]
    params: list[Any] = [actor.member_id, str(parent_id), key]
    if exclude_id:
        clauses.append("id <> ?")
        params.append(str(exclude_id))
    return store.db.execute(f"SELECT * FROM drive_nodes WHERE {' AND '.join(clauses)}", tuple(params)).fetchone()


@_with_schema
def create_directory(store: Any, actor: DriveActor, parent_id: str | None, name: str) -> dict[str, Any]:
    cleaned = validate_name(name)
    parent = resolve_parent(store, actor, parent_id)
    key = name_key(cleaned)
    if _sibling_conflict(store, actor, str(parent["id"]), key):
        raise DriveError("file_name_conflict", cleaned)
    depth = _depth_of(store, actor, str(parent["id"])) + 1
    if depth > MAX_DEPTH:
        raise DriveError("file_directory_too_deep", str(depth))
    node_id = str(uuid4())
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            """
            INSERT INTO drive_nodes
                (id, organization_id, owner_member_id, parent_id, kind, name, name_key, storage_key,
                 size_bytes, content_hash, mime_type, is_archive, source_artifact_id, revision,
                 scan_status, is_root, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'directory', ?, ?, NULL, 0, NULL, NULL, 0, NULL, 1, 'clean', 0, ?, ?)
            """,
            (node_id, actor.organization_id, actor.member_id, str(parent["id"]), cleaned, key, timestamp, timestamp),
        )
        audit(store, actor, "create_directory", node=_raw(store, node_id), capability="drive.file.write")
    return _row_to_node(_raw(store, node_id))


def _find_reusable_object(store: Any, actor: DriveActor, content_hash: str) -> Any:
    """同成员已有同内容对象 → 复用它的 storage_key（**不重复占空间、不重复计费**）。"""

    return store.db.execute(
        """
        SELECT storage_key, size_bytes FROM drive_nodes
        WHERE owner_member_id = ? AND content_hash = ? AND storage_key IS NOT NULL AND purged_at IS NULL
        ORDER BY created_at ASC LIMIT 1
        """,
        (actor.member_id, content_hash),
    ).fetchone()


@_with_schema
def put_file(
    store: Any,
    actor: DriveActor,
    parent_id: str | None,
    name: str,
    content: bytes,
    mime_type: str | None = None,
    *,
    source_artifact_id: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    """上传/落盘一个文件。

    - 同目录同名 → `file_name_conflict`（409），**不覆盖**（计划 §6.5 与安全要求第 7 条）；
    - 同内容 → 复用对象（配额只算一份）；
    - 配额在事务内判定；插入失败会回滚，并把刚写进对象存储的那份删掉（不留孤儿）。
    """

    ensure_schema(store)
    cleaned = validate_name(name)
    if not content:
        raise DriveError("file_empty")
    content_hash = hashlib.sha256(content).hexdigest()
    if expected_hash and str(expected_hash) != content_hash:
        raise DriveError("file_upload_hash_mismatch", f"{expected_hash}!={content_hash}")
    parent = resolve_parent(store, actor, parent_id)
    key = name_key(cleaned)
    if _sibling_conflict(store, actor, str(parent["id"]), key):
        raise DriveError("file_name_conflict", cleaned)

    node_id = str(uuid4())
    timestamp = _now()
    written_key: str | None = None
    try:
        with _transaction(store):
            # 去重查询必须在事务里（= 在写锁内）：放在锁外的话，四个并发上传会同时判"没有可复用的
            # 对象"，各写一份 → **同一份内容被计了四次费**（FM-1 验收脚本就是这么抓到的）。
            reusable = _find_reusable_object(store, actor, content_hash)
            if reusable is not None:
                storage_key = str(reusable["storage_key"])
            else:
                _assert_quota(store, actor, len(content))
                storage_key = f"drive/{actor.member_id}/{node_id}"
                stored = store.object_store.put_bytes(storage_key, content, mime_type)
                storage_key = str(stored.key)
                written_key = storage_key
            store.db.execute(
                """
                INSERT INTO drive_nodes
                    (id, organization_id, owner_member_id, parent_id, kind, name, name_key, storage_key,
                     size_bytes, content_hash, mime_type, is_archive, source_artifact_id, revision,
                     scan_status, is_root, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, 1, 'clean', 0, ?, ?)
                """,
                (
                    node_id,
                    actor.organization_id,
                    actor.member_id,
                    str(parent["id"]),
                    cleaned,
                    key,
                    storage_key,
                    len(content),
                    content_hash,
                    mime_type,
                    int(is_archive_name(cleaned)),
                    str(source_artifact_id) if source_artifact_id else None,
                    timestamp,
                    timestamp,
                ),
            )
            audit(
                store,
                actor,
                "upload",
                node=_raw(store, node_id),
                capability="drive.file.upload",
                size_bytes=len(content),
                content_hash=content_hash,
            )
    except Exception:
        # 事务回滚后节点没落库，但对象可能已经写进去了：删掉它，不让存储里留下无主对象
        if written_key:
            try:
                store.object_store.delete(written_key)
            except Exception:  # noqa: BLE001 - 删不掉也不能掩盖原始失败
                pass
        raise
    return _row_to_node(_raw(store, node_id))


@_with_schema
def read_content(store: Any, actor: DriveActor, node_id: str) -> tuple[dict[str, Any], bytes]:
    """下载/读正文。**每次读都校验 hash**：对象存储被改坏了要当场发现，不能把错内容发出去。"""

    row = _require_node(store, actor, node_id, include_deleted=True)
    if str(row["kind"]) != "file" or not row["storage_key"]:
        raise DriveError("file_node_not_found", "not_a_file")
    content = store.object_store.get_bytes(str(row["storage_key"]))
    if row["content_hash"]:
        actual = hashlib.sha256(content).hexdigest()
        if actual != str(row["content_hash"]):
            audit(store, actor, "download", node=row, capability="drive.file.read", decision="deny", reason="hash_mismatch")
            store.db.commit()
            raise DriveError("file_content_hash_mismatch", str(row["content_hash"]))
    audit(store, actor, "download", node=row, capability="drive.file.read", size_bytes=len(content))
    store.db.commit()
    return _row_to_node(row), content


def _assert_revision(row: Any, expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    if int(row["revision"]) != int(expected_revision):
        raise DriveError("file_revision_conflict", f"{row['revision']}!={expected_revision}")


@_with_schema
def rename(store: Any, actor: DriveActor, node_id: str, name: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    row = _require_node(store, actor, node_id)
    if row["is_root"]:
        raise DriveError("file_root_immutable")
    cleaned = validate_name(name)
    key = name_key(cleaned)
    _assert_revision(row, expected_revision)
    if str(row["name_key"]) == key:
        return _row_to_node(row)
    if _sibling_conflict(store, actor, str(row["parent_id"]), key, exclude_id=str(row["id"])):
        raise DriveError("file_name_conflict", cleaned)
    with _transaction(store):
        store.db.execute(
            "UPDATE drive_nodes SET name = ?, name_key = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
            (cleaned, key, _now(), str(row["id"])),
        )
        audit(store, actor, "rename", node=_raw(store, node_id), capability="drive.file.rename")
    return _row_to_node(_raw(store, node_id))


@_with_schema
def move(store: Any, actor: DriveActor, node_id: str, parent_id: str | None, *, expected_revision: int | None = None) -> dict[str, Any]:
    """移动节点。**先挡环**：不能把目录移进自己的子孙里（否则树就断了）。"""

    row = _require_node(store, actor, node_id)
    if row["is_root"]:
        raise DriveError("file_root_immutable")
    target = resolve_parent(store, actor, parent_id)
    _assert_revision(row, expected_revision)
    if str(target["id"]) == str(row["parent_id"]):
        return _row_to_node(row)
    if str(row["kind"]) == "directory":
        # 目标在不在自己的子树里：从目标往上走，遇到自己就是环
        cursor = str(target["id"])
        seen: set[str] = set()
        while cursor and cursor not in seen:
            if cursor == str(row["id"]):
                raise DriveError("file_directory_cycle", str(target["id"]))
            seen.add(cursor)
            parent_row = _raw(store, cursor)
            cursor = str(parent_row["parent_id"]) if parent_row and parent_row["parent_id"] else ""
    if _sibling_conflict(store, actor, str(target["id"]), str(row["name_key"]), exclude_id=str(row["id"])):
        raise DriveError("file_name_conflict", str(row["name"]))
    if _depth_of(store, actor, str(target["id"])) + 1 > MAX_DEPTH:
        raise DriveError("file_directory_too_deep")
    with _transaction(store):
        store.db.execute(
            "UPDATE drive_nodes SET parent_id = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
            (str(target["id"]), _now(), str(row["id"])),
        )
        audit(store, actor, "move", node=_raw(store, node_id), capability="drive.file.move")
    return _row_to_node(_raw(store, node_id))


@_with_schema
def copy(
    store: Any,
    actor: DriveActor,
    node_id: str,
    parent_id: str | None = None,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """复制节点（文件复制一条记录、目录递归复制整棵子树）。

    默认**保留两份**：目标同名时自动加 `-副本`/`-副本2` 后缀而不是覆盖或报错（计划 §8.2）。
    递归复制有节点数上限：一次点错不该把云盘复制爆。
    """

    row = _require_node(store, actor, node_id)
    if row["is_root"]:
        raise DriveError("file_root_immutable")
    target = resolve_parent(store, actor, parent_id or str(row["parent_id"]))

    def unique_name(parent: str, desired: str) -> str:
        candidate = desired
        counter = 0
        while _sibling_conflict(store, actor, parent, name_key(candidate)):
            stem, dot, suffix = desired.rpartition(".")
            label = "-副本" if counter == 0 else f"-副本{counter + 1}"
            candidate = f"{stem}{label}.{suffix}" if dot else f"{desired}{label}"
            counter += 1
            if counter > 50:
                raise DriveError("file_name_conflict", desired)
        return candidate

    def clone(source_row: Any, parent: str, desired: str, budget: list[int]) -> str:
        budget[0] -= 1
        if budget[0] < 0:
            raise DriveError("file_copy_too_large", str(MAX_COPY_NODES))
        new_id = str(uuid4())
        timestamp = _now()
        store.db.execute(
            """
            INSERT INTO drive_nodes
                (id, organization_id, owner_member_id, parent_id, kind, name, name_key, storage_key,
                 size_bytes, content_hash, mime_type, is_archive, source_artifact_id, revision,
                 scan_status, is_root, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)
            """,
            (
                new_id,
                actor.organization_id,
                actor.member_id,
                parent,
                str(source_row["kind"]),
                desired,
                name_key(desired),
                source_row["storage_key"],
                int(source_row["size_bytes"] or 0),
                source_row["content_hash"],
                source_row["mime_type"],
                int(source_row["is_archive"] or 0),
                source_row["source_artifact_id"],
                str(source_row["scan_status"] or "clean"),
                timestamp,
                timestamp,
            ),
        )
        if str(source_row["kind"]) == "directory":
            children = store.db.execute(
                "SELECT * FROM drive_nodes WHERE parent_id = ? AND deleted_at IS NULL AND purged_at IS NULL ORDER BY name_key ASC",
                (str(source_row["id"]),),
            ).fetchall()
            for child in children:
                clone(child, new_id, str(child["name"]), budget)
        return new_id

    with _transaction(store):
        if _depth_of(store, actor, str(target["id"])) + 1 > MAX_DEPTH:
            raise DriveError("file_directory_too_deep")
        desired = validate_name(name) if name else str(row["name"])
        final_name = unique_name(str(target["id"]), desired)
        new_id = clone(row, str(target["id"]), final_name, [MAX_COPY_NODES])
        audit(store, actor, "copy", node=_raw(store, new_id), capability="drive.file.copy", reason=str(row["id"]))
    return _row_to_node(_raw(store, new_id))


# ---- 回收站 -----------------------------------------------------------------


@_with_schema
def trash(store: Any, actor: DriveActor, node_id: str) -> dict[str, Any]:
    """软删除：整棵子树打上同一个 `trashed_with`，回收站只列"这次删的根"。"""

    row = _require_node(store, actor, node_id)
    if row["is_root"]:
        raise DriveError("file_root_immutable")
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            "UPDATE drive_nodes SET deleted_at = ?, trashed_with = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (timestamp, str(row["id"]), timestamp, str(row["id"])),
        )
        if str(row["kind"]) == "directory":
            _mark_subtree(store, str(row["id"]), timestamp, str(row["id"]))
        audit(store, actor, "trash", node=_raw(store, node_id), capability="drive.file.delete")
    return {"id": str(row["id"]), "deleted_at": timestamp, "trashed_count": _subtree_size(store, str(row["id"]))}


def _mark_subtree(store: Any, node_id: str, timestamp: str, trashed_with: str) -> None:
    # 调用方已经在 `_transaction` 里（拿到 _DRIVE_LOCK），这里只用同一把可重入锁兜底
    with _DRIVE_LOCK:
        _mark_subtree_locked(store, node_id, timestamp, trashed_with)


def _mark_subtree_locked(store: Any, node_id: str, timestamp: str, trashed_with: str) -> None:
    stack = [str(node_id)]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        children = store.db.execute(
            "SELECT id FROM drive_nodes WHERE parent_id = ? AND deleted_at IS NULL", (current,)
        ).fetchall()
        for child in children:
            store.db.execute(
                "UPDATE drive_nodes SET deleted_at = ?, trashed_with = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
                (timestamp, trashed_with, timestamp, str(child["id"])),
            )
            stack.append(str(child["id"]))


def _subtree_size(store: Any, node_id: str) -> int:
    rows = store.db.execute("SELECT COUNT(*) AS c FROM drive_nodes WHERE trashed_with = ?", (str(node_id),)).fetchone()
    return int(rows["c"])


@_with_schema
def trash_list(store: Any, actor: DriveActor) -> list[dict[str, Any]]:
    rows = store.db.execute(
        """
        SELECT * FROM drive_nodes
        WHERE owner_member_id = ? AND trashed_with = id AND purged_at IS NULL
        ORDER BY deleted_at DESC
        """,
        (actor.member_id,),
    ).fetchall()
    result = []
    for row in rows:
        node = _row_to_node(row)
        node["trashed_count"] = _subtree_size(store, str(row["id"]))
        result.append(node)
    return result


@_with_schema
def restore(store: Any, actor: DriveActor, node_id: str) -> dict[str, Any]:
    """从回收站恢复。名字被占用时不覆盖：明确报冲突（用户可先改名或删掉新的那个）。"""

    row = _require_node(store, actor, node_id, include_deleted=True)
    if not _field(row, "deleted_at"):
        raise DriveError("file_not_trashed")
    if not row["is_root"] and _sibling_conflict(store, actor, str(row["parent_id"]), str(row["name_key"]), exclude_id=str(row["id"])):
        raise DriveError("file_name_conflict", str(row["name"]))
    with _transaction(store):
        store.db.execute(
            """
            UPDATE drive_nodes SET deleted_at = NULL, trashed_with = NULL, updated_at = ?, revision = revision + 1
            WHERE trashed_with = ?
            """,
            (_now(), str(row["id"])),
        )
        audit(store, actor, "restore", node=_raw(store, node_id), capability="drive.file.restore")
    return _row_to_node(_raw(store, node_id))


@_with_schema
def refs_for(store: Any, node_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM drive_project_refs WHERE drive_node_id = ? ORDER BY imported_at ASC", (str(node_id),)
    ).fetchall()
    return [dict(row) for row in rows]


def _refs_in_subtree(store: Any, node_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM drive_nodes WHERE id = ?
            UNION ALL
            SELECT n.id FROM drive_nodes n JOIN subtree s ON n.parent_id = s.id
        )
        SELECT r.* FROM drive_project_refs r WHERE r.drive_node_id IN (SELECT id FROM subtree)
        """,
        (str(node_id),),
    ).fetchall()
    return [dict(row) for row in rows]


@_with_schema
def purge(store: Any, actor: DriveActor, node_id: str) -> dict[str, Any]:
    """彻底清除：**先落库、后删对象**，且被项目引用时拒绝。

    只有**回收站里**的节点能清除（两步走：软删除 → 彻底清除，计划 §7.3）；目录必须已空
    （第一版不做递归彻底清除——误点一次不该把整棵树蒸发掉）。对象只有**没有其它节点引用**时才真删。
    """

    row = _require_node(store, actor, node_id, include_deleted=True)
    if row["is_root"]:
        raise DriveError("file_root_immutable")
    if not _field(row, "deleted_at"):
        raise DriveError("file_not_trashed")
    if _refs_in_subtree(store, str(row["id"])):
        raise DriveError("file_referenced_by_project")
    if str(row["kind"]) == "directory":
        children = store.db.execute(
            "SELECT COUNT(*) AS c FROM drive_nodes WHERE parent_id = ? AND purged_at IS NULL", (str(row["id"]),)
        ).fetchone()["c"]
        if int(children):
            raise DriveError("file_directory_not_empty")
    storage_keys: list[tuple[str, int]] = []
    rows = store.db.execute(
        "SELECT storage_key, size_bytes FROM drive_nodes WHERE trashed_with = ? AND storage_key IS NOT NULL",
        (str(row["id"]),),
    ).fetchall()
    for item in rows:
        key = str(item["storage_key"])
        others = store.db.execute(
            """
            SELECT COUNT(*) AS c FROM drive_nodes
            WHERE storage_key = ? AND purged_at IS NULL AND (trashed_with IS NULL OR trashed_with <> ?)
            """,
            (key, str(row["id"])),
        ).fetchone()["c"]
        if not int(others):
            storage_keys.append((key, int(item["size_bytes"] or 0)))
    timestamp = _now()
    with _transaction(store):
        # 先把"要删的对象"记进队列并提交（下面才动对象存储）
        for key, size in storage_keys:
            store.db.execute(
                """
                INSERT INTO drive_object_cleanup
                    (id, organization_id, owner_member_id, storage_key, size_bytes, status, attempts, last_error, enqueued_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                """,
                (str(uuid4()), actor.organization_id, actor.member_id, key, size, timestamp, timestamp),
            )
        store.db.execute(
            "UPDATE drive_nodes SET purged_at = ?, scan_status = 'purged', updated_at = ? WHERE trashed_with = ?",
            (timestamp, timestamp, str(row["id"])),
        )
        audit(store, actor, "purge", node=row, capability="drive.file.purge", reason=f"{len(storage_keys)} 个对象入队")
    deleted, failed = _drain_cleanup(store, actor.organization_id, storage_keys)
    return {
        "id": str(row["id"]),
        "purged": True,
        "objects_deleted": deleted,
        "objects_pending": failed,
        "storage_keys": [key for key, _ in storage_keys],
    }


def _drain_cleanup(store: Any, organization_id: str, keys: list[tuple[str, int]]) -> tuple[int, int]:
    """尽力删对象；失败留在队列里（`status='pending'`、`attempts+1`、记 `last_error`）。"""

    deleted = 0
    pending = 0
    for key, _size in keys:
        row = store.db.execute(
            "SELECT id FROM drive_object_cleanup WHERE organization_id = ? AND storage_key = ? AND status = 'pending' ORDER BY enqueued_at DESC LIMIT 1",
            (organization_id, key),
        ).fetchone()
        try:
            store.object_store.delete(key)
        except Exception as error:  # noqa: BLE001 - 删不掉不能丢追踪记录
            pending += 1
            if row is not None:
                store.db.execute(
                    "UPDATE drive_object_cleanup SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE id = ?",
                    (f"{type(error).__name__}:{error}"[:300], _now(), str(row["id"])),
                )
                store.db.commit()
            continue
        deleted += 1
        if row is not None:
            store.db.execute(
                "UPDATE drive_object_cleanup SET status = 'deleted', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), str(row["id"])),
            )
            store.db.commit()
    return deleted, pending


@_with_schema
def cleanup_queue(store: Any, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM drive_object_cleanup WHERE status = ? ORDER BY enqueued_at ASC LIMIT ?",
        (status, max(1, min(int(limit or 100), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


@_with_schema
def retry_cleanup(store: Any, *, limit: int = 50) -> dict[str, Any]:
    """重试队列里的删除（运维/定时用）。删成功的标 `deleted`，失败的留 `pending` 并计数。"""

    pending = cleanup_queue(store, status="pending", limit=limit)
    deleted = 0
    failed = 0
    for item in pending:
        try:
            store.object_store.delete(str(item["storage_key"]))
        except Exception as error:  # noqa: BLE001
            failed += 1
            store.db.execute(
                "UPDATE drive_object_cleanup SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE id = ?",
                (f"{type(error).__name__}:{error}"[:300], _now(), str(item["id"])),
            )
        else:
            deleted += 1
            store.db.execute(
                "UPDATE drive_object_cleanup SET status = 'deleted', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), str(item["id"])),
            )
        store.db.commit()
    return {"attempted": len(pending), "deleted": deleted, "failed": failed}


@_with_schema
def orphan_objects(store: Any, *, limit: int = 500) -> list[str]:
    """孤儿对象**只报告不删**（反向扫描：对象存储里有、节点树里没人引用）。

    为什么只报告：对象存储的列举能力在后端之间不一致（本地目录 vs S3 分页），而"删一个还没有
    归属的对象"是不可逆的。计划的顺序是"先能看见，再决定怎么清"（FM-6 的孤儿扫描）。
    """

    keys: list[str] = []
    store_root = getattr(store.object_store, "root", None)
    if store_root is None:  # S3 后端：本期不做列举（FM-6）
        return []
    base = Path(store_root)
    if not base.is_dir():
        return []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(base).as_posix()
        if not key.startswith("drive/"):
            continue
        row = store.db.execute(
            "SELECT COUNT(*) AS c FROM drive_nodes WHERE storage_key = ? AND purged_at IS NULL", (key,)
        ).fetchone()
        if not int(row["c"]):
            keys.append(key)
        if len(keys) >= limit:
            break
    return keys


# ---- 旧数据回填 -------------------------------------------------------------


@_with_schema
def backfill_legacy(store: Any) -> dict[str, int]:
    """把 `personal_drive_files` 的老行搬进节点树（幂等，可在每次启动时跑）。

    - **保留**旧 id、storage_key、content_hash、created_at（计划 §9 FM-1 第 6 条）；
    - `project_ids`（JSON 字符串）展开成 `drive_project_refs`：拿不到准确的成果物 id，
      就写 `artifact_id = NULL` 且 `legacy_import = true`（计划 §5.2 明确允许）；
    - 老表**不删**：搬完它是只读备份（回滚时还能对照）。
    """

    exists = store.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'personal_drive_files'"
    ).fetchone()
    if exists is None:
        return {"scanned": 0, "created": 0, "refs": 0, "skipped": 0}
    rows = store.db.execute("SELECT * FROM personal_drive_files").fetchall()
    created = 0
    refs = 0
    skipped = 0
    for row in rows:
        owner = str(row["owner"])
        member = store.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (owner,)).fetchone()
        if member is None:
            skipped += 1  # 成员已经不存在（历史数据）：不编组织，跳过并下面用 skipped 如实报出来
            continue
        organization_id = str(member["organization_id"])
        node_id = str(row["id"])
        if _raw(store, node_id) is not None:
            skipped += 1
            continue
        actor = DriveActor(member_id=owner, organization_id=organization_id)
        root = root_node(store, actor)
        with _transaction(store):
            store.db.execute(
                """
                INSERT INTO drive_nodes
                    (id, organization_id, owner_member_id, parent_id, kind, name, name_key, storage_key,
                     size_bytes, content_hash, mime_type, is_archive, source_artifact_id, revision,
                     scan_status, is_root, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, 1, 'clean', 0, ?, ?)
                """,
                (
                    node_id,
                    organization_id,
                    owner,
                    str(root["id"]),
                    str(row["name"]),
                    name_key(str(row["name"])),
                    str(row["storage_key"]),
                    int(row["size_bytes"] or 0),
                    row["content_hash"],
                    row["mime_type"],
                    int(row["is_archive"] or 0),
                    None,
                    str(row["created_at"]),
                    str(row["created_at"]),
                ),
            )
            created += 1
        try:
            project_ids = json.loads(row["project_ids"] or "[]")
        except ValueError:
            project_ids = []
        for project_id in project_ids if isinstance(project_ids, list) else []:
            existing_ref = store.db.execute(
                "SELECT id FROM drive_project_refs WHERE drive_node_id = ? AND project_id = ? AND artifact_key = ''",
                (node_id, str(project_id)),
            ).fetchone()
            if existing_ref is not None:
                continue
            with _transaction(store):
                store.db.execute(
                    """
                    INSERT INTO drive_project_refs
                        (id, drive_node_id, project_id, artifact_id, artifact_key, imported_by, imported_at, legacy_import)
                    VALUES (?, ?, ?, NULL, '', ?, ?, 1)
                    """,
                    (str(uuid4()), node_id, str(project_id), owner, str(row["created_at"])),
                )
            refs += 1
    return {"scanned": len(rows), "created": created, "refs": refs, "skipped": skipped}


@_with_schema
def add_ref(
    store: Any,
    actor: DriveActor,
    node: Any,
    project_id: str,
    artifact_id: str | None,
    *,
    legacy: bool = False,
) -> bool:
    """登记"这份文件被哪个项目导入过"（重复导入同一项目不产生第二条）。"""

    existing = store.db.execute(
        "SELECT id FROM drive_project_refs WHERE drive_node_id = ? AND project_id = ? AND artifact_key = ?",
        (str(node["id"]), str(project_id), str(artifact_id or "")),
    ).fetchone()
    if existing is not None:
        return False
    store.db.execute(
        """
        INSERT INTO drive_project_refs
            (id, drive_node_id, project_id, artifact_id, artifact_key, imported_by, imported_at, legacy_import)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            str(node["id"]),
            str(project_id),
            str(artifact_id) if artifact_id else None,
            str(artifact_id or ""),
            actor.member_id,
            _now(),
            int(bool(legacy)),
        ),
    )
    return True


__all__ = [
    "ARCHIVE_SUFFIXES",
    "DEFAULT_QUOTA_BYTES",
    "DriveActor",
    "DriveError",
    "actor_for",
    "add_ref",
    "audit",
    "audit_log",
    "node_audit",
    "backfill_legacy",
    "breadcrumb",
    "cleanup_queue",
    "copy",
    "create_directory",
    "ensure_schema",
    "get_node",
    "is_archive_name",
    "legacy_code",
    "list_children",
    "move",
    "name_key",
    "orphan_objects",
    "purge",
    "put_file",
    "quota_bytes",
    "read_content",
    "refs_for",
    "rename",
    "resolve_parent",
    "restore",
    "retry_cleanup",
    "root_node",
    "trash",
    "trash_list",
    "usage",
    "validate_name",
]
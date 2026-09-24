-- 029_drive_nodes.sql
-- 个人云盘正式数据模型（FM-1：把运行时动态建表的平面文件表升级成带目录、引用与回收站的节点树）。
--
-- 背景：`personal_drive_files` 是「一个成员一堆平铺文件」，没有目录、没有下载端点、没有回收站，
-- 且表是运行时 `CREATE TABLE IF NOT EXISTS` 建的（不在迁移里、没有 RLS）。文件管理要把它当
-- 正经的云盘用，就必须先有树、有约束、有租户隔离。
--
-- 三张表 + 一张审计表：
--   * `drive_nodes`          —— 目录/文件节点树（每成员一个根，`is_root = 1`）；
--   * `drive_project_refs`   —— 「这份文件被哪个项目导入过」，替代旧的 `project_ids` JSON 字符串；
--   * `drive_object_cleanup` —— **对象清理队列**：先落库、后删对象。删对象失败也不能先丢追踪记录；
--   * `drive_audit`          —— 云盘操作审计（谁、对哪个节点、做了什么、允许还是拒绝）。
--
-- 三条硬约束（SQLite 侧同名同义，见 `app/drive.py` 的 `ensure_schema`）：
--   1. 同成员、同父目录下**存活**节点名唯一（部分唯一索引：回收站里的名字可以被重新使用）；
--   2. 每个成员**只有一个根目录**（部分唯一索引在 `is_root = 1` 上）；
--   3. 目录不得有 storage_key、文件必须有（由服务层校验；这里给不了跨列 CHECK 的通用写法，
--      所以 `scan_status` 与 `content_hash` 的耦合判定放在服务层，见计划 §5.1）。
--
-- 旧表 `personal_drive_files` **不删**：迁移期保留只读备份，服务层做一次性回填（同 id、同
-- storage_key、同 content_hash、同 created_at），回填是幂等的（`INSERT ... ON CONFLICT DO NOTHING`）。
--
-- 注意占位符风格：迁移里是 PostgreSQL 语法（`jsonb`、`timestamptz`、部分索引、RLS），
-- 不要出现 SQLite 的问号占位符（契约测试会拦，它按字符扫）。

CREATE TABLE IF NOT EXISTS drive_nodes (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    owner_member_id text NOT NULL,
    parent_id text REFERENCES drive_nodes(id),
    kind text NOT NULL,
    name text NOT NULL,
    name_key text NOT NULL,
    storage_key text,
    size_bytes bigint NOT NULL DEFAULT 0,
    content_hash text,
    mime_type text,
    is_archive boolean NOT NULL DEFAULT false,
    source_artifact_id text,
    revision bigint NOT NULL DEFAULT 1,
    scan_status text NOT NULL DEFAULT 'clean',
    is_root boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    trashed_with text,
    purged_at timestamptz
);

-- 同父同名唯一：只约束**存活**节点，回收站里的名字可以重新用（否则"删掉再建同名"会被自己拦住）
CREATE UNIQUE INDEX IF NOT EXISTS drive_nodes_sibling_name_idx
    ON drive_nodes(owner_member_id, parent_id, name_key)
    WHERE deleted_at IS NULL AND is_root = false;
-- 每个成员一个根
CREATE UNIQUE INDEX IF NOT EXISTS drive_nodes_root_idx
    ON drive_nodes(owner_member_id)
    WHERE is_root = true;
-- 列表/回收站/来源查询
CREATE INDEX IF NOT EXISTS drive_nodes_parent_idx ON drive_nodes(owner_member_id, parent_id, kind, name_key);
CREATE INDEX IF NOT EXISTS drive_nodes_trash_idx ON drive_nodes(owner_member_id, trashed_with);
CREATE INDEX IF NOT EXISTS drive_nodes_artifact_idx ON drive_nodes(source_artifact_id);
-- 内容复用（同成员同 hash 共用对象，配额只算一份）
CREATE INDEX IF NOT EXISTS drive_nodes_hash_idx ON drive_nodes(owner_member_id, content_hash);

CREATE TABLE IF NOT EXISTS drive_project_refs (
    id text PRIMARY KEY,
    drive_node_id text NOT NULL REFERENCES drive_nodes(id),
    project_id text NOT NULL,
    artifact_id text,
    -- 空串代替 NULL：唯一约束里 NULL 互不相等，用 NULL 会让同一条引用能插两遍
    artifact_key text NOT NULL DEFAULT '',
    imported_by text NOT NULL,
    imported_at timestamptz NOT NULL,
    legacy_import boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS drive_project_refs_unique_idx
    ON drive_project_refs(drive_node_id, project_id, artifact_key);
CREATE INDEX IF NOT EXISTS drive_project_refs_project_idx ON drive_project_refs(project_id);

CREATE TABLE IF NOT EXISTS drive_object_cleanup (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    owner_member_id text NOT NULL,
    storage_key text NOT NULL,
    size_bytes bigint NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    enqueued_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS drive_object_cleanup_status_idx ON drive_object_cleanup(status, enqueued_at);

CREATE TABLE IF NOT EXISTS drive_audit (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    member_id text NOT NULL,
    node_id text,
    parent_id text,
    action text NOT NULL,
    capability text NOT NULL DEFAULT '',
    decision text NOT NULL DEFAULT 'allow',
    reason text NOT NULL DEFAULT '',
    name_hash text,
    content_hash text,
    size_bytes bigint NOT NULL DEFAULT 0,
    revision bigint,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS drive_audit_node_idx ON drive_audit(node_id, created_at);
CREATE INDEX IF NOT EXISTS drive_audit_member_idx ON drive_audit(member_id, created_at);

-- 租户隔离：云盘是成员私有资产，但仍是组织内的数据，与其余业务表同一套 RLS 口径
ALTER TABLE drive_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE drive_project_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE drive_object_cleanup ENABLE ROW LEVEL SECURITY;
ALTER TABLE drive_audit ENABLE ROW LEVEL SECURITY;

CREATE POLICY drive_nodes_tenant_policy ON drive_nodes
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
-- 引用表没有自己的 organization_id：跟着节点走（节点已经被租户策略挡住）
CREATE POLICY drive_project_refs_tenant_policy ON drive_project_refs
    USING (EXISTS (SELECT 1 FROM drive_nodes n WHERE n.id = drive_project_refs.drive_node_id))
    WITH CHECK (EXISTS (SELECT 1 FROM drive_nodes n WHERE n.id = drive_project_refs.drive_node_id));
CREATE POLICY drive_object_cleanup_tenant_policy ON drive_object_cleanup
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY drive_audit_tenant_policy ON drive_audit
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
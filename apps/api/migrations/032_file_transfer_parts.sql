-- 032_file_transfer_parts.sql
-- 大文件**断点续传**（FM-6）：把分片上传接到 `file_transfer_sessions` 上。
--
-- 为什么需要它：一期传输是"一次性 PUT 整份内容"——几百 MB 的文件在中途断一次就得从头再来，
-- 而"大文件"恰恰是最容易断的场景。这里把传输会话升级成**可续传**的：
--   * 传输发起时向对象存储申请一个 multipart 上传会话（本地目录与 S3 两条后端都支持）；
--   * 客户端按 `part_number` 逐片上传，每片独立校验 sha256；断线后先 `GET .../parts` 拿续传游标，
--     把没传上去的那几片补上即可，已传的不用重传；
--   * 全部到齐才 `complete`（缺片明确报错，不拼出半个文件）；
--   * 最终对象还要再比对一次整体 sha256（与会话声明的 expected_hash 对不上就删掉、标 failed）。
--
-- 幂等与冲突：同一 `(transfer_id, part_number)` 重复上传 = **覆盖该片**（重试的常态），
-- 但要写入新的 sha256；片号越界（<1 或 >10000，S3 的硬限制）直接拒绝。

CREATE TABLE IF NOT EXISTS file_transfer_parts (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    transfer_id text NOT NULL REFERENCES file_transfer_sessions(id),
    part_number integer NOT NULL,
    size_bytes bigint NOT NULL DEFAULT 0,
    content_hash text NOT NULL DEFAULT '',
    etag text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS file_transfer_parts_unique_idx
    ON file_transfer_parts(transfer_id, part_number);
CREATE INDEX IF NOT EXISTS file_transfer_parts_transfer_idx ON file_transfer_parts(transfer_id);

-- 会话上记住对象存储的多分片会话 id（发起一次、续传期间复用）
ALTER TABLE file_transfer_sessions ADD COLUMN IF NOT EXISTS multipart_upload_id text;
-- 分片大小（**首片上传时定下**：客户端按它切片，游标按它推"还缺哪些"）
ALTER TABLE file_transfer_sessions ADD COLUMN IF NOT EXISTS part_size_bytes bigint NOT NULL DEFAULT 0;
-- 会话属于哪个成员：传输内容可能是他从自己私有云盘搬出来的，
-- **不能按组织收口**（同组织同事不该看到/读到别人的传输会话）
ALTER TABLE file_transfer_sessions ADD COLUMN IF NOT EXISTS owner_member_id text;

ALTER TABLE file_transfer_parts ENABLE ROW LEVEL SECURITY;

CREATE POLICY file_transfer_parts_tenant_policy ON file_transfer_parts
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
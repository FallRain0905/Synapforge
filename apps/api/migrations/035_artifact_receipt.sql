-- 035_artifact_receipt.sql
-- 产物溯源 receipt（W2.4，docs/RECEIPT_FORMAT.md §5.1 采纳"加列"方案）。
--
-- 回答"这个成果物是哪次执行、哪次工具调用产出的"：一产物一 receipt 的主形态，
-- B（agentd）上传时随 ArtifactCreate.receipt 提交，A 校验结构后落库这几列，并在
-- project.artifact.uploaded 事件的 payload.receipt 里原样携带（见 AGENT_EVENT_CONTRACT §5）。
--
-- 列语义（receipt_version = 0 表示没有 receipt——历史行、人工上传、或 receipt 被拒收）：
--   * tool_name / tool_call_id：产出工具与执行体侧调用标识（≤128 字符，生成侧截断）
--   * args_hash / output_hash：SHA-256 前 16 位小写 hex（RECEIPT_FORMAT §2 的纠正口径，
--     字段名如实叫 *_hash 不叫 *_sha256）
--   * output_bytes：计入哈希的字节数；truncated：哈希只覆盖保留段
-- receipt.status 不落列：能被上传成 artifact 的产物默认成功，失败产物不建 artifact；
-- source_artifact_hashes 不落列：输入链沿既有 input_artifact_ids（按 id 可回查各自 hash）。

ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS receipt_version integer NOT NULL DEFAULT 0;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS tool_name text NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS tool_call_id text NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS args_hash text NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS output_hash text NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS output_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS truncated boolean NOT NULL DEFAULT false;

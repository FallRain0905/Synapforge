-- 039_coordination_recovery.sql
-- D3 恢复/幂等收口：
--   * stage_reports.idempotency_key：重复提交返回**原报告**（幂等，不重复记账）——
--     执行体重试/网络重放不得产生第二份同 attempt 的报告；
--   * 部分唯一索引：键非空时全局唯一（null = 历史行/未带键，不受约束）。

ALTER TABLE stage_reports ADD COLUMN IF NOT EXISTS idempotency_key text;
CREATE UNIQUE INDEX IF NOT EXISTS stage_reports_idem_idx
    ON stage_reports(idempotency_key) WHERE idempotency_key IS NOT NULL;

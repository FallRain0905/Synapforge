-- 027_agent_turn_approvals.sql
-- 对话轮次里的**权限请求**（M-5c S-3：把执行体的 approval 做成页面上的"待批准"卡片）。
--
-- 背景：opencode 在动工作区外的文件（以及部分命令）之前会发 `permission.asked`，**不回复就不继续**。
-- 内核此前沿用 CLI 通道的 `--auto` 等价语义一律放行（S-1），这一期改成真卡片：
--   执行体报一条请求 → 页面出现待批准卡片 → 人点批准/拒绝 → 内核按决定回复 opencode。
--
-- 实测的回复取值与语义（探针在真机上跑过三遍）：
--   `reject` → HTTP 200，**文件确实没被写出来**；`once` / `always` → 写出来了。
-- 所以卡片上的三个按钮就是这三档；`decided_by` 记下是谁批的（审计要有人）。
--
-- 幂等：主键就是 opencode 的 request id（`per_…`），执行体重报同一条不会产生第二张卡；
-- 决策只认第一次（后到的决策不覆盖已定的结果——与平台其余幂等口径一致）。

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
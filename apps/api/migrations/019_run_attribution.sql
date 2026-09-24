-- 019_run_attribution.sql
-- 执行归属落库：让"哪台设备、谁的机器"在 Run 与事件里直接可读。
--
-- 背景：设计文档（AGENT_DEVICE_CONNECTION_DESIGN.md）要求
--   "任务结果、进程事件和人工操作都必须可以追溯到设备、Agent、成员和 Run"，
-- 但 runs 表此前只有 agent_id，事件 actor_kind 多数落成 system——
-- 多成员协作时"这是谁家的机器干的活"只能靠 agents.owner_member_id 事后反推。
--
-- 语义：
--   * device_id：执行体上报（HTTP 路径由内核带，Gateway 路径由平台用最近活跃连接解析），
--     服务端会校验它与 agent_id 对得上，对不上就丢弃（防伪造）
--   * member_id：**一律服务端推导**（设备归属优先、其次 Agent 归属），不接受客户端自报
--   * 历史事件回填：把已知的 Agent 行为（claim/progress/result/run.*）从 actor_kind=system
--     改为 agent——只改这些明确由执行体发起的事件类型，不动人工操作

ALTER TABLE runs ADD COLUMN IF NOT EXISTS device_id text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS member_id text;

CREATE INDEX IF NOT EXISTS runs_device_id_idx ON runs (device_id);
CREATE INDEX IF NOT EXISTS runs_member_id_idx ON runs (member_id);

UPDATE events
   SET actor_kind = 'agent'
 WHERE actor_kind = 'system'
   AND actor LIKE 'agent-%'
   AND event_type IN (
       'task.claimed', 'task.progress', 'task.result_submitted',
       'run.created', 'run.completed', 'run.failed', 'run.blocked'
   );
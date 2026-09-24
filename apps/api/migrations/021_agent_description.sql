-- 021_agent_description.sql
-- 执行体描述的机器可读化（AIP-1a / AIP-1c，见 docs/AIP_1_PLAN.md）：
--   能力卡（技能名 + 版本 + 输入 + 输出）、执行体身份拆成"程序包序列号 + 实例序列号"。
--
-- 背景：`required_capabilities` 与执行体自报的能力此前都是**自由字符串**、按精确相等匹配
--   （`store._task_requirements_satisfied`），于是 `python` 与 `Python` 被当成两种能力、
--   能力没有版本，而且 `agent_id` 把"哪种执行体（codex/cli/…）"与"哪个实例（哪台机器）"
--   混在一个字符串里，统计"同一执行体多实例"只能靠前缀猜。
--
-- 语义：
--   * capability_cards：`[{skill, version, inputs, outputs, description}]`，技能 id 由应用侧
--     归一化（小写、分隔符折叠）；`agents.supported_tools` 保留为卡片技能的扁平派生视图，
--     老内核只发字符串数组时照旧工作（本迁移不改任何既有列的值）。
--   * package_id：执行体程序包标识（跨机器相同，如 `codex@0.9.3`）；
--   * instance_id：实例标识（同包的不同机器互不相同）——今天等于 agent_id，冗余一列是为了
--     让"包 / 实例"两级在数据模型里显式，不再靠字符串解析；
--   * package_source：`reported`（内核上报）/ `inferred`（平台按设备探测结果推断）——
--     推断值必须可被识别，避免把猜测当事实（见计划 §5.1）。
--
-- 回填策略：本迁移只做**不可能出错**的部分——instance_id 补齐、package_source 缺省标注。
--   package_id 的推断需要读设备运行态（device_runtime_state.adapter_versions），
--   由应用侧幂等执行（`Store._backfill_agent_packages`），只在 package_id IS NULL 时写。

ALTER TABLE agents ADD COLUMN IF NOT EXISTS capability_cards jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS package_id text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS instance_id text;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS package_source text NOT NULL DEFAULT 'inferred';

CREATE INDEX IF NOT EXISTS agents_package_id_idx ON agents (package_id);

UPDATE agents SET instance_id = agent_id WHERE instance_id IS NULL;

-- 技能域与授权范围域是两套词表：授权范围（task.claim / artifact.write …）值形如
-- `<域>.<动作>`。这里建一个只读视图，便于运维核对"谁声明了什么技能、被授予了什么范围"。
CREATE OR REPLACE VIEW agent_skill_declarations AS
SELECT a.agent_id,
       a.package_id,
       a.instance_id,
       a.package_source,
       card->>'skill' AS skill,
       card->>'version' AS version
  FROM agents a
  LEFT JOIN LATERAL jsonb_array_elements(COALESCE(a.capability_cards, '[]'::jsonb)) AS card ON TRUE;
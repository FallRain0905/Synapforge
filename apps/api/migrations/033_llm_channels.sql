-- 033_llm_channels.sql
-- LLM 渠道与全员免费额度：管理员在后台录入 OpenAI 兼容上游（渠道），所有成员
-- 通过平台代理端点消费平台提供的免费 LLM 额度，按 token 扣减并记用量流水。
--
-- 三张表：
--   * llm_channels        渠道本体（base_url + api_key + 模型列表 + 优先级 + 检测/测速结果）
--   * llm_member_quotas   成员额度（token 上限与已用；**负数上限 = 不限量**）
--   * llm_usage_log       用量流水（每次代理调用一条：谁、走哪个渠道、哪个模型、多少 token）
--
-- 安全口径：
--   * api_key 只落库，任何接口都不回传明文（对外只给 key_hint 末 4 位）；
--   * 渠道清单与 key 只有管理员能看；成员只能读自己的额度与可用模型；
--   * 渠道行归属创建管理员所在组织（organization_id = actor 的组织），额度与流水
--     归属对应成员的组织，RLS 与其余迁移同一口径（app.current_organization_id()）。

CREATE TABLE IF NOT EXISTS llm_channels (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    name text NOT NULL,
    base_url text NOT NULL,
    api_key text NOT NULL DEFAULT '',
    models text NOT NULL DEFAULT '[]',
    priority integer NOT NULL DEFAULT 100,
    enabled boolean NOT NULL DEFAULT true,
    last_check_at timestamptz,
    last_check_ok boolean,
    last_check_detail text NOT NULL DEFAULT '',
    last_latency_ms integer,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS llm_channels_name_idx ON llm_channels(name);

CREATE TABLE IF NOT EXISTS llm_member_quotas (
    member_id text PRIMARY KEY,
    organization_id text NOT NULL,
    token_limit bigint NOT NULL,
    tokens_used bigint NOT NULL DEFAULT 0,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage_log (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    member_id text NOT NULL,
    channel_id text,
    model text NOT NULL DEFAULT '',
    prompt_tokens bigint NOT NULL DEFAULT 0,
    completion_tokens bigint NOT NULL DEFAULT 0,
    total_tokens bigint NOT NULL DEFAULT 0,
    latency_ms integer,
    status text NOT NULL,
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_log_member_idx ON llm_usage_log(member_id);
CREATE INDEX IF NOT EXISTS llm_usage_log_org_idx ON llm_usage_log(organization_id, created_at);

ALTER TABLE llm_channels ENABLE ROW LEVEL SECURITY;
CREATE POLICY llm_channels_tenant_policy ON llm_channels
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE llm_member_quotas ENABLE ROW LEVEL SECURITY;
CREATE POLICY llm_member_quotas_tenant_policy ON llm_member_quotas
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE llm_usage_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY llm_usage_log_tenant_policy ON llm_usage_log
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

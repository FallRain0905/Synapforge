-- 017_accounts.sql
-- 账号系统：口令哈希、管理员标记、登入时间；会话令牌从明文改为只存 SHA-256。
--
-- 设计说明：
--   * human_members.password_hash 存自描述哈希串 `scrypt$n$r$p$salt$digest`，
--     代价参数随串一起存，未来调参不影响老账号校验；历史成员该列为 NULL（无口令，
--     视为不可登录，由首个管理员通过邀请码/重置流程接管）。
--   * is_admin 是"全局管理员"标记（账号管理权限），与项目级角色
--     (project_memberships.role) 是两套东西，后者管项目内权限。
--   * sessions.token 的列名保持不变，语义改为存 SHA-256 十六进制（64 字符）。
--     历史行是明文令牌（43 字符），改语义后无法再解析，直接清掉。

ALTER TABLE human_members ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE human_members ADD COLUMN IF NOT EXISTS password_updated_at timestamptz;
ALTER TABLE human_members ADD COLUMN IF NOT EXISTS last_login_at timestamptz;
ALTER TABLE human_members ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;

DELETE FROM sessions;

CREATE INDEX IF NOT EXISTS sessions_member_id_idx ON sessions (member_id);
CREATE INDEX IF NOT EXISTS human_members_email_lower_idx ON human_members (lower(email));
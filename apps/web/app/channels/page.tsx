"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /channels → 设置 Hub 的「渠道」Tab（管理员权限校验随渠道视图自带）。 */
export default function ChannelsRedirectPage() {
  return (
    <LegacyRedirect
      from="/channels"
      to="/settings?tab=/channels"
      label="LLM 渠道"
      hint="LLM 渠道已收进「设置」Hub"
      testId="channels-redirect"
    />
  );
}

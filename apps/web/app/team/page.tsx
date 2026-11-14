"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /team → 设置 Hub 的「团队」Tab（query 原样保留，replace 不入历史）。 */
export default function TeamRedirectPage() {
  return (
    <LegacyRedirect
      from="/team"
      to="/settings?tab=/team"
      label="团队与成员"
      hint="团队与成员已收进「设置」Hub"
      testId="team-redirect"
    />
  );
}

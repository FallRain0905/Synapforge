"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /account → 设置 Hub 的「账号」Tab（侧栏/顶栏账号入口也指向这里）。 */
export default function AccountRedirectPage() {
  return (
    <LegacyRedirect
      from="/account"
      to="/settings?tab=/account"
      label="账号设置"
      hint="账号设置已收进「设置」Hub"
      testId="account-redirect"
    />
  );
}

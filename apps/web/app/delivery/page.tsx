"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /delivery → 工作流 Hub 的「论文交付」Tab（query 原样保留）。 */
export default function DeliveryRedirectPage() {
  return (
    <LegacyRedirect
      from="/delivery"
      to="/workflows?tab=/delivery"
      label="论文交付"
      hint="论文交付已收进「工作流」Hub"
      testId="delivery-redirect"
    />
  );
}

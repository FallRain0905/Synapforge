"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /pack → 工作流 Hub 的「建模模板包」Tab（query 原样保留）。 */
export default function PackRedirectPage() {
  return (
    <LegacyRedirect
      from="/pack"
      to="/workflows?tab=/pack"
      label="建模模板包"
      hint="建模模板包已收进「工作流」Hub"
      testId="pack-redirect"
    />
  );
}

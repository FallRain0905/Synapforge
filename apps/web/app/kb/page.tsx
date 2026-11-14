"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /kb → 工作流 Hub 的「知识库」Tab（query 原样保留）。 */
export default function KbRedirectPage() {
  return (
    <LegacyRedirect
      from="/kb"
      to="/workflows?tab=/kb"
      label="知识库"
      hint="知识库已收进「工作流」Hub"
      testId="kb-redirect"
    />
  );
}

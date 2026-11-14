"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /graph → 工作流 Hub 的「图谱」Tab（query 原样保留）。 */
export default function GraphRedirectPage() {
  return (
    <LegacyRedirect
      from="/graph"
      to="/workflows?tab=/graph"
      label="图谱可视化"
      hint="图谱可视化已收进「工作流」Hub"
      testId="graph-redirect"
    />
  );
}

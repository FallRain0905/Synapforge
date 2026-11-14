"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /artifacts → 文档 Hub 的「成果物」Tab（query 原样保留，replace 不入历史）。 */
export default function ArtifactsRedirectPage() {
  return (
    <LegacyRedirect
      from="/artifacts"
      to="/documents?tab=/artifacts"
      label="成果物库"
      hint="成果物库已收进「文档与成果」Hub"
      testId="artifacts-redirect"
    />
  );
}

"use client";

import { LegacyRedirect } from "../../components/legacy-redirect";

/** 旧 /drive → 文档 Hub 的「个人云盘」Tab（完整文件管理器随 Tab 内嵌，query 原样保留）。 */
export default function DriveRedirectPage() {
  return (
    <LegacyRedirect
      from="/drive"
      to="/documents?tab=/drive"
      label="个人云盘"
      hint="个人云盘已收进「文档与成果」Hub"
      testId="drive-redirect"
    />
  );
}

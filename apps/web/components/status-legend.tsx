"use client";

import { Info } from "lucide-react";
import { STATUS_DICTIONARY, type StatusDomain } from "../lib/status-dictionary";

/**
 * 状态说明（可折叠，W0.4）：挂在列表页标题下方，默认只占一行，
 * 展开后按领域列出每个状态的用户可读含义。只用既有样式类，不新增 CSS。
 */
export function StatusLegend({ domains }: { domains: StatusDomain[] }) {
  return (
    <details className="hint" data-testid="status-legend">
      <summary style={{ cursor: "pointer", listStyle: "none" }}>
        <Info size={13} style={{ display: "inline", marginRight: 4 }} />
        状态说明
      </summary>
      <div style={{ marginTop: 6, display: "grid", gap: 2 }}>
        {domains.flatMap((domain) =>
          Object.entries(STATUS_DICTIONARY[domain]).map(([status, text]) => (
            <div key={`${domain}:${status}`} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
              <strong style={{ minWidth: 128, flexShrink: 0 }}>{status}</strong>
              <small>{text}</small>
            </div>
          )),
        )}
      </div>
    </details>
  );
}

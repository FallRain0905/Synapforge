"use strict";

// 更新源的纯函数部分（不依赖 electron，方便 node 直接测）。
async function fetchFeed(feed) {
  const response = await fetch(feed, {
    signal: AbortSignal.timeout(8000),
    headers: { "Cache-Control": "no-cache" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  const version = String(payload.version || "").trim();
  if (!version) throw new Error("latest.json 缺少 version");
  return {
    version,
    url: String(payload.url || payload.download || ""),
    notes: String(payload.notes || ""),
  };
}

module.exports = { fetchFeed };

#!/usr/bin/env python3
"""M-5c S-0 探针（v1 通道）：/event + POST /session + POST /session/{id}/message，采真实事件形状。用完即删。"""

import collections
import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4123"
MODEL = {"providerID": "deepseek", "modelID": "deepseek-v4.1-flash"}
PROMPT = (
    "请分三步：① 先运行 `ls` 看一眼当前工作目录，用一句话说明你看到了什么；"
    "② 把结果写进 /tmp/oc-probe-out.txt（没权限就直说没写成）；"
    "③ 用 Markdown 表格列出 2 行示例（两列：步骤 / 说明），最后一句中文总结。"
)

events: list[dict] = []
lock = threading.Lock()
raw = open("/tmp/oc-sse-v1-raw.jsonl", "w", encoding="utf-8")


def sse_reader() -> None:
    request = urllib.request.Request(f"{BASE}/event", headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"_unparsed": payload}
            with lock:
                events.append({"at": round(time.time(), 3), "data": data})
            raw.write(json.dumps({"at": round(time.time(), 3), "data": data}, ensure_ascii=False) + "\n")
            raw.flush()


def call(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_text": text[:400]}


def summarize() -> str:
    with lock:
        snapshot = list(events)
    counts = collections.Counter()
    samples: dict[str, str] = {}
    part_updates: list[str] = []
    for item in snapshot:
        data = item["data"]
        kind = str(data.get("type"))
        counts[kind] += 1
        if kind not in samples:
            samples[kind] = json.dumps(data, ensure_ascii=False)
        if "part" in kind:
            part = (data.get("properties") or {}).get("part") or {}
            text = part.get("text")
            part_updates.append(f"{part.get('type')}:{len(text) if isinstance(text, str) else '-'}")
    lines = [f"事件总数：{len(snapshot)}", ""]
    for kind, count in counts.most_common():
        lines.append(f"{count:5d}  {kind}")
        lines.append(f"       sample: {samples[kind][:600]}")
    lines.append("")
    lines.append(f"part.updated 事件数：{len(part_updates)}；前 24 条（类型:文本长度）：{part_updates[:24]}")
    return "\n".join(lines)


threading.Thread(target=sse_reader, daemon=True).start()
time.sleep(2)

created = call("POST", "/session", {"title": "S-0 probe"})
session_id = created.get("id") or (created.get("data") or {}).get("id")
print(f"[probe] session={session_id} raw={json.dumps(created, ensure_ascii=False)[:200]}", flush=True)

try:
    message = call(
        "POST",
        f"/session/{session_id}/message",
        {"model": MODEL, "parts": [{"type": "text", "text": PROMPT}]},
        timeout=240,
    )
    print(f"[probe] message keys={sorted(message)[:12]} error={message.get('error')}", flush=True)
except urllib.error.HTTPError as error:
    print(f"[probe] message HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:400]}", flush=True)

time.sleep(3)
summary = summarize()
with open("/tmp/oc-sse-v1-summary.txt", "w", encoding="utf-8") as handle:
    handle.write(summary)
raw.close()
print(summary, flush=True)
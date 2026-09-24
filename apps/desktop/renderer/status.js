// 状态页渲染逻辑：只调壳暴露的 IPC，不直接碰内核（保持权限边界）
const $ = (id) => document.getElementById(id);

const STATE_TEXT = {
  connected: "已连接平台",
  starting: "内核启动中…",
  connecting: "正在连接平台…",
  reconnecting: "连接断开，正在重连…",
  disconnected: "未连接（内核运行中）",
  stopped: "内核已停止",
  emergency_stopped: "紧急停止生效中",
  error: "连接错误",
};
const STATE_DOT = {
  connected: "green",
  starting: "yellow",
  connecting: "yellow",
  reconnecting: "yellow",
  disconnected: "grey",
  stopped: "grey",
  emergency_stopped: "red",
  error: "red",
};

function render(snapshot) {
  const status = snapshot || {};
  const connection = status.connection || {};
  const state = connection.state || "disconnected";
  const paired = Boolean(status.identity && status.identity.device_id);
  const identity = status.identity || {};

  $("conn-dot").className = `dot ${STATE_DOT[state] || "grey"}`;
  $("conn-text").textContent = STATE_TEXT[state] || state;
  $("contract-chip").textContent = `contract ${(status.shell && status.shell.contract) || status.contract || "—"}`;
  $("identity").textContent = paired
    ? `设备 ${identity.device_id} · Agent ${identity.agent_id} · 项目 ${identity.project_id ? identity.project_id.slice(0, 8) : "未授权"} · 平台 ${identity.platform_url || "—"}`
    : "未配对：在下方粘贴平台地址与配对串，或从平台页面点「接入这台电脑」";
  const lastError = (status.shell && status.shell.last_error) || connection.last_error;
  $("last-error").textContent = lastError ? `最后错误：${lastError}` : "";

  $("pair-card").style.display = paired ? "none" : "block";

  const agents = status.local_agents || [];
  $("agents").innerHTML = agents.length
    ? agents.map((agent) => `<span class="chip ${agent.state === "AVAILABLE" ? "ok" : "bad"}">${agent.adapter_id} · ${agent.state}${agent.version ? " · " + agent.version : ""}</span>`).join("")
    : '<span class="hint">尚未探测（点「重扫」）</span>';

  const tasks = status.tasks || {};
  const queue = status.queue || {};
  const current = tasks.claimed_current;
  $("tasks").textContent = `已完成 ${tasks.completed_since_start || 0} 个任务 · ${tasks.paused ? "已暂停领取" : "正在领取"}` +
    ` · 队列 ${queue.local_queue_length || 0}` +
    (current ? ` · 正在执行 ${current.id}${current.title ? "（" + current.title + "）" : ""}` : "") +
    (status.emergency_stop ? " · 紧急停止生效中" : "");
  $("pause").textContent = tasks.paused ? "恢复领取" : "暂停领取";
  $("emergency").textContent = status.emergency_stop ? "解除紧急停止" : "紧急停止";

  if (!window.shellState) window.shellState = {};
  window.shellState.paired = paired;
}

async function refresh() {
  try {
    render(await window.shell.snapshot());
  } catch (error) {
    $("last-error").textContent = `无法读取内核状态：${error.message || error}`;
  }
  try {
    const lines = await window.shell.logs();
    $("logs").textContent = lines && lines.length ? lines.join("\n") : "（内核暂无日志）";
  } catch { /* 内核未就绪时忽略 */ }
}

$("pair").addEventListener("click", async () => {
  const platform = $("platform").value.trim();
  const blob = $("blob").value.trim();
  if (!platform || !blob) {
    $("pair-hint").textContent = "平台地址与配对串都不能为空";
    return;
  }
  $("pair").disabled = true;
  $("pair-hint").textContent = "正在配对…";
  try {
    const snapshot = await window.shell.pair(platform, blob);
    render(snapshot);
    $("pair-hint").textContent = "配对成功";
  } catch (error) {
    $("pair-hint").textContent = `配对失败：${error.message || error}`;
  } finally {
    $("pair").disabled = false;
  }
});

$("rescan").addEventListener("click", async () => render(await window.shell.rescan()));
$("pause").addEventListener("click", async () => render(await window.shell.togglePause()));
$("emergency").addEventListener("click", async () => render(await window.shell.toggleEmergency()));

window.shell.onStatus((snapshot) => render(snapshot));
window.shell.onLog((line) => {
  const pre = $("logs");
  pre.textContent = `${pre.textContent}\n${line}`.split("\n").slice(-200).join("\n");
  pre.scrollTop = pre.scrollHeight;
});

void refresh();
setInterval(refresh, 4000);
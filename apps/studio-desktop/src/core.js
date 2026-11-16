"use strict";

// 本地内核的生命周期：解析路径 → 拉起 → 等握手 → 健康检查 → 优雅停止 → 崩溃重启。
// 壳只认 `studio.json` 这一个契约（contract = studio-v1），不猜端口、不读数据库。

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const CONTRACT = "studio-v1";
const READY_TIMEOUT_MS = 20000;
const MAX_RESTARTS = 3;

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM 说明进程存在但没有权限；别的错误码一律当"已退出"。
    return error && error.code === "EPERM";
  }
}

function pickPython() {
  if (process.env.SF_PYTHON && fs.existsSync(process.env.SF_PYTHON)) return process.env.SF_PYTHON;
  const local = process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, "Programs", "Python", "Python312", "python.exe")
    : "";
  if (local && fs.existsSync(local)) return local;
  return "python";
}

function locateCore(repoRoot, packaged) {
  if (packaged) {
    const exe = path.join(process.resourcesPath, "core", "synapforge-core.exe");
    return fs.existsSync(exe) ? { command: exe, args: [], kind: "packaged" } : null;
  }
  // 开发期优先用打包产物（和生产同一条路径），没有再退回源码。
  const exe = path.join(repoRoot, "dist-studio-core", "synapforge-core", "synapforge-core.exe");
  if (fs.existsSync(exe)) return { command: exe, args: [], kind: "built" };
  const script = path.join(repoRoot, "scripts", "synapforge_studio.py");
  if (fs.existsSync(script)) {
    return { command: pickPython(), args: [script], kind: "source" };
  }
  return null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class StudioCore {
  constructor({ repoRoot, packaged, onState, onLog }) {
    this.repoRoot = repoRoot;
    this.packaged = packaged;
    this.onState = onState || (() => {});
    this.onLog = onLog || (() => {});

    this.stateDir = process.env.SYNAPFORGE_STATE_DIR || path.join(os.homedir(), ".synapforge");
    this.statePath = path.join(this.stateDir, "state.db");
    this.infoPath = path.join(this.stateDir, "studio.json");
    this.logPath = path.join(this.stateDir, "logs", "studio.log");

    this.child = null;
    this.info = null;
    this.status = "idle"; // idle | starting | ready | crashed | failed | stopped
    this.external = false;
    this.wantStop = false;
    this.restarts = 0;
    this.lastError = "";
    this.lines = [];
  }

  emit(status, extra = {}) {
    this.status = status;
    this.onState({ status, info: this.info, external: this.external, error: this.lastError, ...extra });
  }

  record(line) {
    const text = String(line).replace(/\r?\n$/, "");
    if (!text) return;
    this.lines.push(text);
    if (this.lines.length > 300) this.lines.shift();
    this.onLog(text);
  }

  readHandshake() {
    try {
      const raw = fs.readFileSync(this.infoPath, "utf8");
      const info = JSON.parse(raw);
      return info && info.contract === CONTRACT ? info : null;
    } catch {
      return null;
    }
  }

  async health(info, timeout = 2500) {
    if (!info || !info.port || !info.token) return false;
    try {
      const response = await fetch(`http://127.0.0.1:${info.port}/api/studio/health`, {
        headers: { "X-Studio-Token": info.token },
        signal: AbortSignal.timeout(timeout),
      });
      if (!response.ok) return false;
      const payload = await response.json();
      return Boolean(payload && payload.ok && payload.contract === CONTRACT);
    } catch {
      return false;
    }
  }

  async request(info, method, apiPath) {
    const response = await fetch(`http://127.0.0.1:${info.port}${apiPath}`, {
      method,
      headers: { "X-Studio-Token": info.token, "Content-Type": "application/json" },
      body: method === "GET" ? undefined : "{}",
      signal: AbortSignal.timeout(3000),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  /** 外部已有实例（CLI 起的、上次没退干净的）→ 直接接管，不再起第二个。 */
  async attachIfRunning() {
    const info = this.readHandshake();
    if (!info || !pidAlive(info.pid)) return null;
    if (!(await this.health(info))) return null;
    this.info = info;
    this.external = true;
    this.emit("ready");
    return info;
  }

  async start() {
    if (this.status === "ready" && this.info && (this.external || pidAlive(this.info.pid))) {
      return this.info;
    }
    this.wantStop = false;
    this.lastError = "";
    this.emit("starting");

    const attached = await this.attachIfRunning();
    if (attached) return attached;

    const core = locateCore(this.repoRoot, this.packaged);
    if (!core) {
      this.lastError = "找不到内核。先运行 scripts\\build-studio-core.ps1，或确认 Python 可用。";
      this.emit("failed");
      throw new Error(this.lastError);
    }

    // 上次崩溃留下的握手：pid 已死，删掉再起，避免读到旧端口。
    try {
      fs.rmSync(this.infoPath, { force: true });
    } catch {
      /* 删不掉就让内核自己覆盖 */
    }
    fs.mkdirSync(path.dirname(this.logPath), { recursive: true });

    const args = [
      ...core.args,
      "--no-browser",
      "--port", "0",
      "--state", this.statePath,
      "--log-file", this.logPath,
      "--parent-pid", String(process.pid),
    ];
    this.record(`[shell] 启动内核：${core.command} ${args.join(" ")}`);

    const child = spawn(core.command, args, {
      cwd: this.repoRoot,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.child = child;
    child.stdout.on("data", (chunk) => this.record(chunk.toString("utf8")));
    child.stderr.on("data", (chunk) => this.record(chunk.toString("utf8")));
    child.on("exit", (code, signal) => this._onExit(code, signal));

    try {
      this.info = await this._waitForHandshake(child.pid);
      await this._waitForHealth(this.info);
      this.emit("ready");
      // 稳定跑满一分钟才清空重启计数，避免"起-崩-起"循环里无限重试。
      setTimeout(() => {
        if (this.status === "ready" && this.restarts > 0) this.restarts = 0;
      }, 60000);
      return this.info;
    } catch (error) {
      this.lastError = String(error && error.message ? error.message : error);
      this.emit("failed");
      throw error;
    }
  }

  async _waitForHandshake(pid) {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (!this.child || this.child.exitCode !== null) {
        throw new Error(`内核提前退出（code=${this.child ? this.child.exitCode : "?"}）`);
      }
      const info = this.readHandshake();
      if (info && info.pid === pid) return info;
      await sleep(100);
    }
    throw new Error("等内核握手超时（20s）：studio.json 未出现");
  }

  async _waitForHealth(info) {
    for (let i = 0; i < 40; i += 1) {
      if (await this.health(info)) return;
      await sleep(150);
    }
    throw new Error("内核已写握手，但 /health 不通");
  }

  _onExit(code, signal) {
    this.child = null;
    if (this.wantStop || this.external) {
      this.emit("stopped");
      return;
    }
    this.lastError = `内核退出（code=${code}${signal ? `, signal=${signal}` : ""}）`;
    this.info = null;
    if (this.restarts >= MAX_RESTARTS) {
      this.emit("failed");
      return;
    }
    this.restarts += 1;
    this.emit("crashed", { restarts: this.restarts });
    const delay = 800 * this.restarts;
    setTimeout(() => {
      if (this.wantStop) return;
      this.start().catch(() => {
        /* 状态已由 start() 内部发出去 */
      });
    }, delay);
  }

  async stop() {
    this.wantStop = true;
    if (!this.child) {
      this.emit("stopped");
      return;
    }
    const info = this.info;
    try {
      if (info) await this.request(info, "POST", "/api/studio/shutdown");
    } catch {
      /* 内核已经不听使唤了，下面直接 kill */
    }
    const exited = await this._waitExit(4000);
    if (!exited) {
      this.record("[shell] 优雅退出超时，强制结束内核");
      try {
        this.child.kill();
      } catch {
        /* ignore */
      }
      await this._waitExit(2000);
    }
    this.info = null;
    this.emit("stopped");
  }

  _waitExit(timeout) {
    const child = this.child;
    if (!child || child.exitCode !== null) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.removeListener("exit", onExit);
        resolve(false);
      }, timeout);
      const onExit = () => {
        clearTimeout(timer);
        resolve(true);
      };
      child.once("exit", onExit);
    });
  }

  async restart() {
    this.restarts = 0;
    if (this.external) {
      // 别人的进程不归壳管；只把连接重建一次。
      this.external = false;
      this.info = null;
    }
    await this.stop();
    return this.start();
  }

  async tailLog(maxBytes = 12000) {
    try {
      const stat = fs.statSync(this.logPath);
      const start = Math.max(0, stat.size - maxBytes);
      const fd = fs.openSync(this.logPath, "r");
      try {
        const buffer = Buffer.alloc(stat.size - start);
        fs.readSync(fd, buffer, 0, buffer.length, start);
        return buffer.toString("utf8");
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      return this.lines.join("\n");
    }
  }
}

module.exports = { StudioCore, pidAlive, locateCore };

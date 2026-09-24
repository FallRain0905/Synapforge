"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Copy, Cpu, KeyRound, MonitorDown, RefreshCcw, ShieldCheck, Trash2, UserPlus } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Modal, Panel, StatusPill } from "../../components/ui";
import {
  API_URL,
  DEFAULT_DEVICE_PROJECT_CAPABILITIES,
  Device,
  DeviceCredential,
  DevicePairing,
  DeviceProjectCredential,
  DeviceRuntimeState,
  createDevicePairing,
  createDeviceProjectGrant,
  errorMessage,
  listDevices,
  listOrganizations,
  pairingBlob,
  projectGrantBlob,
  revokeDevice,
  rotateDeviceToken,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

/** 配对有效期（秒）：与后端 create_device_pairing 的默认值一致。 */
const PAIRING_TTL_SECONDS = 900;

function remainingSeconds(expiresAt: string): number {
  const parsed = new Date(expiresAt).getTime();
  if (Number.isNaN(parsed)) return 0;
  return Math.max(0, Math.round((parsed - Date.now()) / 1000));
}

function countdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

/** B4：把心跳里的执行体清单压成一行可读文本；没有任何执行体时如实说明。 */
function adapterSummary(runtime: DeviceRuntimeState): string {
  const versions = Object.entries(runtime.adapter_versions || {});
  if (!versions.length) return "未探测到可用执行体";
  return versions.map(([adapter, version]) => `${adapter} ${version}`).join("、");
}

export default function DevicesPage() {
  const { notify, projects } = useWorkspace();
  const [devices, setDevices] = useState<Device[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [agentName, setAgentName] = useState("");
  const [pairing, setPairing] = useState<DevicePairing | null>(null);
  const [pairingLeft, setPairingLeft] = useState(0);
  const [credential, setCredential] = useState<DeviceCredential | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<Device | null>(null);
  const [grantTarget, setGrantTarget] = useState<Device | null>(null);
  const [grantProjectId, setGrantProjectId] = useState("");
  const [grantCapabilities, setGrantCapabilities] = useState<string[]>([...DEFAULT_DEVICE_PROJECT_CAPABILITIES]);
  const [projectCredential, setProjectCredential] = useState<DeviceProjectCredential | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextDevices, organizations] = await Promise.all([listDevices(), listOrganizations()]);
      setDevices(nextDevices);
      setOrganizationId((current) => current || organizations[0]?.id || "");
    } catch (error) {
      notify(errorMessage(error, "设备列表读取失败"));
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => { void load(); }, [load]);

  // 配对码倒计时：过期后按钮变灰，避免用户拿着废码去接入。
  useEffect(() => {
    if (!pairing) return;
    const tick = () => setPairingLeft(remainingSeconds(pairing.expires_at));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  const command = useMemo(() => {
    if (!pairing) return "";
    const name = agentName.trim() || "我的工作站";
    return `powershell -ExecutionPolicy Bypass -File .\\scripts\\connect-agent.ps1 -Url ${API_URL} -AgentName "${name}" -Pairing ${pairingBlob(pairing)}`;
  }, [pairing, agentName]);

  /**
   * 桌面端深链：安装了桌面端时，浏览器会询问是否用「Math Agent Platform」打开，
   * 桌面端随即完成配对（契约见 docs/SIDECAR_CONTRACT.md）。
   * 未安装时浏览器不会响应——所以命令行的复制路径必须一直保留（DE10）。
   */
  const desktopLink = useMemo(() => {
    if (!pairing) return "";
    const params = new URLSearchParams({ platform: API_URL, blob: pairingBlob(pairing) });
    return `map://pair?${params.toString()}`;
  }, [pairing]);

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify(`${label}已复制`);
    } catch {
      notify("复制失败：请手动选择文本复制");
    }
  };

  const handleGenerate = async () => {
    if (!organizationId) {
      notify("读不到组织信息，无法生成配对");
      return;
    }
    setBusy("pairing");
    try {
      const next = await createDevicePairing(organizationId, PAIRING_TTL_SECONDS);
      setPairing(next);
      setPairingLeft(remainingSeconds(next.expires_at));
      await load();
    } catch (error) {
      notify(errorMessage(error, "配对创建失败"));
    } finally {
      setBusy("");
    }
  };

  const handleRevoke = async (device: Device) => {
    setBusy(device.device_id);
    try {
      await revokeDevice(device.device_id);
      await load();
      notify(`已撤销设备 ${device.device_name}`);
    } catch (error) {
      notify(errorMessage(error, "设备撤销失败"));
    } finally {
      setBusy("");
      setRevokeTarget(null);
    }
  };

  const handleRotate = async (device: Device) => {
    setBusy(device.device_id);
    try {
      const next = await rotateDeviceToken(device.device_id);
      setCredential(next);
      await load();
      notify("Token 已轮换：旧 Token 立即失效");
    } catch (error) {
      notify(errorMessage(error, "Token 轮换失败"));
    } finally {
      setBusy("");
    }
  };

  const handleGrant = async (device: Device) => {
    if (!grantProjectId) {
      notify("先选一个项目");
      return;
    }
    setBusy(device.device_id);
    try {
      const next = await createDeviceProjectGrant(grantProjectId, {
        device_id: device.device_id,
        capabilities: grantCapabilities,
      });
      setProjectCredential(next);
      setGrantTarget(null);
      notify(`已授权 ${device.device_name} → 项目（能力 ${next.grant.capabilities.length} 项）`);
    } catch (error) {
      notify(errorMessage(error, "项目授权失败"));
    } finally {
      setBusy("");
    }
  };

  const workerCommand = useMemo(() => {
    if (!projectCredential) return "";
    // --url 是 agentd 的顶层参数，必须放在子命令之前（与 README 的其它示例一致）。
    return `python -X utf8 apps/agent/agentd.py --url ${API_URL} worker-run --grant ${projectGrantBlob(projectCredential)}`;
  }, [projectCredential]);

  /**
   * 授权深链（DP-2-08）：桌面端内核收到后直接写入凭据并**立刻开始领任务**，不必重启。
   * 未安装桌面端时浏览器不响应——命令行路径始终保留。
   */
  const grantDesktopLink = useMemo(() => {
    if (!projectCredential) return "";
    const params = new URLSearchParams({ platform: API_URL, blob: projectGrantBlob(projectCredential) });
    return `map://grant?${params.toString()}`;
  }, [projectCredential]);

  const activeCount = devices.filter((device) => device.status === "active").length;

  return (
    <div className="page-content" id="devices">
      <PageHeading
        hint={`${devices.length} 台设备（${activeCount} 台有效）· 接入只需三条输入：平台地址、Agent 名称、配对串`}
        actions={
          <button className="button button-primary" data-testid="devices-generate-pairing" disabled={busy === "pairing"} onClick={() => void handleGenerate()}>
            {busy === "pairing" ? <RefreshCcw size={15} className="spin" /> : <UserPlus size={15} />} 生成配对
          </button>
        }
      />

      <section className="grid grid-main-side">
        <Panel title="设备列表" subtitle="设备身份、公钥指纹与最近心跳" testId="devices-list">
          {loading ? <LoadingSkeleton rows={3} label="正在加载设备" /> : devices.length ? (
            <div className="table">
              <div className="table-head"><span>设备</span><span>平台</span><span>Agent</span><span>状态</span><span /></div>
              {devices.map((device) => (
                <div className="table-row" key={device.device_id} data-testid={`devices-device-${device.device_id}`}>
                  <div className="table-title">
                    <strong>{device.device_name}</strong>
                    <small>{device.device_id} · 指纹 {device.public_key_fingerprint.slice(0, 12)}… · 最近心跳 {device.last_seen ? formatTime(device.last_seen) : "从未"}</small>
                    <small data-testid={`devices-runtime-${device.device_id}`}>
                      {device.runtime
                        ? `执行体：${adapterSummary(device.runtime)} · 队列 ${device.runtime.local_queue_length} · 会话 ${device.runtime.user_session_state} · 上报于 ${formatTime(device.runtime.reported_at)}`
                        : "尚无心跳上报：设备接入后由内核每 15 秒上报一次可用执行体与队列长度"}
                    </small>
                  </div>
                  <span className="chip" data-label="平台">{device.platform} · v{device.runtime?.agent_version || device.agent_version}</span>
                  <span className="hint" data-label="Agent">{device.agent_id}</span>
                  <span data-label="状态"><StatusPill status={device.status === "active" ? "APPROVED" : "REVOKED"} label={device.status === "active" ? "active" : "revoked"} /></span>
                  <div className="row-actions">
                    <button
                      className="text-button"
                      disabled={device.status !== "active" || busy === device.device_id}
                      data-testid={`devices-grant-${device.device_id}`}
                      onClick={() => { setGrantTarget(device); setGrantProjectId(projects[0]?.id ?? ""); }}
                    >
                      <ShieldCheck size={13} /> 授权到项目
                    </button>
                    <button
                      className="text-button"
                      disabled={device.status !== "active" || busy === device.device_id}
                      data-testid={`devices-rotate-${device.device_id}`}
                      onClick={() => void handleRotate(device)}
                    >
                      <KeyRound size={13} /> 轮换 Token
                    </button>
                    <button
                      className="text-button"
                      disabled={device.status !== "active" || busy === device.device_id}
                      data-testid={`devices-revoke-${device.device_id}`}
                      onClick={() => setRevokeTarget(device)}
                    >
                      <Trash2 size={13} /> 撤销
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-cta" data-testid="devices-empty">
              <Cpu size={22} />
              <strong>还没有设备接入</strong>
              <small>点右上角「生成配对」，把命令复制到要接入的机器上执行即可；不需要手工编造任何 ID。</small>
            </div>
          )}
        </Panel>

        <Panel title="桌面端（推荐）" subtitle="Windows 安装包 · 托盘常驻，自动连接与执行" testId="devices-desktop-download">
          <p className="hint" style={{ marginBottom: 10 }}>
            装上它以后不用碰命令行：托盘图标常驻后台，生成配对后点「接入这台电脑」就会自动完成配对并开始领任务。
          </p>
          <div className="link-row">
            <a
              className="button button-primary"
              href="/downloads/synapforge-setup-0.2.1-x64.exe"
              data-testid="devices-download"
              title="Windows 安装包（约 150MB，未签名）"
            >
              <MonitorDown size={15} /> 下载桌面端（Windows）
            </a>
            <span className="hint">安装包未签名：首次运行 Windows 会提示"未知发布者"，选「更多信息 → 仍要运行」。</span>
          </div>
        </Panel>

        <Panel title="接入流程" subtitle="三条命令之内完成" testId="devices-guide">
          <div className="list">
            <div className="list-item list-item-static">
              <span className="icon-tile icon-tile-blue">1</span>
              <div className="item-copy"><strong>生成配对</strong><small>平台返回一次性配对码与挑战，15 分钟内有效</small></div>
            </div>
            <div className="list-item list-item-static">
              <span className="icon-tile icon-tile-blue">2</span>
              <div className="item-copy"><strong>在被接入的机器上执行命令</strong><small>脚本自动完成：登记 Agent → 生成 Ed25519 密钥 → 设备注册签名 → Token 存入凭据管理器</small></div>
            </div>
            <div className="list-item list-item-static">
              <span className="icon-tile icon-tile-blue">3</span>
              <div className="item-copy"><strong>启动 Gateway 连接</strong><small>脚本会打印可直接粘贴的命令；连接断开后 90 秒内平台标记为离线</small></div>
            </div>
          </div>
          <div className="divider" />
          <div className="hint">
            安全约定：私钥只留在接入方机器；设备 Token 只在注册响应里出现一次，平台只保存哈希，无法回显；
            公钥指纹用于事后核对（一个公钥只能注册一台设备）。
          </div>
        </Panel>
      </section>

      {pairing && (
        <Modal
          title="设备配对"
          subtitle={pairingLeft > 0 ? `配对码 ${countdown(pairingLeft)} 后失效，且只能用一次` : "配对码已失效，请重新生成"}
          wide
          testId="devices-pairing-modal"
          onClose={() => setPairing(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setPairing(null)}>关闭</button>
              <button
                className="button button-primary"
                disabled={pairingLeft <= 0}
                data-testid="devices-copy-command"
                onClick={() => void copy(command, "接入命令")}
              >
                <Copy size={15} /> 复制接入命令
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <label className="field"><span>Agent 名称（写进命令里，可在接入机器上改）</span>
              <input
                value={agentName}
                placeholder="例如：我的工作站"
                data-testid="devices-agent-name"
                onChange={(event) => setAgentName(event.target.value)}
              />
            </label>

            <div className="form-row">
              <span className="chip">配对码 <code data-testid="devices-pairing-code">{pairing.pairing_code}</code></span>
              <button className="text-button" onClick={() => void copy(pairing.pairing_code, "配对码")}><Copy size={13} /> 复制</button>
              <span className={`chip ${pairingLeft > 0 ? "" : "status-red"}`}>剩余 {countdown(pairingLeft)}</span>
            </div>

            <div className="field">
              <span>方式一：用桌面端接入（推荐）</span>
              <a
                className={`button button-primary ${pairingLeft > 0 ? "" : "button-disabled"}`.trim()}
                href={pairingLeft > 0 ? desktopLink : undefined}
                aria-disabled={pairingLeft <= 0}
                data-testid="devices-open-desktop"
                style={{ justifySelf: "start", pointerEvents: pairingLeft > 0 ? "auto" : "none", opacity: pairingLeft > 0 ? 1 : 0.55 }}
              >
                <MonitorDown size={15} /> 接入这台电脑
              </a>
              <div className="hint">
                会调起已安装的桌面端并自动完成配对，之后它会在后台自动连接、自动执行任务。
                未安装时浏览器不会响应——<a className="inline-link" href="/downloads/synapforge-setup-0.2.1-x64.exe" data-testid="devices-download">下载桌面端安装包（Windows，约 150MB，未签名）</a>，装完重试；或用下面的方式二。
              </div>
            </div>

            <div className="field">
              <span>方式二：命令行接入（需要仓库路径，或把 scripts/connect-agent.ps1 一起拷过去）</span>
              <pre className="code-block" data-testid="devices-command">{command}</pre>
              <div className="hint">
                命令里的平台地址是 <code>{API_URL}</code>；如果接入机器不在同一台主机上，把它改成该机器能访问到的地址。
              </div>
            </div>

            <div className="hint">
              <ShieldCheck size={13} style={{ display: "inline", marginRight: 4 }} />
              配对串里包含一次性配对码与挑战，等同于短期凭证：不要贴到公开渠道；过期后用「生成配对」重新来一次。
            </div>
          </div>
        </Modal>
      )}

      {grantTarget && (
        <Modal
          title="授权到项目"
          subtitle={`${grantTarget.device_name} · ${grantTarget.agent_id}`}
          wide
          testId="devices-grant-modal"
          onClose={() => setGrantTarget(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setGrantTarget(null)}>取消</button>
              <button
                className="button button-primary"
                disabled={!grantProjectId || busy === grantTarget.device_id}
                data-testid="devices-grant-submit"
                onClick={() => void handleGrant(grantTarget)}
              >
                {busy === grantTarget.device_id ? <RefreshCcw size={15} className="spin" /> : <ShieldCheck size={15} />} 授权
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="hint">
              仅接入设备身份还不够：Agent 要出现在项目看板并领取任务，需要项目范围能力。
              授权会同时写入设备级授权与 Agent 级授权，并生成一次性项目 Token。
            </div>
            <label className="field"><span>项目</span>
              <select value={grantProjectId} data-testid="devices-grant-project" onChange={(event) => setGrantProjectId(event.target.value)}>
                {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
              </select>
            </label>
            <div className="field">
              <span>能力（默认全部 {DEFAULT_DEVICE_PROJECT_CAPABILITIES.length} 项）</span>
              <div className="chips">
                {DEFAULT_DEVICE_PROJECT_CAPABILITIES.map((capability) => {
                  const active = grantCapabilities.includes(capability);
                  return (
                    <button
                      key={capability}
                      type="button"
                      className={`chip ${active ? "status-blue" : ""}`.trim()}
                      data-testid={`devices-grant-cap-${capability}`}
                      onClick={() => setGrantCapabilities((current) =>
                        current.includes(capability) ? current.filter((item) => item !== capability) : [...current, capability])}
                    >
                      {capability}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </Modal>
      )}

      {projectCredential && (
        <Modal
          title="项目 Token（只显示这一次）"
          subtitle={`${projectCredential.grant.agent_id} → 项目 ${projectCredential.grant.project_id.slice(0, 8)}`}
          wide
          testId="devices-project-token-modal"
          onClose={() => setProjectCredential(null)}
          actions={
            <>
              <button className="button button-secondary" data-testid="devices-project-token-close" onClick={() => setProjectCredential(null)}>我已保存</button>
              <button className="button button-primary" onClick={() => void copy(projectCredential.project_token, "项目 Token")}>
                <Copy size={15} /> 复制 Token
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="pack-missing">
              <strong>这是最后一次显示</strong>
              <span>平台只保存该 Token 的 SHA-256，关闭后无法查看；能力 {projectCredential.grant.capabilities.length} 项，过期时间 {formatTime(projectCredential.grant.expires_at)}。</span>
            </div>
            <pre className="code-block" data-testid="devices-project-token-value">{projectCredential.project_token}</pre>
            <div className="field">
              <span>装了桌面端的话，直接交给本机内核（推荐）</span>
              <a className="code-block" data-testid="devices-grant-deeplink" href={grantDesktopLink}>
                <MonitorDown size={13} style={{ display: "inline", marginRight: 6 }} />点这里把授权交给本机桌面端
              </a>
              <div className="hint">
                桌面端内核会立刻开始领取任务，不需要重启，也不用手工复制 Token；
                没装桌面端时这个链接不会有反应，用下面的命令行即可。
              </div>
            </div>
            <div className="field">
              <span>启动该 Agent 的任务循环（把授权串一次性交给它，之后从凭据管理器读取）</span>
              <pre className="code-block" data-testid="devices-worker-command">{workerCommand}</pre>
              <div className="hint">
                授权串里含项目 Token，等同短期凭证：不要贴到公开渠道。Agent 侧会把它存进 Windows 凭据管理器
                （<code>MathAgentPlatform/project-token/&lt;project_id&gt;</code>），不写入本地状态库。
              </div>
            </div>
          </div>
        </Modal>
      )}

      {credential && (
        <Modal
          title="设备 Token（只显示这一次）"
          subtitle={`${credential.device.device_name} · ${credential.device.device_id}`}
          wide
          testId="devices-token-modal"
          onClose={() => setCredential(null)}
          actions={
            <>
              <button className="button button-secondary" data-testid="devices-token-close" onClick={() => setCredential(null)}>我已保存</button>
              <button className="button button-primary" onClick={() => void copy(credential.device_token, "Token")}>
                <Copy size={15} /> 复制 Token
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="pack-missing">
              <strong>这是最后一次显示</strong>
              <span>平台只保存 Token 的 SHA-256，关闭后无法再次查看；旧 Token 在轮换生效时已立即失效。</span>
            </div>
            <pre className="code-block" data-testid="devices-token-value">{credential.device_token}</pre>
            <div className="hint">
              <CheckCircle2 size={13} style={{ display: "inline", marginRight: 4 }} />
              正常路径不需要手工复制：<code>connect-agent.ps1</code> 会把 Token 直接写入 Windows 凭据管理器
              （<code>MathAgentPlatform/device-token/{credential.device.device_id}</code>）。
            </div>
          </div>
        </Modal>
      )}

      {revokeTarget && (
        <ConfirmDialog
          title="撤销设备？"
          description={
            <>
              <strong>{revokeTarget.device_name}</strong>（{revokeTarget.device_id}）
              <div style={{ marginTop: 6 }}>撤销后该设备的活跃 Gateway 连接会被断开，且无法再用原 Token 连接；Agent 会被标记为离线。此操作不可撤销，需要重新配对才能恢复。</div>
            </>
          }
          confirmLabel="撤销设备"
          tone="danger"
          busy={busy === revokeTarget.device_id}
          testId="devices-revoke-confirm"
          onCancel={() => setRevokeTarget(null)}
          onConfirm={() => void handleRevoke(revokeTarget)}
        />
      )}
    </div>
  );
}
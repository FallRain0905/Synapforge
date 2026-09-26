"use client";

/**
 * 渠道管理（管理员）。
 *
 * 平台把一个 OpenAI 兼容上游包装成「渠道」：管理员录入 base_url + api_key + 模型列表，
 * 全员即获得平台免费额度——成员按**模型名**路由到渠道，渠道本身对成员不可见。
 *
 * 几条硬约束（踩过，或不遵守会出事）：
 * - **api_key 明文只在服务端**：列表只回 `key_hint`（末 4 位），页面绝不显示明文；
 *   编辑时该输入框留空 = 保持不变（服务端语义：缺省/null 不变、空串清空）。
 * - **检测/测速打的是真实上游**：会消耗真实额度（max_tokens=1），失败也如实回填，不美化。
 * - 非管理员访问：服务端返回 403，这里给出明确提示而不是空白页。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  Gauge,
  KeyRound,
  Pencil,
  Plus,
  RefreshCcw,
  ShieldAlert,
  Trash2,
  Zap,
} from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Modal, Panel } from "../../components/ui";
import {
  LlmChannel,
  LlmChannelInput,
  LlmCheckResult,
  LlmSpeedResult,
  LlmUsage,
  checkLlmChannel,
  createLlmChannel,
  deleteLlmChannel,
  errorMessage,
  getCurrentAccount,
  getLlmUsage,
  listLlmChannels,
  setLlmQuota,
  speedTestLlmChannel,
  updateLlmChannel,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

type FormState = {
  name: string;
  base_url: string;
  api_key: string;
  models: string;
  priority: number;
  enabled: boolean;
};

const EMPTY_FORM: FormState = { name: "", base_url: "", api_key: "", models: "", priority: 100, enabled: true };

/** 逗号/换行/空格分隔的模型名 → 去空数组（支持一次粘贴多个）。 */
function parseModels(raw: string): string[] {
  return raw
    .split(/[,，\n\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export default function ChannelsPage() {
  const { notify } = useWorkspace();
  const [checkingAccess, setCheckingAccess] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [channels, setChannels] = useState<LlmChannel[]>([]);
  const [usage, setUsage] = useState<LlmUsage | null>(null);
  const [defaultQuota, setDefaultQuota] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [checkResults, setCheckResults] = useState<Record<string, LlmCheckResult>>({});
  const [speedResults, setSpeedResults] = useState<Record<string, LlmSpeedResult>>({});
  const [editing, setEditing] = useState<LlmChannel | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<LlmChannel | null>(null);
  const [quotaEditing, setQuotaEditing] = useState<string>("");
  const [quotaValue, setQuotaValue] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const [list, overview] = await Promise.all([listLlmChannels(), getLlmUsage().catch(() => null)]);
      setChannels(list.channels);
      setDefaultQuota(list.default_quota_tokens);
      if (overview) setUsage(overview);
    } catch (error) {
      notify(errorMessage(error, "渠道列表读取失败"));
    } finally {
      setLoading(false);
    }
  }, [notify]);

  // 先确认身份：非管理员给明确提示，而不是让页面空着（服务端也会 403 兜底）
  useEffect(() => {
    void (async () => {
      try {
        const account = await getCurrentAccount();
        setIsAdmin(Boolean(account.is_admin));
        if (account.is_admin) await load();
      } catch (error) {
        notify(errorMessage(error, "身份校验失败"));
      } finally {
        setCheckingAccess(false);
        setLoading(false);
      }
    })();
  }, [load, notify]);

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setCreating(true);
  };

  const openEdit = (channel: LlmChannel) => {
    setForm({
      name: channel.name,
      base_url: channel.base_url,
      api_key: "", // 留空 = 不变
      models: channel.models.join(", "),
      priority: channel.priority,
      enabled: channel.enabled,
    });
    setEditing(channel);
  };

  const submit = async () => {
    const models = parseModels(form.models);
    if (!form.name.trim()) return notify("请填渠道名称");
    if (!form.base_url.trim()) return notify("请填 Base URL（OpenAI 兼容）");
    if (!models.length) return notify("至少填一个模型名");
    if (creating && !form.api_key.trim()) return notify("新建渠道必须填 API Key");
    setSaving(true);
    try {
      const payload: LlmChannelInput = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        models,
        priority: Number(form.priority) || 100,
        enabled: form.enabled,
      };
      if (editing) {
        // 留空 = 不变：不把 api_key 放进 payload（避免把 key 清空）
        if (form.api_key.trim()) payload.api_key = form.api_key.trim();
        await updateLlmChannel(editing.id, payload);
        notify(`渠道「${payload.name}」已保存`);
      } else {
        payload.api_key = form.api_key.trim();
        await createLlmChannel(payload);
        notify(`渠道「${payload.name}」已创建`);
      }
      setCreating(false);
      setEditing(null);
      await load();
    } catch (error) {
      notify(errorMessage(error, "保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const runCheck = async (channel: LlmChannel) => {
    setBusyId(channel.id);
    try {
      const result = await checkLlmChannel(channel.id);
      setCheckResults((current) => ({ ...current, [channel.id]: result }));
      notify(result.ok ? `「${channel.name}」检测通过（${result.latency_ms} ms）` : `「${channel.name}」检测未通过`);
      await load();
    } catch (error) {
      notify(errorMessage(error, "检测失败"));
    } finally {
      setBusyId("");
    }
  };

  const runSpeed = async (channel: LlmChannel) => {
    setBusyId(channel.id);
    try {
      const result = await speedTestLlmChannel(channel.id, 3);
      setSpeedResults((current) => ({ ...current, [channel.id]: result }));
      notify(`「${channel.name}」测速：${result.ok_rounds}/${result.rounds.length} 轮通过`);
      await load();
    } catch (error) {
      notify(errorMessage(error, "测速失败"));
    } finally {
      setBusyId("");
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    try {
      await deleteLlmChannel(pendingDelete.id);
      notify(`渠道「${pendingDelete.name}」已删除（历史用量流水保留）`);
      setPendingDelete(null);
      await load();
    } catch (error) {
      notify(errorMessage(error, "删除失败"));
    }
  };

  const saveQuota = async (memberId: string) => {
    const parsed = Number(quotaValue);
    if (!Number.isFinite(parsed)) return notify("额度必须是数字（负数 = 不限量）");
    try {
      await setLlmQuota(memberId, Math.trunc(parsed));
      notify(`已更新 ${memberId} 的额度`);
      setQuotaEditing("");
      await load();
    } catch (error) {
      notify(errorMessage(error, "额度保存失败"));
    }
  };

  if (checkingAccess) {
    return (
      <div className="page-content" id="channels">
        <PageHeading hint="平台代管的 OpenAI 兼容上游与全员免费额度" />
        <LoadingSkeleton rows={3} />
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="page-content" id="channels">
        <PageHeading hint="平台代管的 OpenAI 兼容上游与全员免费额度" />
        <Panel title="仅管理员可访问" subtitle="渠道里存着上游密钥，因此只对管理员开放">
          <div className="empty-state" data-testid="channels-forbidden">
            <ShieldAlert size={18} />
            <div className="item-copy">
              <strong>你不是管理员</strong>
              <small>渠道配置涉及上游 API Key，需要管理员权限。你的免费额度可在「空间设置」查看。</small>
            </div>
          </div>
        </Panel>
      </div>
    );
  }

  const formOpen = creating || Boolean(editing);

  return (
    <div className="page-content" id="channels">
      <PageHeading
        hint="平台代管的 OpenAI 兼容上游与全员免费额度"
        actions={
          <button className="button button-primary" data-testid="channel-create" onClick={openCreate}>
            <Plus size={15} /> 新建渠道
          </button>
        }
      />

      <section className="metrics-grid">
        <Metric label="渠道" value={channels.length} detail={`启用 ${channels.filter((c) => c.enabled).length}`} />
        <Metric label="调用次数" value={usage?.totals.requests ?? "—"} detail={`成功 ${usage?.totals.ok_requests ?? "—"}`} />
        <Metric
          label="消耗 token"
          value={(usage?.totals.total_tokens ?? 0).toLocaleString()}
          detail="按上游 usage 记账"
        />
        <Metric label="默认额度" value={defaultQuota.toLocaleString()} detail="PLATFORM_LLM_FREE_TOKENS_PER_MEMBER" />
      </section>

      <Panel
        title="渠道列表"
        subtitle="成员按模型名路由到这里；api_key 只存库，页面只显示末 4 位"
        testId="channel-list"
      >
        {loading ? (
          <LoadingSkeleton rows={3} />
        ) : channels.length ? (
          <div className="list">
            {channels.map((channel) => {
              const check = checkResults[channel.id];
              const speed = speedResults[channel.id];
              return (
                <div className="list-item list-item-static" key={channel.id} data-testid={`channel-${channel.id}`}>
                  <span className={`icon-tile ${channel.enabled ? "icon-tile-green" : ""}`}>
                    <KeyRound size={15} />
                  </span>
                  <div className="item-copy">
                    <strong>
                      {channel.name}
                      <span className={`badge ${channel.enabled ? "status-green" : "status-neutral"}`} style={{ marginLeft: 8 }}>
                        {channel.enabled ? "启用" : "停用"}
                      </span>
                      <span className="badge status-neutral" style={{ marginLeft: 6 }}>优先级 {channel.priority}</span>
                    </strong>
                    <small>{channel.base_url}</small>
                    <small>
                      模型：{channel.models.join("、")} · Key：{channel.key_hint ? `••••${channel.key_hint}` : "未设置"}
                    </small>
                    {channel.last_check_at && (
                      <small>
                        最近检测 {formatTime(channel.last_check_at)} ·{" "}
                        {channel.last_check_ok ? "通过" : "未通过"}
                        {channel.last_latency_ms ? ` · ${channel.last_latency_ms} ms` : ""}
                        {channel.last_check_detail ? ` · ${channel.last_check_detail}` : ""}
                      </small>
                    )}
                    {check && <small data-testid={`check-result-${channel.id}`}>本次检测：{check.detail}</small>}
                    {speed && (
                      <small data-testid={`speed-result-${channel.id}`}>
                        测速 {speed.ok_rounds}/{speed.rounds.length} 轮通过
                        {speed.avg_ms !== null ? ` · 均值 ${speed.avg_ms} ms（${speed.min_ms}–${speed.max_ms} ms）` : ""}
                      </small>
                    )}
                  </div>
                  <div className="panel-heading-actions">
                    <button
                      className="button button-secondary"
                      disabled={busyId === channel.id}
                      onClick={() => void runCheck(channel)}
                      data-testid={`channel-check-${channel.id}`}
                    >
                      {busyId === channel.id ? <RefreshCcw size={14} className="spin" /> : <Zap size={14} />} 检测
                    </button>
                    <button
                      className="button button-secondary"
                      disabled={busyId === channel.id}
                      onClick={() => void runSpeed(channel)}
                      data-testid={`channel-speed-${channel.id}`}
                    >
                      <Gauge size={14} /> 测速
                    </button>
                    <button className="app-icon" aria-label="编辑" onClick={() => openEdit(channel)} data-testid={`channel-edit-${channel.id}`}>
                      <Pencil size={15} />
                    </button>
                    <button className="app-icon" aria-label="删除" onClick={() => setPendingDelete(channel)} data-testid={`channel-delete-${channel.id}`}>
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState>
            还没有渠道。点右上角「新建渠道」录入一个 OpenAI 兼容上游，成员即可用平台额度调用。
          </EmptyState>
        )}
      </Panel>

      <Panel
        title="用量总览"
        subtitle="按成员聚合（额度为负 = 不限量）"
        testId="llm-usage"
        actions={
          <button className="button button-secondary" onClick={() => void load()}>
            <RefreshCcw size={14} /> 刷新
          </button>
        }
      >
        {usage && usage.members.length ? (
          <div className="list">
            {usage.members.map((entry) => (
              <div className="list-item list-item-static" key={entry.member_id}>
                <span className="icon-tile">
                  <Activity size={15} />
                </span>
                <div className="item-copy">
                  <strong>{entry.member_id}</strong>
                  <small>
                    {entry.requests} 次调用 · 消耗 {entry.log_tokens.toLocaleString()} token
                    {entry.unlimited
                      ? " · 不限量"
                      : entry.token_limit !== null
                        ? ` · 额度 ${entry.token_limit.toLocaleString()}（已用 ${(entry.tokens_used ?? 0).toLocaleString()}）`
                        : ""}
                  </small>
                </div>
                {quotaEditing === entry.member_id ? (
                  <div className="form-row">
                    <input
                      type="number"
                      value={quotaValue}
                      onChange={(event) => setQuotaValue(event.target.value)}
                      placeholder="-1 表示不限量"
                      data-testid={`quota-input-${entry.member_id}`}
                    />
                    <button className="button button-primary" onClick={() => void saveQuota(entry.member_id)}>保存</button>
                    <button className="button button-secondary" onClick={() => setQuotaEditing("")}>取消</button>
                  </div>
                ) : (
                  <button
                    className="button button-secondary"
                    onClick={() => {
                      setQuotaEditing(entry.member_id);
                      setQuotaValue(String(entry.token_limit ?? defaultQuota));
                    }}
                    data-testid={`quota-edit-${entry.member_id}`}
                  >
                    调整额度
                  </button>
                )}
              </div>
            ))}
            <div className="hint">负数表示不限量；调整后立刻生效。上游不给 usage 时按字符数粗估（约 4 字符 = 1 token）。</div>
          </div>
        ) : (
          <EmptyState>还没有调用记录。成员用平台额度调一次对话后，这里就会出现流水。</EmptyState>
        )}
      </Panel>

      {formOpen && (
        <Modal
          title={editing ? `编辑渠道「${editing.name}」` : "新建渠道"}
          subtitle="OpenAI 兼容上游；成员的请求按模型名路由到这里"
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          wide
          testId="channel-form"
          actions={
            <>
              <button className="button button-secondary" onClick={() => { setCreating(false); setEditing(null); }}>取消</button>
              <button className="button button-primary" disabled={saving} onClick={() => void submit()} data-testid="channel-save">
                {saving ? <RefreshCcw size={15} className="spin" /> : null} 保存
              </button>
            </>
          }
        >
          <div className="form-grid">
            <label className="field">
              <span>渠道名称（唯一）</span>
              <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：百炼-主力" data-testid="channel-name" />
            </label>
            <label className="field">
              <span>Base URL（OpenAI 兼容，自动补 /v1）</span>
              <input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://example.invalid/v1" data-testid="channel-base-url" />
            </label>
            <label className="field">
              <span>API Key {editing && <small>（当前 ••••{editing.key_hint || "无"}；留空即不变）</small>}</span>
              <input
                type="password"
                value={form.api_key}
                onChange={(event) => setForm({ ...form, api_key: event.target.value })}
                placeholder={editing ? "留空 = 保持不变" : "sk-..."}
                data-testid="channel-api-key"
              />
            </label>
            <label className="field">
              <span>模型名（逗号分隔，成员按此路由）</span>
              <input value={form.models} onChange={(event) => setForm({ ...form, models: event.target.value })} placeholder="model-a, model-b" data-testid="channel-models" />
            </label>
            <label className="field">
              <span>优先级（数字小者优先）</span>
              <input type="number" value={form.priority} onChange={(event) => setForm({ ...form, priority: Number(event.target.value) })} />
            </label>
            <label className="field">
              <span>启用</span>
              <div className="form-row">
                <input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
                <small>停用后成员无法路由到这条渠道（历史流水保留）</small>
              </div>
            </label>
          </div>
          <div className="hint">密钥只落库、不回传明文；检测与测速会对上游发一次真实的最小请求（max_tokens=1）。</div>
        </Modal>
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`删除渠道「${pendingDelete.name}」？`}
          description={
            <>
              删除后成员无法再路由到这条渠道；<strong>历史用量流水会保留</strong>（归到「已删渠道」）。
              正在使用该模型的调用会立刻失败。
            </>
          }
          confirmLabel="删除"
          tone="danger"
          onConfirm={() => void confirmDelete()}
          onCancel={() => setPendingDelete(null)}
          testId="channel-delete-confirm"
        />
      )}
    </div>
  );
}

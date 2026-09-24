"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Eye, EyeOff, Key, RefreshCcw, Save, Settings2, Zap } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { EmptyState, Metric, Panel } from "../../components/ui";
import {
  AiProbeReport,
  errorMessage,
  getAiSettings,
  getPlatformMetrics,
  getPlatformQuota,
  getProjectUsage,
  saveAiSettings,
  testAiConnections,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

type Metrics = { counts: Record<string, number>; storage_bytes: number; pending_outbox: number; quotas: Record<string, number> };
type Quota = { limits: Record<string, number>; cost_rates: Record<string, number>; resources: string[] };
type Usage = { counts: Record<string, number>; storage: Record<string, number>; estimated_cost: { amount: number; currency: string; note: string }; quotas: { used: Record<string, number>; exceeded: Record<string, boolean> } };

type AiSettings = {
  member_id: string;
  llm_api_key: string;
  llm_base_url: string;
  llm_model: string;
  embedding_api_key: string;
  embedding_base_url: string;
  embedding_model: string;
  embedding_dimensions: number;
  mineru_api_key: string;
};

const EMPTY_SETTINGS: AiSettings = {
  member_id: "", llm_api_key: "", llm_base_url: "", llm_model: "",
  embedding_api_key: "", embedding_base_url: "", embedding_model: "", embedding_dimensions: 1024, mineru_api_key: "",
};

const PROVIDER_PRESETS: Record<string, { label: string; llm_base_url: string; embedding_base_url: string; llm_model: string; embedding_model: string }> = {
  deepseek: { label: "DeepSeek", llm_base_url: "https://api.deepseek.com/v1", embedding_base_url: "", llm_model: "deepseek-chat", embedding_model: "" },
  siliconflow: { label: "SiliconFlow", llm_base_url: "https://api.siliconflow.cn/v1", embedding_base_url: "https://api.siliconflow.cn/v1", llm_model: "deepseek-ai/DeepSeek-V3", embedding_model: "BAAI/bge-m3" },
  qwen: { label: "通义千问", llm_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", embedding_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", llm_model: "qwen-plus", embedding_model: "text-embedding-v3" },
  openai: { label: "OpenAI", llm_base_url: "https://api.openai.com/v1", embedding_base_url: "https://api.openai.com/v1", llm_model: "gpt-4o-mini", embedding_model: "text-embedding-3-small" },
};

export default function SettingsPage() {
  const { projectId, dashboard, notify } = useWorkspace();
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [aiSettings, setAiSettings] = useState<AiSettings>(EMPTY_SETTINGS);
  const [isConfigured, setIsConfigured] = useState({ llm: false, embedding: false, mineru: false });
  const [saving, setSaving] = useState(false);
  const [showKeys, setShowKeys] = useState({ llm: false, embedding: false, mineru: false });
  const [testing, setTesting] = useState(false);
  const [testResults, setTestResults] = useState<AiProbeReport | null>(null);
  const [form, setForm] = useState<AiSettings>(EMPTY_SETTINGS);

  useEffect(() => {
    void (async () => {
      try {
        // 经 api.ts 统一发请求：登录后自动带会话令牌（原先这里裸 fetch，会缺 Authorization）
        const [metricsData, quotaData] = await Promise.all([
          getPlatformMetrics<Metrics>().catch(() => null),
          getPlatformQuota<Quota>().catch(() => null),
        ]);
        if (metricsData) setMetrics(metricsData);
        if (quotaData) setQuota(quotaData);
        if (projectId) {
          const usageData = await getProjectUsage<Usage>(projectId).catch(() => null);
          if (usageData) setUsage(usageData);
        }
        const settings = await getAiSettings().catch(() => null);
        if (settings) {
          setIsConfigured({
            llm: settings.llm_api_key === "configured",
            embedding: settings.embedding_api_key === "configured",
            mineru: settings.mineru_api_key === "configured",
          });
          setForm({
            ...EMPTY_SETTINGS,
            llm_base_url: settings.llm_base_url || "",
            llm_model: settings.llm_model || "",
            embedding_base_url: settings.embedding_base_url || "",
            embedding_model: settings.embedding_model || "",
            embedding_dimensions: settings.embedding_dimensions || 1024,
          });
        }
      } catch {
        // 离线展示已有信息
      }
    })();
  }, [projectId]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const saved = await saveAiSettings({
        ...form,
        llm_api_key: form.llm_api_key || "",
        embedding_api_key: form.embedding_api_key || "",
        mineru_api_key: form.mineru_api_key || "",
      });
      setIsConfigured({
        llm: Boolean(saved.llm_api_key),
        embedding: Boolean(saved.embedding_api_key),
        mineru: Boolean(saved.mineru_api_key),
      });
      setForm((current) => ({ ...current, llm_api_key: "", embedding_api_key: "", mineru_api_key: "" }));
      notify("AI 配置已保存（密钥已加密存储）");
    } catch (error) {
      // 服务端原因如实展示，而不是笼统"保存失败"。
      notify(errorMessage(error, "保存失败"));
    } finally {
      setSaving(false);
    }
  };

  /** 测试连接：用已保存的凭据各发一次最小请求（服务端代发，浏览器不接触密钥）。 */
  const handleTest = async () => {
    setTesting(true);
    setTestResults(null);
    try {
      const results = await testAiConnections();
      setTestResults(results);
      const okCount = Object.values(results).filter((item) => item.ok).length;
      notify(`测试完成：${okCount}/${Object.keys(results).length} 项可用`);
    } catch (error) {
      notify(errorMessage(error, "测试连接失败"));
    } finally {
      setTesting(false);
    }
  };

  const applyPreset = (preset: keyof typeof PROVIDER_PRESETS) => {
    const config = PROVIDER_PRESETS[preset];
    setForm((current) => ({
      ...current,
      llm_base_url: config.llm_base_url,
      embedding_base_url: config.embedding_base_url,
      llm_model: config.llm_model,
      embedding_model: config.embedding_model,
    }));
  };

  return (
    <div className="page-content" id="settings">
      <PageHeading hint="组织、项目、配额与 AI 凭据" actions={<span className="badge status-neutral">{dashboard.project.competition_pack || "—"}</span>} />

      <section className="metrics-grid">
        <Metric label="组织" value={metrics?.counts.organizations ?? "—"} detail="多租户边界" />
        <Metric label="项目" value={metrics?.counts.projects ?? "—"} detail={`任务 ${metrics?.counts.tasks ?? "—"}`} />
        <Metric label="成果物" value={metrics?.counts.artifacts ?? "—"} detail={`已批准 ${metrics?.counts.approved_artifacts ?? "—"}`} />
        <Metric label="存储" value={metrics?.storage_bytes?.toLocaleString() ?? "—"} detail={`outbox ${metrics?.pending_outbox ?? "—"}`} />
      </section>

      <section className="grid grid-main-side">
        <Panel
          title="AI 模型配置"
          subtitle="团队自配第三方模型（LLM + Embedding + MinerU）；密钥加密存储，只属于你"
          testId="ai-settings-panel"
          actions={
            <>
              <button className="button button-secondary" data-testid="ai-settings-test" disabled={testing} onClick={() => void handleTest()}>
                {testing ? <RefreshCcw size={15} className="spin" /> : <Zap size={15} />} 测试连接
              </button>
              <button className="button button-primary" data-testid="ai-settings-save" disabled={saving} onClick={() => void handleSave()}>
                {saving ? <RefreshCcw size={15} className="spin" /> : <Save size={15} />} 保存配置
              </button>
            </>
          }
        >
          {testResults && (
            <div className="list" data-testid="ai-settings-test-results" style={{ marginBottom: 12 }}>
              {Object.entries(testResults).map(([name, result]) => (
                <div className="list-item list-item-static" key={name}>
                  <span className={`icon-tile ${result.ok ? "icon-tile-green" : "icon-tile-red"}`}>
                    {result.ok ? <CheckCircle2 size={14} /> : <Zap size={14} />}
                  </span>
                  <div className="item-copy">
                    <strong>{name === "llm" ? "LLM" : name === "embedding" ? "Embedding" : "MinerU"}</strong>
                    <small>{result.detail}</small>
                  </div>
                  <span className={`badge ${result.ok ? "status-green" : "status-red"}`}>{result.ok ? "可用" : "不可用"}</span>
                </div>
              ))}
              <div className="hint">测试用的是已保存的凭据；刚改动的输入请先保存再测。</div>
            </div>
          )}
          <div className="chips">
            {Object.entries(PROVIDER_PRESETS).map(([key, preset]) => (
              <button key={key} type="button" className="chip" onClick={() => applyPreset(key)}>{preset.label}</button>
            ))}
          </div>

          <div className="tabs">
            <button className="tab tab-active"><Key size={13} style={{ display: "inline", marginRight: 4 }} /> LLM（对话/生成）</button>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>API Key {isConfigured.llm && <CheckCircle2 size={13} style={{ display: "inline", color: "var(--green)" }} />}</span>
              <div className="form-row">
                <input type={showKeys.llm ? "text" : "password"} placeholder={isConfigured.llm ? "已配置（输入新值覆盖）" : "sk-..."} value={form.llm_api_key} onChange={(event) => setForm({ ...form, llm_api_key: event.target.value })} />
                <button type="button" className="app-icon" onClick={() => setShowKeys({ ...showKeys, llm: !showKeys.llm })}>{showKeys.llm ? <EyeOff size={15} /> : <Eye size={15} />}</button>
              </div>
            </label>
            <label className="field"><span>Base URL（OpenAI 兼容）</span><input placeholder="https://api.deepseek.com/v1" value={form.llm_base_url} onChange={(event) => setForm({ ...form, llm_base_url: event.target.value })} /></label>
            <label className="field"><span>模型名</span><input placeholder="deepseek-chat" value={form.llm_model} onChange={(event) => setForm({ ...form, llm_model: event.target.value })} /></label>
          </div>

          <div className="divider" />

          <div className="tabs">
            <button className="tab tab-active"><Zap size={13} style={{ display: "inline", marginRight: 4 }} /> Embedding（向量嵌入）</button>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>API Key {isConfigured.embedding && <CheckCircle2 size={13} style={{ display: "inline", color: "var(--green)" }} />}</span>
              <div className="form-row">
                <input type={showKeys.embedding ? "text" : "password"} placeholder={isConfigured.embedding ? "已配置（输入新值覆盖）" : "emb-..."} value={form.embedding_api_key} onChange={(event) => setForm({ ...form, embedding_api_key: event.target.value })} />
                <button type="button" className="app-icon" onClick={() => setShowKeys({ ...showKeys, embedding: !showKeys.embedding })}>{showKeys.embedding ? <EyeOff size={15} /> : <Eye size={15} />}</button>
              </div>
            </label>
            <label className="field"><span>Base URL</span><input placeholder="https://api.siliconflow.cn/v1" value={form.embedding_base_url} onChange={(event) => setForm({ ...form, embedding_base_url: event.target.value })} /></label>
            <label className="field"><span>嵌入模型</span><input placeholder="BAAI/bge-m3" value={form.embedding_model} onChange={(event) => setForm({ ...form, embedding_model: event.target.value })} /></label>
            <label className="field"><span>向量维度</span><input type="number" min={64} max={8192} value={form.embedding_dimensions} onChange={(event) => setForm({ ...form, embedding_dimensions: Number(event.target.value) || 1024 })} /></label>
          </div>

          <div className="divider" />

          <div className="tabs">
            <button className="tab tab-active"><Settings2 size={13} style={{ display: "inline", marginRight: 4 }} /> MinerU（PDF → Markdown）</button>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>API Token {isConfigured.mineru && <CheckCircle2 size={13} style={{ display: "inline", color: "var(--green)" }} />}</span>
              <div className="form-row">
                <input type={showKeys.mineru ? "text" : "password"} placeholder={isConfigured.mineru ? "已配置（输入新值覆盖）" : "eyJ..."} value={form.mineru_api_key} onChange={(event) => setForm({ ...form, mineru_api_key: event.target.value })} />
                <button type="button" className="app-icon" onClick={() => setShowKeys({ ...showKeys, mineru: !showKeys.mineru })}>{showKeys.mineru ? <EyeOff size={15} /> : <Eye size={15} />}</button>
              </div>
            </label>
          </div>

          <div className="hint">密钥只保存在你自己的成员设置中，不会暴露给其他成员。Embedding 凭据用于知识库的索引与检索。</div>
        </Panel>

        <div className="grid">
          <Panel title="配额限额" subtitle="PLATFORM_QUOTA_* 可覆盖" testId="quota-limits">
            {quota ? (
              <div className="list">
                {Object.entries(quota.limits).map(([key, value]) => (
                  <div className="list-item list-item-static" key={key}>
                    <span className="icon-tile"><Settings2 size={15} /></span>
                    <div className="item-copy"><strong>{key}</strong><small>上限 {value.toLocaleString()}</small></div>
                  </div>
                ))}
              </div>
            ) : <EmptyState>配额信息不可用</EmptyState>}
          </Panel>

          <Panel title="本项目用量" subtitle="成本为估算" testId="project-usage">
            {usage ? (
              <>
                <div className="chips">
                  {Object.entries(usage.counts).map(([key, value]) => <span className="chip" key={key}>{key} {value}</span>)}
                </div>
                <div className="hint">估算成本 {usage.estimated_cost.amount} {usage.estimated_cost.currency} · {usage.estimated_cost.note}</div>
              </>
            ) : <EmptyState>用量信息不可用</EmptyState>}
          </Panel>
        </div>
      </section>
    </div>
  );
}
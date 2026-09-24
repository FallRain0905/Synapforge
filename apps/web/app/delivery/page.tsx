"use client";

import Link from "next/link";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, FileCheck2, RefreshCcw, Send, ShieldCheck } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { ConfirmDialog, EmptyState, Panel, Progress } from "../../components/ui";
import {
  BundleVerification,
  DeliveryAssembly,
  DeliveryChecklist,
  DeliveryCompileReport,
  SubmissionBundleReport,
  compileDeliveryPaper,
  createSubmissionBundle,
  getDeliveryAssembly,
  getDeliverySlides,
  runDeliveryChecklist,
  verifySubmissionBundle,
  downloadArtifactContent,} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

const CHECK_LABEL: Record<string, string> = {
  figure_references: "图表引用",
  citation_keys: "引用文献",
  equation_labels: "公式标签",
  anonymity: "匿名要求",
  attachments: "交付附件",
  page_count: "页数",
};

const CHECK_TONE: Record<string, string> = { pass: "status-green", warn: "status-amber", fail: "status-red" };

export default function DeliveryPage() {
  const { projectId, notify, refresh, packState, dashboard, reviewCenter } = useWorkspace();
  const [assembly, setAssembly] = useState<DeliveryAssembly | null>(null);
  const [checklist, setChecklist] = useState<DeliveryChecklist | null>(null);
  const [compile, setCompile] = useState<DeliveryCompileReport | null>(null);
  const [bundle, setBundle] = useState<SubmissionBundleReport | null>(null);
  const [confirmBundle, setConfirmBundle] = useState(false);
  const [verification, setVerification] = useState<BundleVerification | null>(null);
  const [slides, setSlides] = useState("");
  const [busy, setBusy] = useState("");
  // 就绪度用的检查项：进页面后自动跑一次（用户不必先点「交付检查」才知道差什么）
  const [autoChecked, setAutoChecked] = useState(false);

  const loadAssembly = useCallback(async () => {
    if (!projectId) return;
    try {
      const next = await getDeliveryAssembly(projectId);
      setAssembly(next);
    } catch {
      notify("装配读取失败");
    }
  }, [projectId, notify]);

  useEffect(() => {
    void loadAssembly();
  }, [loadAssembly]);

  useEffect(() => {
    if (!projectId || autoChecked) return;
    setAutoChecked(true);
    void runDeliveryChecklist(projectId)
      .then((report) => setChecklist(report))
      .catch(() => notify("交付检查读取失败：可以点右上角「交付检查」重试"));
  }, [projectId, autoChecked, notify]);

  /** 就绪度：把"还差什么"拼成带直达入口的清单（CL-5-01/02）。 */
  const readiness = (() => {
    const items: { key: string; label: string; detail: string; done: boolean; href?: string; tone: "ok" | "warn" | "fail" }[] = [];
    const pendingArtifacts = dashboard.artifacts.filter((artifact) => String(artifact.status) === "PENDING_REVIEW");
    const blockingGates = reviewCenter.gates.filter((gate) => String(gate.status) !== "PASSED");
    const failedChecks = (checklist?.checks ?? []).filter((check) => check.status === "fail");
    const warnedChecks = (checklist?.checks ?? []).filter((check) => check.status === "warn");

    items.push({
      key: "content",
      label: pendingArtifacts.length ? `${pendingArtifacts.length} 份内容还没批准` : "内容都已批准",
      detail: pendingArtifacts.length
        ? `未批准的内容不会进提交包：${pendingArtifacts.slice(0, 3).map((item) => item.name).join("、")}${pendingArtifacts.length > 3 ? " 等" : ""}`
        : "已批准素材可以被装配进论文与提交包",
      done: pendingArtifacts.length === 0,
      href: pendingArtifacts.length ? "/review" : undefined,
      tone: pendingArtifacts.length ? "warn" : "ok",
    });

    items.push({
      key: "assembly",
      label: assembly?.generatable ? `大纲可生成（${assembly.section_count} 章）` : "大纲还生成不了",
      detail: assembly
        ? (assembly.blocked_reasons.length
            ? `缺这些已批准素材：${assembly.blocked_reasons.map((reason) => reason.split(":").slice(1).join(":")).join("、")}`
            : "装配来源齐备")
        : "正在读取装配结果…",
      done: Boolean(assembly?.generatable),
      href: assembly?.generatable ? undefined : "/documents",
      tone: assembly?.generatable ? "ok" : "fail",
    });

    items.push({
      key: "checks",
      label: failedChecks.length ? `交付检查 ${failedChecks.length} 项未通过` : warnedChecks.length ? `交付检查 ${warnedChecks.length} 项提示` : "交付检查通过",
      detail: failedChecks.length
        ? failedChecks.map((check) => check.detail).join("；").slice(0, 160)
        : warnedChecks.length
          ? warnedChecks.map((check) => check.detail).join("；").slice(0, 160)
          : "图表引用、文献条目、公式标签、匿名与页数都符合要求",
      done: failedChecks.length === 0 && warnedChecks.length === 0,
      tone: failedChecks.length ? "fail" : warnedChecks.length ? "warn" : "ok",
    });

    items.push({
      key: "gates",
      label: blockingGates.length ? `${blockingGates.length} 个门禁未通过` : "门禁全部通过",
      detail: blockingGates.length ? "未通过的门禁会挡住下游任务与提交包冻结" : "没有阻塞项",
      done: blockingGates.length === 0,
      href: blockingGates.length ? "/review" : undefined,
      tone: blockingGates.length ? "warn" : "ok",
    });

    items.push({
      key: "compile",
      label: compile ? (compile.status === "compiled" ? `已编译 ${compile.pages} 页 PDF` : "编译未完成") : "还没编译",
      detail: compile?.reason || (compile?.status === "compiled" ? "提交包需要编译产物时可一起带上" : "点右上角「编译论文」生成 PDF"),
      done: compile?.status === "compiled",
      tone: compile?.status === "compiled" ? "ok" : "warn",
    });

    const blocking = items.filter((item) => item.tone === "fail").length;
    const warnings = items.filter((item) => item.tone === "warn").length;
    return { items, blocking, warnings, ready: blocking === 0 };
  })();

  const step = async (label: string, action: () => Promise<string>) => {
    setBusy(label);
    try {
      notify(await action());
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page-content" id="delivery">
      <PageHeading
        hint="只用已批准素材装配；提交包冻结需人工批准，并可在另一套部署校验恢复"
        actions={
          <>
            <button className="button button-secondary" disabled={busy !== ""} data-testid="delivery-checklist" onClick={() => void step("checklist", async () => { const report = await runDeliveryChecklist(projectId); setChecklist(report); return `交付检查：${report.passed ? "全部通过" : `${report.blocking_count} 项阻断`}`; })}>
              <ShieldCheck size={15} /> 交付检查
            </button>
            <button className="button button-secondary" disabled={busy !== ""} data-testid="delivery-compile" onClick={() => void step("compile", async () => { const report = await compileDeliveryPaper(projectId); setCompile(report); return report.status === "compiled" ? `编译完成：${report.pages} 页 PDF` : `未编译：${report.reason}`; })}>
              {busy === "compile" ? <RefreshCcw size={15} className="spin" /> : <FileCheck2 size={15} />} 编译论文
            </button>
            <button className="button button-primary" disabled={busy !== ""} data-testid="delivery-bundle" onClick={() => setConfirmBundle(true)}>
              <Send size={15} /> 生成提交包
            </button>
          </>
        }
      />

      <Panel
        title="交付就绪度"
        subtitle={readiness.blocking ? `还有 ${readiness.blocking} 项必须先处理` : readiness.warnings ? `可以提交，但有 ${readiness.warnings} 项提示` : "各项就绪：可以生成提交包"}
        testId="delivery-readiness"
      >
        <div className="list">
          {readiness.items.map((item) => (
            <div className="list-item list-item-static" key={item.key} data-testid={`delivery-readiness-${item.key}`}>
              <span className={`icon-tile ${item.tone === "ok" ? "icon-tile-green" : item.tone === "fail" ? "icon-tile-red" : "icon-tile-amber"}`}>
                {item.done ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
              </span>
              <div className="item-copy">
                <strong>{item.label}</strong>
                <small>{item.detail}</small>
              </div>
              {item.href ? (
                <Link className="text-button" href={item.href}>{item.key === "content" ? "去审核内容" : item.key === "assembly" ? "去补文档" : "去审核门禁"} →</Link>
              ) : (
                <span className="chip">{item.done ? "就绪" : "待处理"}</span>
              )}
            </div>
          ))}
        </div>
        {assembly && assembly.excluded_count > 0 && (
          <div className="pack-missing" data-testid="delivery-excluded-list">
            <strong>不会进提交包的内容（{assembly.excluded_count} 份）</strong>
            <span>
              {assembly.excluded_unapproved.slice(0, 5).map((item) => `${item.name}（${item.status}）`).join("、")}
              {assembly.excluded_count > 5 ? " 等" : ""}
              ——提交包只装已批准素材；这些内容批准后重新生成即可带上。
            </span>
          </div>
        )}
      </Panel>

      <section className="grid grid-main-side">
        <Panel
          title="大纲与内容装配"
          subtitle="只读取已批准成果物，未批准素材会被排除并列出"
          testId="delivery-assembly"
          actions={<button className="text-button" disabled={busy !== ""} onClick={() => void loadAssembly()}>刷新</button>}
        >
          {assembly ? (
            <>
              <div className="chips">
                <span className={`badge ${assembly.generatable ? "status-green" : "status-red"}`}>
                  {assembly.generatable ? `可生成（${assembly.section_count} 章）` : "缺少已批准素材"}
                </span>
                <span className="chip">来源 {assembly.sources.length}</span>
                <span className="chip">排除未批准 {assembly.excluded_count}</span>
              </div>
              <Progress label={`章节齐备度`} value={(assembly.section_count / Math.max(assembly.section_count + assembly.blocked_reasons.length, 1)) * 100} />
              {assembly.blocked_reasons.length > 0 && (
                <div className="pack-missing">
                  <strong>待补齐章节</strong>
                  <span>{assembly.blocked_reasons.map((reason) => reason.split(":")[1]).join("、")}</span>
                </div>
              )}
              <div className="list">
                {assembly.sections.map((section) => (
                  <div className="list-item list-item-static" key={section.title}>
                    <span className="icon-tile icon-tile-blue"><FileCheck2 size={15} /></span>
                    <div className="item-copy">
                      <strong>{section.title}</strong>
                      <small>{section.source_name} · {section.content_hash.slice(0, 10)} · 批准人 {section.approved_by ?? "—"}</small>
                    </div>
                  </div>
                ))}
                {!assembly.sections.length && <EmptyState>还没有已批准素材；先在文档页把草稿提交并人工批准</EmptyState>}
              </div>
              <button className="panel-footer-action" disabled={busy !== ""} onClick={() => void step("slides", async () => { setSlides(await getDeliverySlides(projectId)); return "答辩提纲已生成（Marp 兼容）"; })}>
                <Download size={14} /> 生成答辩提纲
              </button>
            </>
          ) : <EmptyState>正在读取装配…</EmptyState>}
        </Panel>

        <div className="grid">
          <Panel title="交付检查" subtitle="图表 / 引用 / 公式 / 匿名 / 附件 / 页数" testId="delivery-checklist-report">
            {checklist ? (
              <div className="list">
                {checklist.checks.map((check) => (
                  <div className="list-item list-item-static" key={check.code}>
                    <span className={`badge ${CHECK_TONE[check.status]}`}>{check.status === "pass" ? "通过" : check.status === "warn" ? "提示" : "阻断"}</span>
                    <div className="item-copy">
                      <strong>{CHECK_LABEL[check.code] ?? check.code}</strong>
                      <small>{check.detail}</small>
                    </div>
                  </div>
                ))}
              </div>
            ) : <EmptyState>点击「交付检查」运行检查清单</EmptyState>}
          </Panel>

          <Panel title="真实编译" subtitle="MiKTeX/xelatex 编译并登记 compiled_pdf" testId="delivery-compile-report">
            {compile ? (
              compile.status === "compiled" ? (
                <div className="list">
                  <div className="list-item list-item-static">
                    <span className="icon-tile icon-tile-green"><FileCheck2 size={15} /></span>
                    <div className="item-copy">
                      <strong>{compile.pages} 页 · {Math.round((compile.pdf_bytes ?? 0) / 1024)} KB</strong>
                      <small>{compile.artifact_name} · sha256 {(compile.pdf_sha256 ?? "").slice(0, 12)}</small>
                    </div>
                    <button className="text-button" onClick={() => void downloadArtifactContent(String(compile.artifact_id), "compiled.pdf")}>下载</button>
                  </div>
                </div>
              ) : (
                <div className="pack-missing"><strong>未编译</strong><span>{compile.reason}</span></div>
              )
            ) : <EmptyState>点击「编译论文」用已批准论文源文件生成 PDF（引擎缺失时会明确报错）</EmptyState>}
          </Panel>

          <Panel title="提交包与恢复校验" subtitle="冻结需人工批准；可在隔离部署比对哈希" testId="delivery-bundle-report">
            {bundle ? (
              <>
                <div className="chips">
                  <span className={`badge ${bundle.layer === "submitted" ? "status-amber" : "status-neutral"}`}>{bundle.layer}</span>
                  <span className="chip">来源 {bundle.source_count}</span>
                  <span className="chip">章节 {bundle.section_count}</span>
                  <span className="chip">阻断 {bundle.blocking_count}</span>
                </div>
                <div className="hint">manifest {bundle.manifest_hash.slice(0, 16)}</div>
                <div className="form-row">
                  <button className="button button-secondary" data-testid="delivery-verify" disabled={busy !== ""} onClick={() => void step("verify", async () => { const result = await verifySubmissionBundle(projectId, bundle.artifact_id, false); setVerification(result); return result.verified ? "提交包一致性校验通过" : `校验发现 ${result.mismatches.length} 处不一致`; })}>一致性校验</button>
                  <button className="button button-secondary" data-testid="delivery-restore" disabled={busy !== ""} onClick={() => void step("restore", async () => { const result = await verifySubmissionBundle(projectId, bundle.artifact_id, true); setVerification(result); return result.restore?.restored ? "跨部署恢复校验通过" : "跨部署恢复校验未通过"; })}>跨部署恢复校验</button>
                </div>
                {verification && (
                  <div className="document-impact-result" data-testid="delivery-verify-result">
                    <strong>{verification.verified ? "校验通过" : "存在不一致"}</strong>
                    <small>来源 {verification.checked_sources}/{verification.expected_sources}{verification.mismatches.length ? ` · ${verification.mismatches.join("、")}` : ""}</small>
                    {verification.restore && <small>恢复：{verification.restore.restored ? `已恢复 ${verification.restore.restored_artifact_count} 个成果物，哈希一致` : verification.restore.mismatches.join("、")}</small>}
                  </div>
                )}
              </>
            ) : <EmptyState>点击「生成提交包」装配清单并提交待审</EmptyState>}
          </Panel>
        </div>
      </section>

      {slides && (
        <Panel
          title="答辩提纲（Marp）"
          subtitle={`模板包 ${packState?.pack.version ?? ""}`}
          actions={
            <>
              <button
                className="text-button"
                data-testid="delivery-slides-download"
                onClick={() => {
                  const blob = new Blob([slides], { type: "text/markdown;charset=utf-8" });
                  const url = URL.createObjectURL(blob);
                  const anchor = document.createElement("a");
                  anchor.href = url;
                  anchor.download = "答辩提纲.md";
                  anchor.click();
                  URL.revokeObjectURL(url);
                }}
              >
                <Download size={13} /> 下载
              </button>
              <button className="text-button" onClick={() => setSlides("")}>收起</button>
            </>
          }
        >
          <pre className="code-block">{slides}</pre>
        </Panel>
      )}
      {confirmBundle && (
        <ConfirmDialog
          title="生成提交包？"
          description={
            <>
              会按当前已批准素材装配一份提交包候选（进入 submitted 层），随后需要人工批准才能冻结；
              冻结后哈希写入台账，跨部署校验以它为准。
            </>
          }
          confirmLabel="生成提交包"
          busy={busy !== ""}
          testId="delivery-bundle-confirm"
          onCancel={() => setConfirmBundle(false)}
          onConfirm={() => {
            setConfirmBundle(false);
            void step("bundle", async () => {
              const report = await createSubmissionBundle(projectId, "web");
              setBundle(report);
              setVerification(null);
              return `提交包已生成（${report.layer}），待人工批准冻结`;
            });
          }}
        />
      )}
    </div>
  );
}

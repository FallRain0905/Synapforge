"use client";

/**
 * 工作流包（W3.7，阶段 3）：通用工作流包体系的第一屏。
 *
 * 三段：包列表（版本与定义）、应用到项目（物化任务骨架）、运行列表与推进。
 * 红线（规划 §7）：「骨架 ≠ 结果」文案必须挂在"应用工作流"动作旁——应用模板
 * 生成的是任务和成果物骨架，结果由 Agent 执行并经审核产生。
 * 状态桶映射按 docs/WORKFLOW_API_SHAPES.md §6，与 status-dictionary 词典对齐。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Boxes, CheckCircle2, GitBranch, Info, PlayCircle, Plus } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { EmptyState, LoadingSkeleton, Metric, Modal, Panel, StatusPill } from "../../components/ui";
import { WorkflowEditor } from "../../components/workflow-editor";
import { WorkflowFlowView } from "../../components/workflow-flow";
import { useAuth } from "../../lib/auth";
import { PAGE_GRID } from "../../lib/page-layout";
import { REPOSITIONING_COPY } from "../../lib/status-dictionary";
import { useWorkspace } from "../../lib/workspace";
import {
  advanceWorkflowRun,
  errorMessage,
  getProjectWorkflowDraft,
  getProjects,
  getWorkflow,
  getWorkflowRun,
  installBuiltinWorkflows,
  listWorkflowRuns,
  listWorkflows,
  Project,
  startWorkflowRun,
  WorkflowAdvanceResult,
  WorkflowDefinition,
  WorkflowPackage,
  WorkflowRunView,
} from "../../lib/api";

/** 节点任务状态 → 页面桶（API 形状文档 §6，词典对齐）；未知状态原样显示（不编）。 */
const NODE_BUCKETS: Record<string, string> = {
  CLAIMED: "执行中",
  RUNNING: "执行中",
  READY: "待领取",
  BLOCKED: "受阻",
  NEEDS_REVISION: "受阻",
  WAITING_REVIEW: "待审核",
  APPROVED: "已完成",
  FAILED: "失败",
};

function bucketOf(status: string): string {
  return NODE_BUCKETS[status] ?? status;
}

export default function WorkflowsPage() {
  const { ready, authenticated } = useAuth();
  const { notify } = useWorkspace();
  const [packages, setPackages] = useState<WorkflowPackage[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, WorkflowDefinition | null>>({});
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<WorkflowPackage | null>(null);
  const [draftContent, setDraftContent] = useState<{ definition: WorkflowDefinition; note: string } | null>(null);
  const [applyTarget, setApplyTarget] = useState<WorkflowPackage | null>(null);
  const [runs, setRuns] = useState<WorkflowRunView[] | null>(null);
  const [runDetail, setRunDetail] = useState<WorkflowRunView | null>(null);
  const [advanceSummary, setAdvanceSummary] = useState<WorkflowAdvanceResult | null>(null);

  const loadPackages = useCallback(async () => {
    try {
      setPackages(await listWorkflows());
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "工作流包列表读取失败"));
    }
  }, []);

  const loadRuns = useCallback(async (targetProject: string) => {
    if (!targetProject) {
      setRuns([]);
      return;
    }
    try {
      setRuns(await listWorkflowRuns(targetProject));
    } catch {
      setRuns([]);
    }
  }, []);

  useEffect(() => {
    if (!ready || !authenticated) return;
    void loadPackages();
    void getProjects()
      .then((list) => {
        setProjects(list);
        setProjectId((current) => current || list[0]?.id || "");
      })
      .catch(() => undefined);
  }, [ready, authenticated, loadPackages]);

  useEffect(() => {
    if (projectId) void loadRuns(projectId);
  }, [projectId, loadRuns]);

  const installBuiltin = async () => {
    setBusy(true);
    try {
      const result = await installBuiltinWorkflows();
      notifyInstall(result);
      await loadPackages();
    } catch (failure) {
      setError(errorMessage(failure, "内置包安装失败"));
    } finally {
      setBusy(false);
    }
  };

  const notifyInstall = (result: { created: string[]; skipped: string[] }) => {
    const parts: string[] = [];
    if (result.created.length) parts.push(`已安装：${result.created.join("、")}`);
    if (result.skipped.length) parts.push(`已存在跳过：${result.skipped.join("、")}`);
    notify(parts.join("；") || "没有变化（幂等安装）");
  };

  /** 反向草稿（W4.4）：从项目任务图抽定义 → 进编辑器 → 发布走 POST /api/workflows。 */
  const reverseDraft = async () => {
    if (!projectId) return;
    setBusy(true);
    try {
      const draft = await getProjectWorkflowDraft(projectId);
      setEditing(null);
      setDraftContent({ definition: draft.definition, note: `${draft.note}（来自 ${draft.task_count} 个任务的反向草稿；需编辑补全后发布）` });
      setEditorOpen(true);
      if (draft.validation_errors.length) {
        notify(`草稿带 ${draft.validation_errors.length} 条校验提示，请在编辑器里补全`);
      }
    } catch (failure) {
      notify(errorMessage(failure, "反向草稿生成失败（空项目无法抽取）"));
    } finally {
      setBusy(false);
    }
  };

  const expandPackage = async (pkg: WorkflowPackage) => {
    if (expanded[pkg.id] !== undefined) {
      setExpanded((current) => {
        const next = { ...current };
        delete next[pkg.id];
        return next;
      });
      return;
    }
    try {
      const detail = await getWorkflow(pkg.id);
      setExpanded((current) => ({ ...current, [pkg.id]: detail.definition ?? null }));
    } catch (failure) {
      setError(errorMessage(failure, "定义读取失败"));
    }
  };

  const advance = async (run: WorkflowRunView) => {
    if (!projectId) return;
    setBusy(true);
    try {
      const result = await advanceWorkflowRun(projectId, run.run_id);
      setAdvanceSummary(result);
      setRunDetail(null);
      await loadRuns(projectId);
    } catch (failure) {
      setError(errorMessage(failure, "推进失败"));
    } finally {
      setBusy(false);
    }
  };

  const showRunDetail = async (run: WorkflowRunView) => {
    if (!projectId) return;
    if (runDetail && runDetail.run_id === run.run_id) {
      setRunDetail(null);
      return;
    }
    try {
      setRunDetail(await getWorkflowRun(projectId, run.run_id));
      setAdvanceSummary(null);
    } catch (failure) {
      setError(errorMessage(failure, "运行详情读取失败"));
    }
  };

  const publishedCount = useMemo(() => (packages ?? []).filter((pkg) => pkg.current_version_id).length, [packages]);

  if (!ready || !authenticated) {
    return (
      <div className="page-content" style={PAGE_GRID} id="workflows">
        <PageHeading hint="通用工作流包：定义一次生产流程，反复应用到项目" />
        <LoadingSkeleton rows={3} />
      </div>
    );
  }

  return (
    <div className="page-content" style={PAGE_GRID} id="workflows" data-testid="workflows-page">
      <PageHeading
        hint="工作流包定义阶段、角色、门禁与交付；应用后生成任务骨架，执行与审核仍在任务体系里"
        actions={
          <>
            <button type="button" className="button button-secondary" onClick={() => void installBuiltin()} disabled={busy} data-testid="workflows-install-builtin">
              <Boxes size={15} /> 安装内置包
            </button>
            <button
              type="button"
              className="button button-secondary"
              data-testid="workflows-reverse-draft"
              disabled={busy || !projects.length || !projectId}
              title="从当前项目的任务图反向抽取定义草稿（W4.4）：草稿不是已发布模板，需编辑后发布"
              onClick={() => void reverseDraft()}
            >
              <GitBranch size={15} /> 从项目反向生成
            </button>
            <button
              type="button"
              className="button button-primary"
              data-testid="workflows-create"
              onClick={() => {
                setEditing(null);
                setEditorOpen(true);
              }}
            >
              <Plus size={15} /> 新建工作流包
            </button>
          </>
        }
      />
      {error ? <div className="pack-missing"><strong>操作未完成</strong><span>{error}</span></div> : null}

      <section className="metrics-grid">
        <Metric label="工作流包" value={packages?.length ?? 0} detail={`已发布版本 ${publishedCount} 个`} />
        <Metric label="运行（当前项目）" value={runs?.length ?? 0} detail={projects.find((item) => item.id === projectId)?.name ?? "未选项目"} />
        <Metric label="内置包" value={2} detail="CUMCM 七节点主线 · 长文四节点" tone="positive" />
      </section>

      <Panel
        title="工作流包"
        subtitle="版本一经发布只读；修改内容 = 发布新版本，历史运行绑定原版本不受影响"
      >
        {packages === null ? (
          <LoadingSkeleton rows={3} label="正在加载工作流包" />
        ) : packages.length ? (
          <div className="list">
            {packages.map((pkg) => {
              const definition = expanded[pkg.id];
              return (
                <div className="list-item list-item-static" key={pkg.id} data-testid={`workflow-package-${pkg.key}`}>
                  <span className="icon-tile icon-tile-blue"><Boxes size={15} /></span>
                  <div className="item-copy" style={{ flex: 1, minWidth: 0 }}>
                    <strong>{pkg.name} <small>· {pkg.key}</small></strong>
                    <small>{pkg.description || "（无说明）"}</small>
                    {definition ? (
                      <small style={{ display: "block" }} data-testid={`workflow-definition-${pkg.key}`}>
                        定义：{definition.stages.length} 个阶段 · {definition.nodes.length} 个节点 ·{" "}
                        {definition.role_bindings.length} 个角色 · 门禁 {definition.gate_policies?.length ?? 0} ·{" "}
                        交付适配器 {definition.delivery_adapters?.length ?? 0}
                        {definition.nodes.length ? `（${definition.nodes.map((node) => node.title || node.id).join(" → ")}）` : ""}
                      </small>
                    ) : null}
                  </div>
                  {pkg.current_version_id ? <StatusPill status="APPROVED" label="已发布" /> : <StatusPill status="DRAFT" label="未发布" />}
                  <div style={{ display: "grid", gap: 4 }}>
                    <button type="button" className="text-button" onClick={() => void expandPackage(pkg)}>
                      {definition !== undefined ? "收起定义" : "查看定义"}
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      data-testid={`workflows-apply-${pkg.key}`}
                      onClick={() => setApplyTarget(pkg)}
                      disabled={!pkg.current_version_id || !projects.length}
                      title={!projects.length ? "先建一个项目" : "应用到项目：物化任务骨架"}
                    >
                      应用到项目 →
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => {
                        setEditing(pkg);
                        setExpanded((current) => {
                          if (current[pkg.id] !== undefined) return current;
                          return { ...current, [pkg.id]: null } as Record<string, WorkflowDefinition | null>;
                        });
                        setEditorOpen(true);
                      }}
                    >
                      发布新版本
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState>
            还没有工作流包：点上方「安装内置包」（幂等，含 CUMCM 主线与长文写作），
            或「新建工作流包」从零定义
          </EmptyState>
        )}
      </Panel>

      <Panel
        title="运行"
        subtitle="一次应用 = 一个运行实例；推进由引擎按依赖与门禁推进，也可手动推一轮"
        actions={
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)} style={{ width: "auto" }} data-testid="workflows-project-select">
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name}</option>
            ))}
            {!projects.length ? <option value="">（还没有项目）</option> : null}
          </select>
        }
      >
        {runs === null ? (
          <LoadingSkeleton rows={2} label="正在加载运行" />
        ) : runs.length ? (
          <div className="list">
            {runs.map((run) => (
              <div className="list-item list-item-static" key={run.run_id} data-testid={`workflow-run-${run.run_id}`}>
                <span className={`icon-tile ${run.status === "COMPLETED" ? "icon-tile-green" : run.status === "STALLED" ? "icon-tile-amber" : "icon-tile-blue"}`}>
                  <PlayCircle size={15} />
                </span>
                <div className="item-copy" style={{ flex: 1, minWidth: 0 }}>
                  <strong>{(packages ?? []).find((pkg) => pkg.id === run.workflow_id)?.name ?? run.workflow_id.slice(0, 8)}</strong>
                  <small>
                    {run.status === "RUNNING" ? "推进中" : run.status === "COMPLETED" ? "已完成" : "已停滞（需要人看）"}
                    {" · "}{Object.keys(run.node_tasks).length} 个节点任务 · 更新于 {run.updated_at.slice(0, 16).replace("T", " ")}
                  </small>
                </div>
                <StatusPill status={run.status === "RUNNING" ? "RUNNING" : run.status === "COMPLETED" ? "APPROVED" : "BLOCKED"} label={run.status} />
                <div style={{ display: "grid", gap: 4 }}>
                  <button type="button" className="text-button" onClick={() => void showRunDetail(run)}>
                    {runDetail?.run_id === run.run_id ? "收起详情" : "详情"}
                  </button>
                  {run.status === "RUNNING" ? (
                    <button type="button" className="text-button" disabled={busy} onClick={() => void advance(run)} data-testid={`workflows-advance-${run.run_id}`}>
                      推进一轮
                    </button>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState>
            当前项目还没有工作流运行：在包列表点「应用到项目」，或到
            <Link href="/pack" className="inline-link">建模模板包</Link>走原有入口
          </EmptyState>
        )}
        {runDetail ? (
          <div style={{ marginTop: 10, borderTop: "1px solid var(--line, #ddd)", paddingTop: 10 }} data-testid="workflow-run-detail">
            <div className="hint" style={{ marginBottom: 6 }}>
              <Info size={13} style={{ display: "inline", marginRight: 4 }} />
              账本：第 {runDetail.ledger?.round ?? 0} 轮 · 停滞计数 {runDetail.ledger?.stall_count ?? 0}
              {runDetail.ledger?.needs_replan ? " · 需要重规划（STALLED，请人工查看门禁与失败节点）" : ""}
              {Object.entries(runDetail.attempts ?? {}).filter(([, count]) => count > 1).length
                ? ` · 重试：${Object.entries(runDetail.attempts ?? {}).filter(([, count]) => count > 1).map(([node, count]) => `${node}×${count}`).join("、")}`
                : ""}
            </div>
            {advanceSummary && advanceSummary.run_id === runDetail.run_id ? (
              <div className="hint" data-testid="workflow-advance-summary">
                刚刚推进：第 {advanceSummary.round} 轮 · {advanceSummary.status}
                {advanceSummary.retried.length ? ` · 重试 ${advanceSummary.retried.length} 个节点` : ""}
                {advanceSummary.deliveries.length ? ` · 交付 ${advanceSummary.deliveries.length} 项` : ""}
              </div>
            ) : null}
            <div className="list">
              {Object.entries(runDetail.node_tasks).map(([nodeId, taskId]) => {
                const node = runDetail.definition?.nodes.find((item) => item.id === nodeId);
                const delivery = runDetail.deliveries?.[nodeId];
                const status = runDetail.node_statuses?.[nodeId] ?? "";
                return (
                  <div className="list-item list-item-static" key={nodeId}>
                    <div className="item-copy" style={{ minWidth: 0 }}>
                      <strong>{node?.title ?? nodeId}</strong>
                      <small>
                        任务 {taskId.slice(0, 8)}
                        {runDetail.attempts?.[nodeId] && runDetail.attempts[nodeId] > 1 ? ` · 第 ${runDetail.attempts[nodeId]} 次尝试` : ""}
                        {delivery ? ` · 交付 ${delivery.status}` : ""}
                      </small>
                    </div>
                    {/* node_statuses（8330f77）是真实任务表状态：有则如实进状态桶；未知原样显示 */}
                    <StatusPill status={status || "PENDING"} label={bucketOf(status || "PENDING")} />
                    <Link className="text-button" href="/tasks">任务板 <ArrowUpRight size={12} /></Link>
                  </div>
                );
              })}
            </div>
            {advanceSummary && advanceSummary.run_id === runDetail.run_id ? (
              <div className="list" style={{ marginTop: 8 }} data-testid="workflow-node-statuses">
                {Object.entries(advanceSummary.node_statuses).map(([nodeId, status]) => (
                  <div className="list-item list-item-static" key={nodeId}>
                    <div className="item-copy" style={{ minWidth: 0 }}>
                      <strong>{runDetail.definition?.nodes.find((node) => node.id === nodeId)?.title ?? nodeId}</strong>
                      {advanceSummary.gates
                        .filter((gate) => gate.node_id === nodeId)
                        .map((gate) => (
                          <small key={gate.gate_policy} style={{ display: "block" }}>
                            门禁 {gate.gate_policy}：{gate.verdict}
                            {gate.leaves.length ? `（${gate.leaves.map((leaf) => `${leaf.criterion}=${leaf.verdict}`).join("；")}）` : ""}
                          </small>
                        ))}
                    </div>
                    <StatusPill status={status || "PENDING"} label={bucketOf(status || "PENDING")} />
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </Panel>

      {applyTarget ? (
        <ApplyWorkflowDialog
          pkg={applyTarget}
          projects={projects}
          onClose={() => setApplyTarget(null)}
          onApplied={() => {
            setApplyTarget(null);
            if (projectId) void loadRuns(projectId);
          }}
        />
      ) : null}

      {editorOpen ? (
        <WorkflowEditor
          editing={editing}
          initialDefinition={editing?.definition ?? draftContent?.definition ?? null}
          draftNote={draftContent?.note}
          onClose={() => {
            setEditorOpen(false);
            setDraftContent(null);
          }}
          onSaved={() => {
            setEditorOpen(false);
            setDraftContent(null);
            void loadPackages();
          }}
          notify={notify}
        />
      ) : null}

      {projectId ? <WorkflowFlowView projectId={projectId} /> : null}
    </div>
  );
}

/** 应用工作流：选项目 + 填输入 → 物化任务骨架。「骨架 ≠ 结果」红线文案就在提交按钮旁。 */
function ApplyWorkflowDialog({
  pkg,
  projects,
  onClose,
  onApplied,
}: {
  pkg: WorkflowPackage;
  projects: Project[];
  onClose: () => void;
  onApplied: () => void;
}) {
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ note: string; tasks: number; run_id: string } | null>(null);

  const definition = pkg.definition;
  const inputSpecs = useMemo(() => definition?.workflow.inputs ?? [], [definition]);

  const submit = async () => {
    if (!projectId) {
      setError("先选一个项目");
      return;
    }
    const missing = inputSpecs.filter((input) => input.required && !inputs[input.name]?.trim());
    if (missing.length) {
      setError(`必填输入未填：${missing.map((input) => input.name).join("、")}`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const started = await startWorkflowRun(projectId, {
        workflow_id: pkg.id,
        workflow_version_id: pkg.current_version_id ?? undefined,
        inputs,
      });
      setResult({ note: started.note, tasks: started.tasks.length, run_id: started.run_id });
    } catch (failure) {
      setError(errorMessage(failure, "应用工作流失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`应用工作流 · ${pkg.name}`}
      subtitle="选项目、填输入 → 物化任务骨架；执行与审核走任务体系"
      onClose={onClose}
      testId="workflows-apply-dialog"
    >
      {result ? (
        <div className="empty-cta" data-testid="workflows-apply-done">
          <CheckCircle2 size={22} />
          <strong>已生成 {result.tasks} 个节点任务</strong>
          <small>{result.note}</small>
          <div className="form-row" style={{ justifyContent: "center" }}>
            <Link className="button button-primary" href="/tasks" onClick={onApplied}>去任务板</Link>
            <button type="button" className="button button-secondary" onClick={onApplied}>留在本页</button>
          </div>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          <label style={{ display: "grid", gap: 4 }}>
            <span>目标项目</span>
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)} data-testid="workflows-apply-project">
              {projects.map((project) => (
                <option key={project.id} value={project.id}>{project.name}</option>
              ))}
            </select>
          </label>
          {inputSpecs.map((input) => (
            <label key={input.name} style={{ display: "grid", gap: 4 }}>
              <span>{input.name}{input.required ? " *" : ""}{input.hint ? `（${input.hint}）` : ""}</span>
              <input
                value={inputs[input.name] ?? ""}
                onChange={(event) => setInputs((current) => ({ ...current, [input.name]: event.target.value }))}
                data-testid={`workflows-apply-input-${input.name}`}
              />
            </label>
          ))}
          {error ? <div className="pack-missing"><strong>未完成</strong><span>{error}</span></div> : null}
          <div className="hint" data-testid="workflows-apply-skeleton-note">
            {REPOSITIONING_COPY.skeletonNotResult}
          </div>
          <div className="form-row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>取消</button>
            <button type="button" className="button button-primary" onClick={() => void submit()} disabled={busy || !projectId} data-testid="workflows-apply-submit">
              {busy ? "物化中…" : "应用（生成骨架）"}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

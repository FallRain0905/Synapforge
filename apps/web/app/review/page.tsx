"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, Eye, Inbox, ShieldCheck, Undo2 } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Modal, Panel, StatusPill } from "../../components/ui";
import { StatusLegend } from "../../components/status-legend";
import { Artifact, Gate, RiskRegistryEntry, errorMessage, getArtifactText, submitReview, updateRisk } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

type RiskAction = "ASSIGN" | "RESOLVE" | "REOPEN";

export default function ReviewPage() {
  const { reviewCenter, projectId, refresh, notify, dashboard, loading } = useWorkspace();
  const [action, setAction] = useState<{ risk: RiskRegistryEntry; action: RiskAction } | null>(null);
  const [owner, setOwner] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [approveTarget, setApproveTarget] = useState<Gate | null>(null);
  const [reviseTarget, setReviseTarget] = useState<Gate | null>(null);
  const [reviseSummary, setReviseSummary] = useState("");
  // 待审成果物：产出与回答都进这里，批准后才允许下游引用（CL-2）
  const [previewArtifact, setPreviewArtifact] = useState<Artifact | null>(null);
  const [previewText, setPreviewText] = useState("");
  const [previewBusy, setPreviewBusy] = useState(false);
  const [artifactRevise, setArtifactRevise] = useState<{ artifact: Artifact; summary: string } | null>(null);
  const pendingArtifacts = dashboard.artifacts.filter((artifact) => String(artifact.status) === "PENDING_REVIEW");

  const openRisks = reviewCenter.risks.filter((risk) => !risk.resolved);
  const closedRisks = reviewCenter.risks.filter((risk) => risk.resolved);
  const pendingGates = reviewCenter.gates.filter((gate) => gate.status !== "PASSED");

  /** 门禁目标的显示名：能对上任务/成果物就显示标题，否则退回短 id。 */
  const targetLabel = (gate: Gate) => {
    const id = String(gate.target_id ?? "");
    if (gate.target_type === "task") {
      const task = dashboard.tasks.find((item) => item.id === id);
      if (task) return task.title;
    }
    if (gate.target_type === "artifact") {
      const artifact = dashboard.artifacts.find((item) => item.id === id);
      if (artifact) return artifact.name;
    }
    if (gate.target_type === "handoff") {
      const handoff = reviewCenter.handoffs.find((item) => item.id === id);
      if (handoff) return handoff.objective.slice(0, 40);
    }
    if (gate.target_type === "project") return "项目级门禁";
    return `${gate.target_type} · ${id.slice(0, 8)}`;
  };

  /** 能否走人工复核：复核只接受 task/artifact/handoff，项目级门禁由程序规则派生。 */
  const canReview = (gate: Gate) => gate.target_type !== "project" && Boolean(gate.target_id);

  const reviewGuard = (gate: Gate) => {
    const openSevere = reviewCenter.risks.filter(
      (risk) => !risk.resolved && ["fatal", "major"].includes(String(risk.severity)) && risk.target_id === String(gate.target_id),
    );
    if (openSevere.length) return `该目标还有 ${openSevere.length} 条未关闭的严重风险，服务端会拒绝批准`;
    return "";
  };

  const submitApproval = async (gate: Gate) => {
    if (!projectId || !canReview(gate) || !gate.target_id) return;
    setBusy(true);
    try {
      await submitReview(projectId, {
        target_type: gate.target_type as "task" | "artifact" | "handoff",
        target_id: String(gate.target_id),
        verdict: "APPROVED",
        summary: "人工复核通过",
      });
      await refresh();
      notify(`已批准：${targetLabel(gate)}`);
    } catch (error) {
      // review_blocked_by_open_risks / task_not_waiting_for_review 等守卫原因如实展示。
      notify(errorMessage(error, "批准失败"));
    } finally {
      setBusy(false);
      setApproveTarget(null);
    }
  };

  const submitRevision = async (gate: Gate) => {
    if (!projectId || !canReview(gate) || !gate.target_id) return;
    if (!reviseSummary.trim()) {
      notify("请填写返工说明（会写入复核记录）");
      return;
    }
    setBusy(true);
    try {
      await submitReview(projectId, {
        target_type: gate.target_type as "task" | "artifact" | "handoff",
        target_id: String(gate.target_id),
        verdict: "NEEDS_REVISION",
        summary: reviseSummary.trim(),
      });
      await refresh();
      notify("已退回修订");
      setReviseSummary("");
    } catch (error) {
      notify(errorMessage(error, "退回失败"));
    } finally {
      setBusy(false);
      setReviseTarget(null);
    }
  };

  const openPreview = async (artifact: Artifact) => {
    setPreviewArtifact(artifact);
    setPreviewText("");
    setPreviewBusy(true);
    try {
      setPreviewText(await getArtifactText(artifact.id));
    } catch {
      setPreviewText("（无法读取内容：可能是二进制产物或内容未落库，请下载查看）");
    } finally {
      setPreviewBusy(false);
    }
  };

  const decideArtifact = async (artifact: Artifact, verdict: "APPROVED" | "NEEDS_REVISION", summary: string) => {
    if (!projectId) return;
    setBusy(true);
    try {
      await submitReview(projectId, { target_type: "artifact", target_id: artifact.id, verdict, summary });
      await refresh();
      notify(
        verdict === "APPROVED"
          ? `已批准「${artifact.name}」：现在可以作为下游任务输入`
          : `已退回「${artifact.name}」：修订请到成果物库提交新版本`,
      );
    } catch (error) {
      // review_blocked_by_open_risks 等守卫原因如实展示（例如该成果物上还有未关闭的严重风险）
      notify(errorMessage(error, verdict === "APPROVED" ? "批准失败" : "退回失败"));
    } finally {
      setBusy(false);
      setArtifactRevise(null);
    }
  };

  const submit = async () => {
    if (!action || !projectId) return;
    setBusy(true);
    try {
      await updateRisk(projectId, action.risk.id, {
        action: action.action,
        owner: action.action === "ASSIGN" ? owner : undefined,
        reason: action.action === "ASSIGN" ? undefined : reason,
      });
      setAction(null);
      setOwner("");
      setReason("");
      await refresh();
      notify(action.action === "ASSIGN" ? "风险责任人已更新" : action.action === "RESOLVE" ? "风险已关闭" : "风险已重新打开");
    } catch (error) {
      notify(errorMessage(error, "风险操作失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-content" id="reviews" data-testid="review-center">
      <PageHeading hint={`${pendingArtifacts.length + pendingGates.length} 项需要人工处理 · ${openRisks.length} 个风险未关闭`} />
      <StatusLegend domains={["review", "gate", "artifact"]} />

      <div className="review-priority">
      <Panel
        title={`待审成果物 · ${pendingArtifacts.length}`}
        subtitle="当前要签字的内容：批准后才允许下游引用；未批准内容不会进入交付包"
        testId="pending-artifact-list"
      >
        <div className="list">
          {pendingArtifacts.map((artifact) => {
            const source = artifact.task_id
              ? dashboard.tasks.find((task) => task.id === artifact.task_id)?.title ?? `任务 ${String(artifact.task_id).slice(0, 8)}`
              : "手工/模板创建";
            return (
              <div className="list-item list-item-static" key={artifact.id} data-testid={`pending-artifact-${artifact.id}`}>
                <span className="icon-tile icon-tile-amber"><Inbox size={15} /></span>
                <div className="item-copy">
                  <strong>{artifact.name}</strong>
                  <small>
                    来自：{source}{artifact.run_id ? ` · 运行 ${String(artifact.run_id).slice(0, 8)}` : ""} · v{artifact.version} · {artifact.artifact_type}
                    {artifact.content_hash ? ` · 哈希 ${artifact.content_hash.slice(0, 10)}…` : ""}
                  </small>
                  <small>未批准的原因：内容还没人签字；批准前它不能作为下游输入，也不会进交付包</small>
                </div>
                <div className="list-actions">
                  <StatusPill status={String(artifact.status)} />
                  <div className="form-row">
                    <button className="text-button" disabled={busy} data-testid={`artifact-preview-${artifact.id}`} onClick={() => void openPreview(artifact)}>
                      <Eye size={13} /> 查看内容
                    </button>
                    <button className="button button-primary review-approve" disabled={busy} data-testid={`artifact-approve-${artifact.id}`} onClick={() => void decideArtifact(artifact, "APPROVED", "人工复核通过")}>
                      <CheckCircle2 size={13} /> 批准
                    </button>
                    <button className="text-button" disabled={busy} data-testid={`artifact-revise-${artifact.id}`} onClick={() => setArtifactRevise({ artifact, summary: "" })}>
                      <Undo2 size={13} /> 退回修订
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
          {loading ? <LoadingSkeleton rows={2} label="正在加载待审成果物" /> : !pendingArtifacts.length && <EmptyState>没有待审内容：执行产出会在任务跑完后出现在这里</EmptyState>}
        </div>
      </Panel>
      </div>

      <section className="metrics-grid review-metrics" aria-label="审核概览">
        <div className="metric"><span>门禁总数</span><strong>{reviewCenter.gates.length}</strong><small>待确认 {pendingGates.length}</small></div>
        <div className="metric metric-warning"><span>未关闭风险</span><strong>{openRisks.length}</strong><small>已关闭 {closedRisks.length}</small></div>
        <div className="metric"><span>复核记录</span><strong>{reviewCenter.reviews.length}</strong><small>含机器审计与人工复核</small></div>
        <div className="metric metric-positive"><span>证据条目</span><strong>{reviewCenter.evidence.length}</strong><small>结论可追溯</small></div>
      </section>

      <section className="grid grid-main-side">
        <Panel title="门禁" subtitle="批准由人工复核产生：提交 APPROVED 复核后门禁派生为 PASSED" testId="gate-list">
          <div className="list">
            {reviewCenter.gates.map((gate) => (
              <div className="list-item list-item-static" key={gate.id} data-testid={`gate-${gate.id}`}>
                <span className={`icon-tile ${gate.status === "PASSED" ? "icon-tile-green" : gate.status === "BLOCKED" ? "icon-tile-red" : "icon-tile-amber"}`}><ShieldCheck size={15} /></span>
                <div className="item-copy">
                  <strong>{targetLabel(gate)}</strong>
                  <small>{gate.target_type} · {String(gate.target_id).slice(0, 8)} · 规则 {Array.isArray(gate.rules) ? gate.rules.length : 0} 条</small>
                  {gate.required_human_approval && gate.status !== "PASSED" && (
                    <small>
                      {!canReview(gate)
                        ? "项目级门禁由程序规则派生，不能通过复核直接批准"
                        : reviewGuard(gate) || "需要人工确认"}
                    </small>
                  )}
                </div>
                <div className="list-actions">
                  <StatusPill status={String(gate.status)} />
                  {gate.required_human_approval && gate.status !== "PASSED" && canReview(gate) && (
                    <div className="form-row">
                      <button
                        className="text-button"
                        disabled={busy}
                        data-testid={`gate-approve-${gate.id}`}
                        onClick={() => setApproveTarget(gate)}
                      >
                        <CheckCircle2 size={13} /> 批准
                      </button>
                      <button
                        className="text-button"
                        disabled={busy}
                        data-testid={`gate-revise-${gate.id}`}
                        onClick={() => { setReviseTarget(gate); setReviseSummary(""); }}
                      >
                        <Undo2 size={13} /> 退回修订
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {loading ? <LoadingSkeleton rows={2} label="正在加载门禁" /> : !reviewCenter.gates.length && <EmptyState>暂无门禁记录</EmptyState>}
          </div>
        </Panel>

        <Panel title="复核意见" subtitle="机器审计与人工复核" testId="review-list">
          <div className="list">
            {reviewCenter.reviews.slice(0, 8).map((review) => (
              <div className="list-item list-item-static" key={review.id}>
                <span className={`risk-severity risk-severity-${String(review.verdict) === "APPROVED" ? "minor" : "major"}`}><ShieldCheck size={14} /></span>
                <div className="item-copy">
                  <strong>{review.verdict} · {review.reviewer}</strong>
                  <small>{review.summary} · {formatTime(review.created_at)}</small>
                </div>
                <span className="chip">{String(review.reviewer_kind)}</span>
              </div>
            ))}
            {!reviewCenter.reviews.length && <EmptyState>暂无复核记录</EmptyState>}
          </div>
        </Panel>
      </section>

      <section className="grid grid-main-side">
        <Panel title="风险登记" subtitle="责任人、关闭证据与重新打开" testId="risk-register" actions={<span className={`gate-count ${openRisks.length ? "risk-count-active" : "gate-count-ok"}`}>{openRisks.length}</span>}>
          <div className="list">
            {reviewCenter.risks.slice(0, 10).map((risk) => (
              <div className="list-item list-item-static" key={risk.id}>
                <span className={`risk-severity risk-severity-${risk.severity}`}><AlertTriangle size={14} /></span>
                <div className="item-copy">
                  <strong>{risk.code}<span className={`risk-label risk-label-${risk.severity}`} style={{ marginLeft: 8 }}>{risk.severity}</span></strong>
                  <small>{risk.message}</small>
                  <small>{risk.owner ? `责任人 ${risk.owner}` : "未分配责任人"}{risk.resolution_reason ? ` · ${risk.resolution_reason}` : ""}</small>
                </div>
                <div className="list-inline-actions">
                  {risk.resolved ? (
                    <>
                      <span className="badge status-green">已关闭</span>
                      <button className="text-button" onClick={() => setAction({ risk, action: "REOPEN" })}>重新打开</button>
                    </>
                  ) : (
                    <>
                      {!risk.owner && <button className="text-button" onClick={() => setAction({ risk, action: "ASSIGN" })}>分配</button>}
                      <button className="text-button" onClick={() => setAction({ risk, action: "RESOLVE" })}>关闭</button>
                    </>
                  )}
                </div>
              </div>
            ))}
            {!reviewCenter.risks.length && <EmptyState>暂无风险登记</EmptyState>}
          </div>
        </Panel>

        <Panel title="交接收据" subtitle="Fanout 必须等待所有接收方确认" testId="handoff-receipts">
          <div className="list">
            {reviewCenter.handoffs.filter((handoff) => handoff.receipts.length > 0).slice(0, 6).map((handoff) => (
              <div className="list-item list-item-static" key={handoff.id}>
                <span className="icon-tile"><Inbox size={15} /></span>
                <div className="item-copy">
                  <strong>{handoff.handoff_type} · {handoff.sender_agent_id}</strong>
                  <small>{handoff.objective}</small>
                  <div className="chips">
                    {handoff.receipts.map((receipt) => (
                      <span className={`chip ${receipt.status === "ACCEPTED" ? "status-green" : receipt.status === "REJECTED" ? "status-red" : "status-amber"}`} key={receipt.id}>
                        {receipt.receiver_id} · {receipt.status}
                      </span>
                    ))}
                  </div>
                </div>
                <StatusPill status={String(handoff.receipt_status)} />
              </div>
            ))}
            {!reviewCenter.handoffs.some((handoff) => handoff.receipts.length > 0) && <EmptyState>暂无交接收据</EmptyState>}
          </div>
        </Panel>
      </section>

      {action && (
        <Modal
          title={action.action === "ASSIGN" ? "分配风险责任人" : action.action === "RESOLVE" ? "关闭风险" : "重新打开风险"}
          subtitle={action.risk.message}
          onClose={() => setAction(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setAction(null)}>取消</button>
              <button className="button button-primary" disabled={busy} data-testid="risk-submit" onClick={() => void submit()}>
                {busy ? "提交中" : "确认"}
              </button>
            </>
          }
        >
          {action.action === "ASSIGN" ? (
            <label className="field"><span>责任人</span><input value={owner} onChange={(event) => setOwner(event.target.value)} placeholder="填写成员标识或 Agent id（如 agent-aster）" /></label>
          ) : (
            <label className="field"><span>说明{action.action === "RESOLVE" ? "（关闭理由）" : "（重新打开原因）"}</span><textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          )}
        </Modal>
      )}

      {previewArtifact && (
        <Modal
          title={`内容预览 · ${previewArtifact.name}`}
          subtitle={`${previewArtifact.artifact_type} · v${previewArtifact.version} · ${previewArtifact.status}`}
          wide
          testId="artifact-preview-modal"
          onClose={() => setPreviewArtifact(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setPreviewArtifact(null)}>关闭</button>
              <button
                className="button button-primary"
                disabled={busy || previewBusy}
                data-testid="artifact-preview-approve"
                onClick={() => void decideArtifact(previewArtifact, "APPROVED", "人工复核通过")}
              >
                <CheckCircle2 size={15} /> 批准这份内容
              </button>
            </>
          }
        >
          <pre
            className="code-block"
            data-testid="artifact-preview-content"
            style={{ whiteSpace: "pre-wrap", fontFamily: "ui-monospace, monospace", fontSize: 12, maxHeight: 420, overflow: "auto" }}
          >
            {previewBusy ? "正在读取…" : (previewText || "（内容为空）")}
          </pre>
        </Modal>
      )}

      {artifactRevise && (
        <Modal
          title="退回修订"
          subtitle={`${artifactRevise.artifact.name} · v${artifactRevise.artifact.version}`}
          testId="artifact-revise-dialog"
          onClose={() => setArtifactRevise(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setArtifactRevise(null)}>取消</button>
              <button
                className="button button-primary"
                disabled={busy}
                data-testid="artifact-revise-confirm"
                onClick={() => void decideArtifact(artifactRevise.artifact, "NEEDS_REVISION", artifactRevise.summary.trim() || "需要修订")}
              >
                <Undo2 size={15} /> 退回
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="hint">
              退回后这份内容不能用于下游；修订在「成果物库」点「提交新版本」——已批准版本不可覆盖，修订会产生新版本并重新走审核。
            </div>
            <label className="field">
              <span>退回原因（写入复核记录，作者据此修订）</span>
              <textarea
                rows={4}
                value={artifactRevise.summary}
                data-testid="artifact-revise-summary"
                onChange={(event) => setArtifactRevise({ ...artifactRevise, summary: event.target.value })}
              />
            </label>
          </div>
        </Modal>
      )}

      {approveTarget && (
        <ConfirmDialog
          title="确认批准？"
          description={
            <>
              <strong>{targetLabel(approveTarget)}</strong>
              <div style={{ marginTop: 6 }}>
                批准会提交一条 <code>verdict=APPROVED</code> 的人工复核：门禁派生为 PASSED，
                任务/成果物/交接随之进入可下游状态（成果物会变为不可修改）。
              </div>
              {reviewGuard(approveTarget) && <div className="pack-missing" style={{ marginTop: 8 }}><strong>可能被拒绝</strong><span>{reviewGuard(approveTarget)}</span></div>}
            </>
          }
          confirmLabel="批准"
          busy={busy}
          testId="gate-approve-confirm"
          onCancel={() => setApproveTarget(null)}
          onConfirm={() => void submitApproval(approveTarget)}
        />
      )}

      {reviseTarget && (
        <Modal
          title="退回修订"
          subtitle={targetLabel(reviseTarget)}
          testId="gate-revise-modal"
          onClose={() => setReviseTarget(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setReviseTarget(null)}>取消</button>
              <button
                className="button button-danger"
                disabled={busy || !reviseSummary.trim()}
                data-testid="gate-revise-submit"
                onClick={() => void submitRevision(reviseTarget)}
              >
                退回并记录
              </button>
            </>
          }
        >
          <label className="field"><span>返工说明（必填，会写入复核记录与门禁快照）</span>
            <textarea
              rows={3}
              value={reviseSummary}
              data-testid="gate-revise-summary"
              placeholder="例如：问题三缺少消融对照，请补齐后重新提交复核"
              onChange={(event) => setReviseSummary(event.target.value)}
            />
          </label>
        </Modal>
      )}
    </div>
  );
}
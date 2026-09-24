"use client";

import { useState } from "react";
import Link from "next/link";
import { Archive, Box, Download, FilePlus2, Info } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Modal, Panel, StatusPill } from "../../components/ui";
import {
  downloadArtifactContent,
  Artifact,
  ArtifactDetail,
  archiveArtifact,
  createArtifactVersion,
  errorMessage,
  getArtifactDetail,
  getArtifactText,
  saveArtifactText,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

/** 成果物类型的中文名：列表里直接显示 `paper_source`/`agent_answer` 这种机器码没人看得懂。 */
const ARTIFACT_TYPE_LABELS: Record<string, string> = {
  problem_source: "题面材料",
  problem_facts: "题面事实",
  problem_analysis: "问题拆解",
  data_profile: "数据画像",
  model_spec: "模型规格",
  code: "代码",
  experiment_plan: "实验计划",
  run_manifest: "运行清单",
  result_table: "结果表",
  figure: "图表",
  audit_report: "审计报告",
  review_report: "复核报告",
  paper_source: "论文源码",
  compiled_pdf: "编译产物",
  submission_bundle: "提交包",
  agent_answer: "Agent 回答",
};

/** 能否作为下游输入：必须是人工批准的版本，且允许下游引用（服务端领取校验也按这条）。 */
function downstreamState(artifact: Artifact): { ok: boolean; reason: string } {
  if (artifact.status === "APPROVED" && artifact.downstream_allowed !== false) {
    return { ok: true, reason: "已批准：可以被下游任务引用" };
  }
  if (artifact.status === "APPROVED") {
    return { ok: false, reason: "已批准但被标记为不可下游引用" };
  }
  if (artifact.status === "REJECTED") return { ok: false, reason: "已被退回：需提交新版本并重新审核" };
  if (artifact.status === "ARCHIVED") return { ok: false, reason: "已归档：保留审计，但不再作为新任务输入" };
  return { ok: false, reason: "尚未审核：批准后才能作为下游输入" };
}

export default function ArtifactsPage() {
  const { dashboard, projectId, refresh, notify, loading } = useWorkspace();
  const [typeFilter, setTypeFilter] = useState("all");
  const [reviseTarget, setReviseTarget] = useState<Artifact | null>(null);
  const [reviseText, setReviseText] = useState("");
  const [reviseBusy, setReviseBusy] = useState(false);
  const [loadingContent, setLoadingContent] = useState(false);
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<Artifact | null>(null);
  const types = Array.from(new Set(dashboard.artifacts.map((artifact) => artifact.artifact_type)));
  const visible = typeFilter === "all" ? dashboard.artifacts : dashboard.artifacts.filter((artifact) => artifact.artifact_type === typeFilter);
  const pendingReview = dashboard.artifacts.filter((artifact) => artifact.status === "PENDING_REVIEW").length;

  /** 打开修订：把被退回版本的正文读出来给人改（批准过的版本不可覆盖，只能出新的）。 */
  const openRevise = async (artifact: Artifact) => {
    setReviseTarget(artifact);
    setReviseText("");
    setLoadingContent(true);
    try {
      setReviseText(await getArtifactText(artifact.id));
    } catch {
      notify("读取旧版本内容失败：可以直接写新内容");
    } finally {
      setLoadingContent(false);
    }
  };

  /** 提交新版本：版本端点要求名称与类型一致；内容随后写入**新**版本（旧版本保持不可变）。 */
  const submitRevision = async () => {
    if (!reviseTarget || !projectId) return;
    setReviseBusy(true);
    try {
      const created = await createArtifactVersion(projectId, reviseTarget.id, {
        name: reviseTarget.name,
        artifact_type: reviseTarget.artifact_type,
        description: reviseTarget.description,
        status: "PENDING_REVIEW",
        task_id: reviseTarget.task_id ?? undefined,
        run_id: reviseTarget.run_id ?? undefined,
      });
      await saveArtifactText(created.id, reviseText);
      await refresh();
      notify(`已提交 v${created.version}（待审）：旧版本保持不变，审核通过后新版本才可用于下游`);
      setReviseTarget(null);
      setReviseText("");
    } catch (error) {
      notify(errorMessage(error, "提交新版本失败"));
    } finally {
      setReviseBusy(false);
    }
  };

  /** 打开详情：来源、版本谱系、引用关系、复核记录与事件（CL-3）。 */
  const openDetail = async (artifact: Artifact) => {
    setDetailBusy(true);
    setDetail(null);
    try {
      setDetail(await getArtifactDetail(projectId, artifact.id));
    } catch (error) {
      notify(errorMessage(error, "详情读取失败"));
    } finally {
      setDetailBusy(false);
    }
  };

  const confirmArchive = async () => {
    if (!archiveTarget) return;
    try {
      await archiveArtifact(archiveTarget.id);
      await refresh();
      notify(`已归档「${archiveTarget.name}」：保留审计与历史引用，不再作为新任务输入`);
      if (detail?.artifact.id === archiveTarget.id) setDetail(null);
    } catch (error) {
      notify(errorMessage(error, "归档失败"));
    } finally {
      setArchiveTarget(null);
    }
  };

  return (
    <div className="page-content" id="artifacts">
      <PageHeading hint="版本、状态与来源保持可追溯" actions={
        <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} style={{ width: "auto" }} data-testid="artifact-filter">
          <option value="all">全部类型</option>
          {types.map((type) => <option key={type} value={type}>{type}</option>)}
        </select>
      } />
      <section className="metrics-grid">
        <Metric label="成果物总数" value={dashboard.artifacts.length} detail={`${types.length} 种类型`} />
        <Metric label="已批准" value={dashboard.artifacts.filter((item) => item.status === "APPROVED").length} detail="不可变版本" tone="positive" />
        <Metric label="待复核" value={dashboard.artifacts.filter((item) => item.status === "PENDING_REVIEW").length} detail="等待人工确认" tone="warning" />
        <Metric label="草稿" value={dashboard.artifacts.filter((item) => item.status === "DRAFT").length} detail="仍可编辑" />
      </section>
      <Panel title="成果物脉络" subtitle="按类型筛选，查看版本与来源" testId="artifact-list">
        <div className="table">
          <div className="table-head"><span>成果物</span><span>类型</span><span>版本</span><span>状态</span><span /></div>
          {visible.map((artifact) => (
            (() => {
              const source = artifact.task_id
                ? dashboard.tasks.find((task) => task.id === artifact.task_id)?.title ?? `任务 ${String(artifact.task_id).slice(0, 8)}`
                : "手工/模板创建";
              const downstream = downstreamState(artifact);
              return (
                <div className="table-row" key={artifact.id} data-testid={`artifact-row-${artifact.id}`}>
                  <div className="table-title">
                    <strong>{artifact.name}</strong>
                    <small>{artifact.description || "暂无说明"} · 来源：{source}{artifact.run_id ? ` · 运行 ${String(artifact.run_id).slice(0, 8)}` : ""} · {formatTime(artifact.created_at)}</small>
                    <small data-testid={`artifact-downstream-${artifact.id}`}>
                      {downstream.ok ? "✓ " : "× "}{downstream.reason}
                      {artifact.content_hash ? ` · 哈希 ${artifact.content_hash.slice(0, 10)}…` : ""}
                    </small>
                    {artifact.task_id && !dashboard.tasks.some((task) => task.id === artifact.task_id) && (
                      <small data-testid={`artifact-orphan-${artifact.id}`}>
                        孤儿内容：来源任务已不存在（保留审计，不影响引用关系）
                      </small>
                    )}
                  </div>
                  <span className="chip" data-label="类型">{ARTIFACT_TYPE_LABELS[artifact.artifact_type] ?? artifact.artifact_type}</span>
                  <span className="hint" data-label="版本">v{artifact.version}{artifact.parent_artifact_id ? "（修订版）" : ""}</span>
                  <span data-label="状态"><StatusPill status={String(artifact.status)} /></span>
                  <div className="row-actions">
                    <button className="text-button" disabled={detailBusy} data-testid={`artifact-detail-${artifact.id}`} onClick={() => void openDetail(artifact)}>
                      详情
                    </button>
                    {artifact.status !== "ARCHIVED" && (
                      <button className="text-button" data-testid={`artifact-archive-${artifact.id}`} onClick={() => setArchiveTarget(artifact)}>
                        <Archive size={13} /> 归档
                      </button>
                    )}
                    {artifact.status === "REJECTED" && (
                      <button className="text-button" disabled={reviseBusy} data-testid={`artifact-revise-${artifact.id}`} onClick={() => void openRevise(artifact)}>
                        <FilePlus2 size={13} /> 提交新版本
                      </button>
                    )}
                    <button className="text-button" onClick={() => void downloadArtifactContent(artifact.id, artifact.name)}><Download size={14} /> 下载</button>
                  </div>
                </div>
              );
            })()
          ))}
        </div>
        {loading ? <LoadingSkeleton rows={3} label="正在加载成果物" /> : !visible.length && <EmptyState><Box size={16} /> 暂无成果物</EmptyState>}
      </Panel>

      {pendingReview > 0 && (
        <div className="pack-missing" data-testid="artifacts-pending-hint">
          <strong>{pendingReview} 份内容在等审核</strong>
          <span>
            执行产出与回答一律先进入「待复核」：有人批准后它们才能作为下游任务输入、才能进交付包。
            到「审核门禁」页逐份给出结论；被退回的版本在这里点「提交新版本」——已批准的版本不可覆盖。
          </span>
        </div>
      )}

      {detailBusy && <div className="hint">正在读取成果物详情…</div>}

      {detail && (
        <Modal
          title={`成果物详情 · ${detail.artifact.name}`}
          subtitle={`v${detail.artifact.version} · ${detail.artifact.status} · ${detail.artifact.artifact_type}`}
          wide
          testId="artifact-detail-modal"
          onClose={() => setDetail(null)}
          actions={
            <>
              <Link className="button button-secondary" href={`/timeline?artifact=${detail.artifact.id}`}>在时间线看它的事件</Link>
              <button className="button button-secondary" onClick={() => void downloadArtifactContent(detail.artifact.id, detail.artifact.name)}>下载内容</button>
              <button className="button button-primary" onClick={() => setDetail(null)}>关闭</button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 14 }}>
            {detail.orphan && (
              <div className="pack-missing" data-testid="artifact-detail-orphan">
                <strong>孤儿内容</strong>
                <span>{detail.orphan_reason}。内容与引用关系保留，只做标注不清理（归档是唯一的退休方式）。</span>
              </div>
            )}

            <div className="field">
              <span>来源</span>
              <div className="chips">
                <span className="chip" data-testid="artifact-detail-source-task">
                  产出任务：{detail.source_task ? detail.source_task.title : "（无记录）"}
                </span>
                <span className="chip">
                  执行：{detail.source_run ? `${detail.source_run.agent_id} · ${formatTime(detail.source_run.started_at)}` : "（无记录）"}
                </span>
                <span className="chip">内容哈希 {(detail.artifact.content_hash || "—").slice(0, 12)}…</span>
                <span className="chip">创建者 {detail.artifact.created_by}（{detail.artifact.created_by_kind ?? "member"}）</span>
              </div>
              <small className="hint">
                能否用于下游：
                {detail.artifact.status === "APPROVED" && detail.artifact.downstream_allowed !== false
                  ? "可以（已批准且允许下游引用）"
                  : "不可以（未批准 / 已退回 / 已归档）"}
              </small>
            </div>

            <div className="field">
              <span>版本谱系（{detail.lineage.length} 个版本）</span>
              <div className="list" data-testid="artifact-detail-lineage">
                {detail.lineage.map((entry) => (
                  <div className="list-item list-item-static" key={entry.artifact_id}>
                    <span className={`icon-tile ${entry.is_current ? "icon-tile-blue" : ""}`}>v{entry.version}</span>
                    <div className="item-copy">
                      <strong>{entry.status}{entry.is_current ? "（当前查看）" : ""}</strong>
                      <small>哈希 {(entry.content_hash || "—").slice(0, 12)}… · {formatTime(entry.created_at)}</small>
                    </div>
                    <StatusPill status={entry.status} />
                  </div>
                ))}
              </div>
              <small className="hint">已批准的版本不可覆盖：修订会产生新版本，旧版本原样保留（可追溯"改了什么"）。</small>
            </div>

            <div className="field">
              <span>被谁引用（{detail.consumers.length}）</span>
              {detail.consumers.length ? (
                <div className="list" data-testid="artifact-detail-consumers">
                  {detail.consumers.map((consumer) => (
                    <div className="list-item list-item-static" key={consumer.task_id}>
                      <div className="item-copy">
                        <strong>{consumer.title}</strong>
                        <small>作为输入成果物 · 任务状态 {consumer.status}</small>
                      </div>
                      <StatusPill status={consumer.status} />
                    </div>
                  ))}
                </div>
              ) : (
                <small className="hint">还没有任务把它当输入（下游任务在创建时可勾选已批准的内容）。</small>
              )}
            </div>

            <div className="field">
              <span>复核记录（{detail.reviews.length}）</span>
              {detail.reviews.length ? (
                <div className="list" data-testid="artifact-detail-reviews">
                  {detail.reviews.map((review) => (
                    <div className="list-item list-item-static" key={review.id}>
                      <div className="item-copy">
                        <strong>{review.verdict} · {review.reviewer}</strong>
                        <small>{review.summary || "（无说明）"} · {formatTime(review.created_at)}</small>
                      </div>
                      <span className="chip">{review.reviewer_kind}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <small className="hint">还没有复核结论{detail.gate ? `；门禁状态 ${detail.gate.status}` : "，也还没有门禁记录"}。</small>
              )}
            </div>
          </div>
        </Modal>
      )}

      {archiveTarget && (
        <ConfirmDialog
          title="归档这份内容？"
          description={
            <>
              <strong>{archiveTarget.name}</strong>（v{archiveTarget.version}）
              <div style={{ marginTop: 6 }}>
                归档后它不再作为新任务输入（领取校验会拦下引用它的任务），但内容、版本谱系与历史引用关系全部保留——
                这是"退休"，不是删除。
              </div>
            </>
          }
          confirmLabel="归档"
          busy={false}
          testId="artifact-archive-confirm"
          onCancel={() => setArchiveTarget(null)}
          onConfirm={() => void confirmArchive()}
        />
      )}

      {reviseTarget && (
        <Modal
          title="提交新版本"
          subtitle={`${reviseTarget.name} · 当前 v${reviseTarget.version}（${reviseTarget.status}）`}
          wide
          testId="artifact-revise-modal"
          onClose={() => setReviseTarget(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setReviseTarget(null)}>取消</button>
              <button className="button button-primary" disabled={reviseBusy || loadingContent} data-testid="artifact-revise-submit" onClick={() => void submitRevision()}>
                提交为待审新版本
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="hint">
              <Info size={13} style={{ display: "inline", marginRight: 4 }} />
              旧版本会原样保留（已批准版本不可覆盖，被退回的版本也要留痕）；新版本带自己的哈希与审核记录。
            </div>
            <label className="field">
              <span>新版本内容（Markdown/文本；二进制产物请用命令行重跑后由内核自动采集）</span>
              <textarea
                rows={12}
                value={loadingContent ? "正在读取旧版本内容…" : reviseText}
                disabled={loadingContent}
                data-testid="artifact-revise-content"
                onChange={(event) => setReviseText(event.target.value)}
                style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}
              />
            </label>
          </div>
        </Modal>
      )}
    </div>
  );
}

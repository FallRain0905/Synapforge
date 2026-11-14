"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, FolderPlus, PlayCircle, RefreshCcw, ShieldCheck, Sparkles } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { ConfirmDialog, EmptyState, FindingList, Modal, Panel, Progress } from "../../components/ui";
import { CreateProjectModal } from "../../components/project-create";
import {
  BoundaryAudit,
  CompetitionPackSummary,
  applyProjectCompetitionPack,
  createBoundaryGate,
  errorMessage,
  fetchProjectPackTemplate,
  getBoundaryAudit,
  getCompetitionPacks,
  validateProjectCompetitionPack,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

const LAYER_LABELS: Record<string, string> = { draft: "草稿", submitted: "提交", approved: "批准" };

export default function PackPage() {
  const { projectId, project, packState, packReport, setPackReport, notify, refresh, reviewCenter } = useWorkspace();
  const [applying, setApplying] = useState(false);
  const [validating, setValidating] = useState(false);
  const [auditing, setAuditing] = useState(false);
  const [audit, setAudit] = useState<BoundaryAudit | null>(null);
  const [selectedQuestions, setSelectedQuestions] = useState<number[]>([]);
  const [preview, setPreview] = useState<{ filename: string; text: string } | null>(null);
  const [previewing, setPreviewing] = useState("");
  const [packs, setPacks] = useState<CompetitionPackSummary[]>([]);
  const [problemCode, setProblemCode] = useState("");
  const [confirmApply, setConfirmApply] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmGate, setConfirmGate] = useState(false);

  const materialization = packState?.materialization;
  const templates = packState?.pack.templates ?? [];

  // 可用模板包清单：此前 getCompetitionPacks 是死代码，页面看不到有哪些包可选。
  const loadPacks = useCallback(async () => {
    try {
      setPacks(await getCompetitionPacks());
    } catch {
      // 列表拉不到不影响当前项目已绑定包的使用。
    }
  }, []);

  useEffect(() => { void loadPacks(); }, [loadPacks]);
  useEffect(() => { setProblemCode(project?.problem_code ?? ""); }, [project?.problem_code]);

  const selectedPack = packs.find((pack) => pack.pack_id === packState?.competition_pack) ?? null;
  const problemOptions = selectedPack?.problem_codes ?? ["A", "B", "C", "D", "E"];

  const handleApply = async () => {
    if (!projectId) return;
    setApplying(true);
    try {
      const result = await applyProjectCompetitionPack(projectId, {
        questions: selectedQuestions.length ? selectedQuestions : undefined,
        problem_code: problemCode || undefined,
      });
      await refresh();
      setPackReport(null);
      notify(
        result.created_task_count || result.created_artifact_count
          ? `模板包已应用：新增 ${result.created_task_count} 个任务、${result.created_artifact_count} 个模板成果物`
          : "模板包已是最新状态",
      );
    } catch (error) {
      notify(errorMessage(error, "模板包应用失败"));
    } finally {
      setApplying(false);
      setConfirmApply(false);
    }
  };

  const handleValidate = async () => {
    if (!projectId) return;
    setValidating(true);
    try {
      const report = await validateProjectCompetitionPack(projectId);
      setPackReport(report);
      notify(`校验完成：${report.status}`);
    } catch (error) {
      notify(errorMessage(error, "校验失败"));
    } finally {
      setValidating(false);
    }
  };

  const handleBoundaryAudit = async (persist: boolean) => {
    if (!projectId) return;
    setAuditing(true);
    try {
      const result = persist ? await createBoundaryGate(projectId) : await getBoundaryAudit(projectId);
      setAudit(result);
      notify(persist ? (result.created ? `已生成门禁：${result.verdict}` : "边界审计干净，等待人工确认") : "边界审计已刷新");
      if (persist) await refresh();
    } catch (error) {
      notify(errorMessage(error, "边界审计失败"));
    } finally {
      setAuditing(false);
    }
  };

  const handlePreview = async (templateId: string, filename: string) => {
    if (!projectId) return;
    setPreviewing(templateId);
    try {
      const text = await fetchProjectPackTemplate(projectId, templateId, selectedQuestions.length ? selectedQuestions : undefined);
      setPreview({ filename, text });
    } catch (error) {
      notify(errorMessage(error, "模板渲染失败"));
    } finally {
      setPreviewing("");
    }
  };

  return (
    <div className="page-content" id="pack">
      <PageHeading
        hint={packState ? `${packState.pack.display_name} · v${packState.pack.version} · ${packState.pack.questions.length} 个问题 · ${templates.length} 个模板` : "等待模板包数据"}
        actions={
          <>
            <select
              value={problemCode}
              onChange={(event) => setProblemCode(event.target.value)}
              style={{ width: "auto", minWidth: 120 }}
              disabled={!projectId}
              data-testid="pack-problem-code"
            >
              <option value="">题号未指定</option>
              {problemOptions.map((code) => <option key={code} value={code}>{code} 题</option>)}
            </select>
            <button className="button button-primary" data-testid="pack-apply" disabled={applying || !projectId} onClick={() => setConfirmApply(true)}>
              {applying ? <RefreshCcw size={15} className="spin" /> : <Sparkles size={15} />} {applying ? "正在应用" : "一键应用模板包"}
            </button>
            <button className="button button-secondary" data-testid="pack-validate" disabled={validating || !projectId} onClick={() => void handleValidate()}>
              {validating ? <RefreshCcw size={15} className="spin" /> : <ShieldCheck size={15} />} 运行模板校验
            </button>
          </>
        }
      />

      {!packState && (
        <section className="panel" data-testid="pack-unbound">
          <div className="empty-cta">
            <FolderPlus size={20} />
            <strong>{projectId ? "当前项目没有可用的模板包" : "还没有项目"}</strong>
            <small>
              {projectId
                ? `项目绑定的模板包「${project?.competition_pack || "未设置"}」无法解析。平台暂不支持给已有项目换包，请用目标模板包新建项目。`
                : "模板包是随项目创建的：先建项目并选择模板包，之后就能在这里一键物化四问流程。"}
            </small>
            <button className="button button-primary" data-testid="pack-unbound-create" onClick={() => setCreateOpen(true)}>
              <FolderPlus size={16} /> 用模板包新建项目
            </button>
          </div>
        </section>
      )}

      <section className="grid grid-3">
        <Panel title="物化进度" subtitle="四问任务与模板成果物" testId="pack-panel">
          {materialization ? (
            <>
              <div className="gate-count gate-count-ok" data-testid="pack-planned-count" style={{ justifySelf: "start" }}>
                {materialization.planned_present}/{materialization.planned_total}
              </div>
              <div data-testid="pack-progress">
                <Progress
                  label="物化进度"
                  value={materialization.progress * 100}
                  detail={`任务 ${materialization.task_present}/${materialization.task_total} · 成果物 ${materialization.artifact_present}/${materialization.artifact_total}`}
                />
              </div>
              <div className="field">
                <span>问题范围（不选表示全部）</span>
                <div className="chips">
                  {(packState?.pack.questions ?? [1, 2, 3, 4]).map((question) => {
                    const active = selectedQuestions.includes(question);
                    return (
                      <button
                        key={question}
                        type="button"
                        className={`chip ${active ? "status-blue" : ""}`.trim()}
                        data-testid={`pack-question-${question}`}
                        onClick={() =>
                          setSelectedQuestions((current) =>
                            current.includes(question) ? current.filter((value) => value !== question) : [...current, question].sort(),
                          )
                        }
                      >
                        问题{["一", "二", "三", "四", "五", "六"][question - 1] ?? question}
                      </button>
                    );
                  })}
                </div>
              </div>
              {(materialization.missing_tasks.length > 0 || materialization.missing_artifacts.length > 0) && (
                <div className="pack-missing" data-testid="pack-missing">
                  <strong>待补齐</strong>
                  <span>{materialization.missing_artifacts.length} 个模板成果物 · {materialization.missing_tasks.length} 个任务</span>
                </div>
              )}
              {materialization.missing_tasks.length === 0 && materialization.missing_artifacts.length === 0 && (
                <div className="pack-missing" data-testid="pack-next-step">
                  <strong>模板包已物化完成：下一步是让它跑起来</strong>
                  <span>
                    这 {materialization.task_total} 个任务现在还没有"执行方式"，Agent 领走后会直接失败。
                    先给任务设一次执行方式（Codex 或一条命令），内核就会按依赖顺序自动领取执行；
                    审核门禁和交接仍然由人来确认。
                  </span>
                  <div className="form-row">
                    <Link className="button button-primary" href="/tasks" data-testid="pack-go-start"><PlayCircle size={16} /> 去启动任务</Link>
                    <Link className="button button-secondary" href="/devices">接入 Agent</Link>
                  </div>
                </div>
              )}
            </>
          ) : <EmptyState>暂无模板包状态</EmptyState>}
        </Panel>

        <Panel title="模板清单" subtitle="点击预览渲染后的骨架" testId="pack-template-list">
          <div className="list" style={{ maxHeight: 420, overflowY: "auto" }}>
            {templates.map((template) => (
              <div className="list-item list-item-static" key={template.template_id} data-testid={`pack-template-${template.template_id}`}>
                <span className="icon-tile"><ArrowUpRight size={14} /></span>
                <div className="item-copy">
                  <strong>{template.name}</strong>
                  <small>{template.filename} · {template.stage}{template.official_format ? " · 官方格式" : ""}</small>
                </div>
                <button
                  className="text-button"
                  disabled={previewing === template.template_id}
                  onClick={() => void handlePreview(template.template_id, template.filename)}
                >
                  {previewing === template.template_id ? "渲染中" : "预览"}
                </button>
              </div>
            ))}
            {!templates.length && <EmptyState>暂无模板</EmptyState>}
          </div>
        </Panel>

        <Panel
          title="信息边界审计"
          subtitle="pack 规则 × 运行事实"
          actions={
            <>
              <button className="text-button" disabled={auditing} onClick={() => void handleBoundaryAudit(false)}>预览</button>
              <button className="text-button" disabled={auditing} onClick={() => setConfirmGate(true)}>落库门禁</button>
            </>
          }
        >
          {audit ? (
            <>
              <div className="chips">
                <span className={`badge ${audit.verdict ? "status-amber" : "status-green"}`}>
                  {audit.verdict ?? "干净（待人工确认）"}
                </span>
                <span className="chip">运行 {audit.run_count}</span>
                <span className="chip">发现 {audit.findings.length}</span>
                {audit.gate_status && <span className="chip">门禁 {audit.gate_status}</span>}
              </div>
              <div className="hint">
                规则：{Object.entries(audit.rules).filter(([, value]) => value).map(([key]) => key).join("、") || "—"}
              </div>
              <FindingList findings={audit.findings} />
            </>
          ) : (
            <EmptyState>尚未运行边界审计；会比对 pack 规则与运行事实（观察模式、未来数据、未声明输入）</EmptyState>
          )}
        </Panel>
      </section>

      <section className="grid grid-main-side">
        <Panel title="模板校验报告" subtitle="四问覆盖、章节字数、官方结果表与信息边界" testId="pack-validation-report">
          {packReport ? (
            <>
              <div className="chips">
                <span className={`status-pill ${packReport.status === "PASS" ? "status-green" : packReport.status === "PASS_WITH_ASSUMPTIONS" ? "status-amber" : "status-red"}`}>
                  {packReport.status}
                </span>
                <span className="chip">覆盖 {packReport.coverage.questions_covered?.length ?? 0}/{packReport.coverage.expected_questions?.length ?? 4} 问</span>
                <span className="chip">发现 {packReport.findings.length} 项</span>
              </div>
              {packReport.missing_artifacts.length > 0 && <div className="pack-missing"><strong>缺少成果物</strong><span>{packReport.missing_artifacts.join("、")}</span></div>}
              <div data-testid="pack-findings"><FindingList findings={packReport.findings} /></div>
            </>
          ) : <EmptyState>点击「运行模板校验」查看结果</EmptyState>}
        </Panel>

        <Panel title="门禁与复核" subtitle="机器审计与人工确认">
          <div className="list">
            {reviewCenter.gates.slice(0, 6).map((gate) => (
              <div className="list-item list-item-static" key={gate.id}>
                <span className={`icon-tile ${gate.status === "PASSED" ? "icon-tile-green" : "icon-tile-amber"}`}><ShieldCheck size={15} /></span>
                <div className="item-copy">
                  <strong>{gate.target_type} · {String(gate.target_id).slice(0, 8)}</strong>
                  <small>{gate.status} · 人工确认{gate.required_human_approval ? "必需" : "可选"}</small>
                </div>
              </div>
            ))}
            {!reviewCenter.gates.length && <EmptyState>暂无门禁</EmptyState>}
          </div>
          <div className="divider" />
          <div className="hint">人工批准仍走审核中心；机器审计只提出复核意见。</div>
        </Panel>
      </section>

      <section>
        <Panel
          title={`可用模板包（${packs.length}）`}
          subtitle="平台内置的领域包与版本；项目在创建时绑定其中一个，应用与校验都作用于绑定的那个包"
          testId="pack-catalog"
        >
          {packs.length ? (
            <div className="grid grid-3">
              {packs.map((pack) => {
                const bound = pack.pack_id === packState?.competition_pack;
                return (
                  <div className="card list-item list-item-static" key={pack.pack_id} data-testid={`pack-catalog-${pack.pack_id}`} style={{ gap: 12, alignItems: "flex-start" }}>
                    <div className="item-copy">
                      <strong>{pack.display_name}{bound ? " · 已绑定" : ""}</strong>
                      <small>v{pack.version} · {pack.template_count} 个模板骨架 · {pack.dag_task_count} 个流程任务</small>
                      <small>题号 {pack.problem_codes.join(" / ")} · 问题 {pack.questions.join("、")}</small>
                    </div>
                    {!bound && (
                      <button className="text-button" data-testid={`pack-use-${pack.pack_id}`} onClick={() => setCreateOpen(true)}>
                        <FolderPlus size={13} /> 用此包新建项目
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          ) : <EmptyState>模板包清单读取失败；当前项目绑定的包仍可使用</EmptyState>}
          <div className="hint" style={{ marginTop: 10 }}>
            平台暂不支持给已有项目更换模板包：换包会改变四问 DAG 与成果物骨架，需走决策变更后另行设计。
          </div>
        </Panel>
      </section>

      {confirmApply && packState && materialization && (
        <ConfirmDialog
          title="应用模板包？"
          description={
            <>
              <div>包：<strong>{packState.pack.display_name} v{packState.pack.version}</strong></div>
              <div style={{ marginTop: 6 }}>
                将补建 <strong>{materialization.missing_tasks.length}</strong> 个任务、
                <strong>{materialization.missing_artifacts.length}</strong> 个模板成果物
                {materialization.missing_tasks.length + materialization.missing_artifacts.length === 0 ? "（当前已全部齐备，应用不会新增内容）" : ""}。
              </div>
              <div style={{ marginTop: 6 }}>
                题号：{problemCode || "未指定"}；问题范围：{selectedQuestions.length ? selectedQuestions.join("、") : "全部"}。
              </div>
              <div className="hint" style={{ marginTop: 6 }}>应用是幂等的：已存在的任务与成果物不会重复创建。</div>
            </>
          }
          confirmLabel="应用模板包"
          busy={applying}
          testId="pack-apply-confirm"
          onCancel={() => setConfirmApply(false)}
          onConfirm={() => void handleApply()}
        />
      )}

      {confirmGate && (
        <ConfirmDialog
          title="把边界审计落库为门禁？"
          description={
            <>
              这会写入一条机器复核与门禁记录；若审计发现 fatal/major 问题，门禁会阻断下游交付，
              需要人工复核或返工后才能继续。
            </>
          }
          confirmLabel="落库门禁"
          busy={auditing}
          testId="pack-gate-confirm"
          onCancel={() => setConfirmGate(false)}
          onConfirm={() => { setConfirmGate(false); void handleBoundaryAudit(true); }}
        />
      )}

      {createOpen && <CreateProjectModal open={createOpen} onClose={() => setCreateOpen(false)} />}

      {preview && (
        <Modal
          title={preview.filename}
          subtitle="按当前项目上下文渲染的模板骨架"
          wide
          testId="pack-preview-modal"
          onClose={() => setPreview(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => void navigator.clipboard?.writeText(preview.text).then(() => notify("已复制"))}>复制内容</button>
              <button
                className="button button-primary"
                onClick={() => {
                  const blob = new Blob([preview.text], { type: "text/plain;charset=utf-8" });
                  const url = URL.createObjectURL(blob);
                  const anchor = document.createElement("a");
                  anchor.href = url;
                  anchor.download = preview.filename;
                  anchor.click();
                  URL.revokeObjectURL(url);
                }}
              >
                下载 {preview.filename}
              </button>
            </>
          }
        >
          <pre className="code-block" data-testid="pack-preview-body">{preview.text}</pre>
        </Modal>
      )}
    </div>
  );
}
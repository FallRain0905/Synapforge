"use client";

import Link from "next/link";
import { useState } from "react";
import { Activity, AlertTriangle, ArrowUpRight, Box, CheckCircle2, ClipboardCheck, Cloud, Cpu, FileCheck2, FileText, FolderPlus, Inbox, Info, MessagesSquare, PlayCircle, Send, ShieldCheck, TerminalSquare } from "lucide-react";
import { PageHeading } from "../components/shell";
import { CreateProjectModal } from "../components/project-create";
import { EmptyState, LoadingSkeleton, Metric, Panel, Progress, StatusPill } from "../components/ui";
import { diagnoseTask } from "../lib/task-flow";
import { useWorkspace } from "../lib/workspace";

const stageLabels: Record<string, string> = {
  problem_intake: "审题与事实",
  problem_analysis: "审题与拆解",
  modeling: "模型建立",
  coding: "代码与计算",
  experiment: "仿真与实验",
  review: "独立复核",
  paper: "论文交付",
  delivery: "交付冻结",
};

const QUICK_LINKS = [
  { href: "/pack", label: "建模模板包", description: "物化四问流程与 9 个模板骨架", icon: <FileCheck2 size={18} /> },
  { href: "/documents", label: "文档与版本", description: "三层版本、差异、协作编辑", icon: <FileText size={18} /> },
  { href: "/review", label: "审核门禁", description: "门禁、复核、风险与收据", icon: <ShieldCheck size={18} /> },
  { href: "/delivery", label: "论文交付", description: "装配、检查、编译与提交包", icon: <Send size={18} /> },
  { href: "/tasks", label: "任务与流程", description: "阶段看板与状态推进", icon: <ClipboardCheck size={18} /> },
  { href: "/drive", label: "个人云盘", description: "200MB 私人文件与项目导入", icon: <Cloud size={18} /> },
];

export default function OverviewPage() {
  const { dashboard, reviewCenter, packState, loading, project, projects, error } = useWorkspace();
  const [createOpen, setCreateOpen] = useState(false);
  const openRisks = reviewCenter.risks.filter((risk) => !risk.resolved);
  const pendingGates = reviewCenter.gates.filter((gate) => gate.status !== "PASSED");
  const approvedArtifacts = dashboard.artifacts.filter((artifact) => artifact.status === "APPROVED");
  const onlineAgents = dashboard.agents.filter((agent) => String(agent.status) === "online").length;
  const activeTasks = dashboard.tasks.filter((task) => ["READY", "CLAIMED", "RUNNING"].includes(String(task.status)));
  const noProject = !loading && !project;

  // 「下一步」引导：这一段决定首页最重要的那个按钮指向哪里。
  // 顺序就是真实上手顺序：建项目 → 准备任务 → 接入 Agent → 让任务跑起来。
  // 注意：**模板包不是必选项**——"准备任务"有两条路：自定义一条任务（让 Agent 写文档/查资料都行），
  // 或用模板包一键物化整个数模流程。完成条件只看"项目里有没有任务"。
  const hasAnyTask = dashboard.tasks.length > 0;
  const packApplied = Boolean(packState && packState.materialization.task_present > 0);
  const hasOnlineAgent = onlineAgents > 0;
  const flow = { tasks: dashboard.tasks, handoffs: dashboard.handoffs, artifacts: dashboard.artifacts, onlineAgents };
  const tasksNeedingExecutor = dashboard.tasks.filter((task) => diagnoseTask(task, flow).state === "no_executor").length;
  const tasksRunnable = dashboard.tasks.filter((task) => ["runnable", "leased", "in_progress"].includes(diagnoseTask(task, flow).state)).length;
  const hasRun = dashboard.runs.length > 0;

  const steps = [
    { key: "project", label: "建项目", done: Boolean(project), href: "", hint: project ? `当前项目：${project.name}` : "项目是任务、成果物与知识库的归属边界" },
    {
      key: "tasks",
      label: "准备任务",
      done: hasAnyTask,
      href: "/workspace",
      hint: hasAnyTask
        ? `已有 ${dashboard.tasks.length} 个任务${packApplied ? "（模板包流程已物化）" : ""}`
        : "两条路：直接建自定义任务（让 Agent 写文档、查资料），或用模板包一键生成数模全流程",
    },
    { key: "agent", label: "接入 Agent", done: hasOnlineAgent, href: "/devices", hint: hasOnlineAgent ? `${onlineAgents} 个 Agent 在线` : "下载桌面端或跑接入脚本，让机器替你来干活" },
    { key: "run", label: "让任务跑起来", done: hasRun, href: "/tasks", hint: hasRun ? `已有 ${dashboard.runs.length} 次执行记录` : tasksNeedingExecutor ? `${tasksNeedingExecutor} 个任务缺少执行方式` : "给任务设执行方式，Agent 会自动领取" },
  ];
  const nextStep = steps.find((step) => !step.done);
  const primary =
    nextStep?.key === "project"
      ? { label: "新建项目", href: "", hint: "先建一个项目" }
      : nextStep
        ? {
            label: nextStep.key === "run" ? "去启动任务" : nextStep.key === "agent" ? "去接入 Agent" : "去准备任务",
            href: nextStep.href,
            hint: nextStep.hint,
          }
        : { label: "进入交付", href: "/delivery", hint: "流程已跑通：装配、检查、编译与提交包" };

  if (noProject) {
    return (
      <div className="page-content" id="overview" data-testid="project-overview">
        <PageHeading hint="还没有项目：项目是任务、成果物与知识库的归属边界" />
        <section className="panel">
          <div className="empty-cta" data-testid="overview-empty-projects">
            <FolderPlus size={22} />
            <strong>{projects.length ? "请选择一个项目" : "从新建第一个项目开始"}</strong>
            <small>
              项目建好后，可以在「建模模板包」一键物化四问流程与模板骨架，
              再接入 Agent 让它领取任务。没有项目时，任务、文档、门禁、交付都不存在归属对象。
            </small>
            <div className="form-row" style={{ justifyContent: "center" }}>
              <button className="button button-primary" data-testid="overview-create-project" onClick={() => setCreateOpen(true)}>
                <FolderPlus size={16} /> 新建项目
              </button>
              <Link className="button button-secondary" href="/pack"><FileCheck2 size={16} /> 先看看模板包</Link>
            </div>
          </div>
        </section>
        {createOpen && <CreateProjectModal open={createOpen} onClose={() => setCreateOpen(false)} />}
      </div>
    );
  }

  return (
    <div className="page-content" id="overview" data-testid="project-overview">
      <PageHeading
        actions={
          <>
            <Link className="button button-secondary" href="/workspace" data-testid="overview-open-workspace">
              <MessagesSquare size={16} /> 项目工作区
            </Link>
            <Link className="button button-secondary" href="/pack"><FileCheck2 size={16} /> 模板包</Link>
            {primary.href ? (
              <Link className="button button-primary" href={primary.href} data-testid="overview-next-step">
                <ArrowUpRight size={16} /> {primary.label}
              </Link>
            ) : (
              <button className="button button-primary" data-testid="overview-next-step" onClick={() => setCreateOpen(true)}>
                <FolderPlus size={16} /> {primary.label}
              </button>
            )}
          </>
        }
        hint={project ? `项目「${project.name}」· 当前阶段 ${stageLabels[project.stage] ?? project.stage} · ${onlineAgents} 个 Agent 在线` : "等待项目数据"}
      />

      {error && <div className="pack-missing"><strong>API 未连接</strong><span>{error}</span></div>}

      <Panel
        title="下一步"
        subtitle={nextStep ? `还差一步：${nextStep.label}——${nextStep.hint}` : "主流程已跑通，往下是交付与审核"}
        testId="overview-steps"
      >
        <div className="list">
          {steps.map((step, index) => (
            <div className="list-item list-item-static" key={step.key} data-testid={`overview-step-${step.key}`}>
              <span className={`icon-tile ${step.done ? "icon-tile-green" : step.key === nextStep?.key ? "icon-tile-blue" : ""}`.trim()}>
                {step.done ? <CheckCircle2 size={15} /> : index + 1}
              </span>
              <div className="item-copy">
                <strong>{step.label}{step.key === nextStep?.key ? "（当前）" : ""}</strong>
                <small>{step.hint}</small>
              </div>
              {step.done ? (
                <StatusPill status="APPROVED" label="完成" />
              ) : (
                <Link className="text-button" href={step.href || "#"} onClick={step.href ? undefined : (event) => { event.preventDefault(); setCreateOpen(true); }}>
                  {step.href ? "去处理 →" : "新建项目 →"}
                </Link>
              )}
            </div>
          ))}
        </div>
        {tasksNeedingExecutor > 0 && (
          <div className="hint" data-testid="overview-executor-hint">
            <Info size={13} style={{ display: "inline", marginRight: 4 }} />
            {tasksNeedingExecutor} 个任务还没法执行：它们缺少"执行方式"。到「任务与流程」点一次「设置执行方式」，
            内核连上平台后就会自动领取并执行——网页里没有、也不需要"开始"按钮（平台是拉取模型）。
          </div>
        )}
        {tasksRunnable > 0 && (
          <div className="hint">
            <PlayCircle size={13} style={{ display: "inline", marginRight: 4 }} />
            {tasksRunnable} 个任务已具备执行条件，正在等 Agent 领取。
          </div>
        )}
        {!noProject && !hasOnlineAgent && packApplied && (
          <div className="hint">
            <Cpu size={13} style={{ display: "inline", marginRight: 4 }} />
            任务已经排好队了，但还没有 Agent 在线：接入后它会立刻开始领任务。
          </div>
        )}
      </Panel>

      <section className="metrics-grid">
        <Metric label="活跃任务" value={activeTasks.length} detail={`共 ${dashboard.tasks.length} 个任务`} />
        <Metric label="待审核门禁" value={pendingGates.length} detail="需要人工确认" tone="warning" />
        <Metric label="已批准成果物" value={approvedArtifacts.length} detail={`共 ${dashboard.artifacts.length} 个版本`} tone="positive" />
        <Metric label="未关闭风险" value={openRisks.length} detail={openRisks.length ? "需指定责任人" : "风险已清空"} tone={openRisks.length ? "warning" : "positive"} />
      </section>

      <section className="grid grid-main-side">
        <Panel title="项目进度" subtitle="阶段、任务与模板包齐备度">
          {loading ? <LoadingSkeleton rows={3} label="正在加载项目状态" /> : (
            <>
              <Progress label={stageLabels[project?.stage ?? ""] ?? project?.stage ?? ""} value={project?.progress ?? 0} />
              {packState && (
                <>
                  <div className="divider" />
                  <Progress
                    label={`${packState.pack.display_name} v${packState.pack.version}`}
                    value={packState.materialization.progress * 100}
                    detail={`任务 ${packState.materialization.task_present}/${packState.materialization.task_total} · 成果物 ${packState.materialization.artifact_present}/${packState.materialization.artifact_total}`}
                  />
                </>
              )}
              <div className="list" style={{ marginTop: 6 }}>
                {dashboard.tasks.slice(0, 4).map((task) => (
                  <div className="list-item list-item-static" key={task.id}>
                    <span className="icon-tile"><ClipboardCheck size={15} /></span>
                    <div className="item-copy"><strong>{task.title}</strong><small>{task.assignee} · {stageLabels[task.stage] ?? task.stage}</small></div>
                    <StatusPill status={String(task.status)} />
                  </div>
                ))}
                {!dashboard.tasks.length && <EmptyState>暂无任务；先在模板包中一键物化</EmptyState>}
                {dashboard.tasks.length > 4 && (
                  <Link className="panel-footer-action" href="/tasks">查看全部 {dashboard.tasks.length} 个任务 <ArrowUpRight size={14} /></Link>
                )}
              </div>
            </>
          )}
        </Panel>

        <Panel title="门禁与风险" subtitle="需要人工介入的事项">
          <div className="list">
            {reviewCenter.gates.slice(0, 3).map((gate) => (
              <div className="list-item list-item-static" key={gate.id}>
                <span className={`icon-tile ${gate.status === "PASSED" ? "icon-tile-green" : "icon-tile-amber"}`}><ShieldCheck size={15} /></span>
                <div className="item-copy"><strong>{gate.target_type} · {String(gate.target_id).slice(0, 8)}</strong><small>{gate.required_human_approval ? "需要人工确认" : "自动门禁"}</small></div>
                <StatusPill status={String(gate.status)} />
              </div>
            ))}
            {openRisks.slice(0, 2).map((risk) => (
              <div className="list-item list-item-static" key={risk.id}>
                <span className={`risk-severity risk-severity-${risk.severity}`}><AlertTriangle size={14} /></span>
                <div className="item-copy"><strong>{risk.code}</strong><small>{risk.message}</small></div>
                <span className={`risk-label risk-label-${risk.severity}`}>{risk.severity}</span>
              </div>
            ))}
            {!reviewCenter.gates.length && !openRisks.length && <EmptyState>门禁与风险均已清空</EmptyState>}
            {(reviewCenter.gates.length > 3 || openRisks.length > 2) && (
              <Link className="panel-footer-action" href="/review">查看审核中心 <ArrowUpRight size={14} /></Link>
            )}
          </div>
        </Panel>
      </section>

      <section className="grid grid-3">
        {QUICK_LINKS.map((link) => (
          <Link key={link.href} href={link.href} className="card list-item list-item-static" style={{ gap: 14 }}>
            <span className="icon-tile icon-tile-blue">{link.icon}</span>
            <div className="item-copy">
              <strong>{link.label}</strong>
              <small>{link.description}</small>
            </div>
            <ArrowUpRight size={16} />
          </Link>
        ))}
      </section>

      <section className="grid grid-3">
        <Panel title="运行与 Agent" actions={<Link className="text-button" href="/runs">控制台 <ArrowUpRight size={14} /></Link>}>
          <div className="chips">
            {dashboard.agents.slice(0, 6).map((agent) => <span className="chip" key={agent.agent_id}>{agent.display_name}</span>)}
            {!dashboard.agents.length && <EmptyState>等待 Agent 接入</EmptyState>}
          </div>
          {dashboard.runs.length > 0 && (
            <div className="hint"><TerminalSquare size={13} style={{ display: "inline", marginRight: 4 }} />最近运行：{dashboard.runs[0].agent_id} · {dashboard.runs[0].status}</div>
          )}
        </Panel>
        <Panel title="交接与成果物" actions={<Link className="text-button" href="/handoffs">交接中心 <ArrowUpRight size={14} /></Link>}>
          <div className="chips">
            <span className="chip"><Inbox size={13} style={{ display: "inline", marginRight: 4 }} />{reviewCenter.handoffs.length} 个交接</span>
            <span className="chip"><Box size={13} style={{ display: "inline", marginRight: 4 }} />{dashboard.artifacts.length} 个成果物</span>
          </div>
        </Panel>
        <Panel title="最近事件" actions={<Link className="text-button" href="/timeline">时间线 <ArrowUpRight size={14} /></Link>}>
          <div className="list">
            {dashboard.events.slice(-3).reverse().map((event) => (
              <div className="list-item list-item-static" key={event.id}>
                <span className="icon-tile"><Activity size={15} /></span>
                <div className="item-copy"><strong>{event.event_type}</strong><small>{event.actor}</small></div>
              </div>
            ))}
            {!dashboard.events.length && <EmptyState>暂无事件</EmptyState>}
          </div>
        </Panel>
      </section>
    </div>
  );
}
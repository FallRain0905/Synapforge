"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Ban, CheckCircle2, PlayCircle, Plus, RefreshCcw, ShieldCheck, TerminalSquare } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Modal, Panel, StatusPill } from "../../components/ui";
import {
  Agent,
  EvidenceRequirement,
  ProjectMemberView,
  Task,
  TaskDetail,
  TaskFlags,
  TaskStatus,
  createTask,
  errorMessage,
  getProjectTaskFlags,
  getTaskDetail,
  listProjectMembers,
  updateTask,
} from "../../lib/api";
import { presentEvent, splitRunSummary } from "../../lib/events";
import { TaskDiagnosis, diagnoseTask } from "../../lib/task-flow";
import { useWorkspace } from "../../lib/workspace";

const STAGE_ORDER = ["problem_intake", "problem_analysis", "modeling", "coding", "experiment", "review", "paper", "delivery"];
const STAGE_LABEL: Record<string, string> = {
  problem_intake: "审题与事实",
  problem_analysis: "审题与拆解",
  modeling: "模型建立",
  coding: "代码与计算",
  experiment: "仿真与实验",
  review: "独立复核",
  paper: "论文交付",
  delivery: "交付冻结",
};

const TASK_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  READY: "待领取",
  CLAIMED: "已领取",
  RUNNING: "执行中",
  WAITING_REVIEW: "待复核",
  NEEDS_REVISION: "需返工",
  BLOCKED: "已阻塞",
  FAILED: "失败",
  APPROVED: "已批准",
  CANCELLED: "已取消",
};

/**
 * 与 `store._task_transition_allowed` 对齐的可选流转。
 *
 * 这里只列平台真正允许的状态迁移：界面不再提供必然被服务端拒绝的按钮
 * （例如 RUNNING → NEEDS_REVISION 非法；APPROVED 必须由人工复核产生，不能直接 PATCH）。
 */
const TASK_TRANSITIONS: Record<string, { target: TaskStatus; label: string; hint: string; confirm?: boolean; danger?: boolean }[]> = {
  DRAFT: [
    { target: "READY", label: "进入待办", hint: "进入可领取队列，Agent 可以领走" },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  READY: [
    { target: "RUNNING", label: "人工接管", hint: "只在你自己（不通过 Agent）执行这个任务时用：它不会让 Agent 领取，也不会产生执行记录" },
    { target: "BLOCKED", label: "阻塞", hint: "阻断下游依赖，需人工解除", confirm: true },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  CLAIMED: [
    { target: "RUNNING", label: "标记执行中", hint: "Agent 已领取：仅同步状态标签，不改变它的执行" },
    { target: "READY", label: "退回待办", hint: "释放给其他 Agent 领取" },
    { target: "BLOCKED", label: "阻塞", hint: "阻断下游依赖，需人工解除", confirm: true },
    { target: "FAILED", label: "标记失败", hint: "执行失败，确认后可从失败重试", confirm: true },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  RUNNING: [
    { target: "WAITING_REVIEW", label: "提交复核", hint: "进入待审核，由人工复核后才可批准" },
    { target: "BLOCKED", label: "阻塞", hint: "阻断下游依赖，需人工解除", confirm: true },
    { target: "FAILED", label: "标记失败", hint: "执行失败，确认后可从失败重试", confirm: true },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  WAITING_REVIEW: [
    { target: "NEEDS_REVISION", label: "返工", hint: "退回修订，修订后可重新排队" },
    { target: "BLOCKED", label: "阻塞", hint: "阻断下游依赖，需人工解除", confirm: true },
  ],
  NEEDS_REVISION: [
    { target: "READY", label: "重新排队", hint: "回到可领取队列" },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  BLOCKED: [
    { target: "READY", label: "解除阻塞", hint: "回到可领取队列" },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  FAILED: [
    { target: "READY", label: "重试", hint: "回到可领取队列" },
    { target: "CANCELLED", label: "取消", hint: "任务终止，不可继续执行", confirm: true, danger: true },
  ],
  APPROVED: [],
  CANCELLED: [],
};

/** 负责人候选项：真实 Agent + 保留历史值（平台把 assignee 当标注，领取时会写成 agent_id）。 */
function assigneeOptions(agents: Agent[], current: string) {
  const options = [{ value: "Unassigned", label: "未指派" }];
  for (const agent of agents) {
    options.push({ value: agent.agent_id, label: `${agent.display_name}（${agent.agent_id}）` });
  }
  if (current && current !== "Unassigned" && !agents.some((agent) => agent.agent_id === current)) {
    options.push({ value: current, label: `${current}（历史值）` });
  }
  return options;
}

/**
 * 任务行里的"能不能跑"提示：可执行状态只给说明，卡住的状态给一个能点的下一步。
 * 这里刻意不提供"开始"按钮——平台是拉取模型，点"开始"不会让任务跑起来。
 */
function TaskRunHint({ diagnosis, onSetExecutor }: { diagnosis: TaskDiagnosis; onSetExecutor: () => void }) {
  if (diagnosis.action?.kind === "executor") {
    return (
      <button className="text-button" data-testid="task-set-executor" title={diagnosis.reason} onClick={onSetExecutor}>
        <PlayCircle size={13} /> 设置执行方式
      </button>
    );
  }
  if (diagnosis.action?.kind === "link") {
    return (
      <Link className="text-button" href={diagnosis.action.href ?? "#"} title={diagnosis.reason}>
        {diagnosis.action.label} →
      </Link>
    );
  }
  if (diagnosis.state === "runnable") {
    return <span className="chip" title={diagnosis.reason}>等待 Agent 领取</span>;
  }
  if (diagnosis.state === "in_progress" || diagnosis.state === "leased") {
    return <span className="chip" title={diagnosis.reason}>{diagnosis.label}</span>;
  }
  return null;
}

export default function TasksPage() {
  const { dashboard, projectId, refresh, notify, reviewCenter, loading } = useWorkspace();
  const [filter, setFilter] = useState("all");
  const [modalOpen, setModalOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ title: "", description: "", stage: "modeling", priority: "medium", assignee: "Unassigned", assignee_member_id: "", deadline: "" });
  // 项目成员目录：派单选择器（成员 id → 显示名）
  const [members, setMembers] = useState<ProjectMemberView[]>([]);
  // 建任务时可勾选"已批准且允许下游"的成果物作为输入：这是把内容接进流程的入口（CL-2-04）
  const [inputArtifactIds, setInputArtifactIds] = useState<string[]>([]);
  const [detailId, setDetailId] = useState("");
  const [confirmAction, setConfirmAction] = useState<{ task: Task; target: TaskStatus; label: string; hint: string; danger?: boolean } | null>(null);
  const [assigning, setAssigning] = useState(false);
  const [executorTarget, setExecutorTarget] = useState<Task | null>(null);
  const [executorForm, setExecutorForm] = useState({
    mode: "codex",
    prompt: "",
    command: "",
    cli: "workbuddy exec {prompt}",
    assignee: "Unassigned",
  });
  // 意图对象（AIP-1d）：任务详情里的预算与显式证据要求（服务端读时算执行态与缺口）
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  // 列表角标（COST-1）：哪些任务有证据缺口 / 用量超预算——一次批量取回，避免逐条查
  const [flags, setFlags] = useState<Record<string, TaskFlags>>({});
  // 深链：`/tasks?task=<id>`（桌面端 map://task/<id> 与外部链接都用它）直接打开任务详情。
  // 注意：读 `window.location.search` 而不是 `useSearchParams()`——后者会让页面转为动态渲染，
  // 而本项目**全部页面都是静态预渲染**（改了就得改架构，不值得）。
  const [intentBusy, setIntentBusy] = useState(false);
  const [intentForm, setIntentForm] = useState({
    max_seconds: "",
    max_attempts: "",
    max_tokens: "",
    evidence: [] as EvidenceRequirement[],
  });

  // 项目成员目录：派单只能派给本项目成员，切项目时刷新
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void (async () => {
      try {
        const list = await listProjectMembers(projectId);
        if (!cancelled) setMembers(list);
      } catch {
        if (!cancelled) setMembers([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const agents = dashboard.agents;
  const detailTask = dashboard.tasks.find((task) => task.id === detailId) ?? null;

  /** 任务详情（AIP-1d）：预算执行态与证据缺口由服务端读时算，和任务本体一起返回。 */
  const loadDetail = async (taskId: string) => {
    if (!taskId) {
      setDetail(null);
      return;
    }
    try {
      const loaded = await getTaskDetail(taskId);
      setDetail(loaded);
      setIntentForm({
        max_seconds: loaded.budget_state.max_seconds ? String(loaded.budget_state.max_seconds) : "",
        max_attempts: loaded.budget_state.max_attempts ? String(loaded.budget_state.max_attempts) : "",
        max_tokens: loaded.budget_state.max_tokens ? String(loaded.budget_state.max_tokens) : "",
        evidence: (loaded.task.evidence_requirements ?? []).map((item) => ({ ...item })),
      });
    } catch (error) {
      setDetail(null);
      notify(errorMessage(error, "任务详情读取失败"));
    }
  };

  useEffect(() => {
    const param = new URLSearchParams(window.location.search).get("task");
    if (param) setDetailId(param);
  }, []);

  useEffect(() => {
    void loadDetail(detailId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailId]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void getProjectTaskFlags(projectId)
      .then((rows) => {
        if (cancelled) return;
        setFlags(Object.fromEntries(rows.map((row) => [row.task_id, row])));
      })
      .catch(() => {
        if (!cancelled) setFlags({});
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, dashboard.tasks.length]);

  /** 保存预算与证据要求：空值 = 清除（服务端"出现才生效"语义）。 */
  const saveIntent = async (task: Task) => {
    if (intentBusy) return;
    setIntentBusy(true);
    try {
      const budget =
        intentForm.max_seconds || intentForm.max_attempts || intentForm.max_tokens
          ? {
              max_seconds: intentForm.max_seconds ? Number(intentForm.max_seconds) : null,
              max_attempts: intentForm.max_attempts ? Number(intentForm.max_attempts) : null,
              max_tokens: intentForm.max_tokens ? Number(intentForm.max_tokens) : null,
            }
          : null;
      await updateTask(task.id, {
        budget,
        evidence_requirements: intentForm.evidence
          .filter((item) => item.evidence_type)
          .map((item) => ({ evidence_type: item.evidence_type, min_count: Math.max(1, Number(item.min_count) || 1), note: item.note ?? "" })),
      });
      notify(budget || intentForm.evidence.length ? "预算与证据要求已保存" : "已清除预算与证据要求");
      await refresh();
      await loadDetail(task.id);
    } catch (error) {
      notify(errorMessage(error, "保存失败"));
    } finally {
      setIntentBusy(false);
    }
  };

  const flowContext = {
    tasks: dashboard.tasks,
    handoffs: dashboard.handoffs,
    artifacts: dashboard.artifacts,
    onlineAgents: agents.filter((agent) => String(agent.status) === "online").length,
  };
  const diagnosisOf = (task: Task): TaskDiagnosis => diagnoseTask(task, flowContext);
  // 可作为下游输入的成果物：已批准 + 允许下游（与服务端领取校验同一口径）
  const approvedArtifacts = dashboard.artifacts.filter(
    (artifact) => String(artifact.status) === "APPROVED" && artifact.downstream_allowed !== false,
  );
  const needsExecutor = dashboard.tasks.filter((task) => diagnosisOf(task).state === "no_executor").length;

  const grouped = useMemo(() => {
    const stages = [...STAGE_ORDER, ...dashboard.tasks.map((task) => task.stage).filter((stage) => !STAGE_ORDER.includes(stage))];
    const unique = Array.from(new Set(stages));
    return unique
      .map((stage) => ({ stage, tasks: dashboard.tasks.filter((task) => task.stage === stage) }))
      .filter((group) => group.tasks.length > 0);
  }, [dashboard.tasks]);

  const visible = filter === "all" ? dashboard.tasks : dashboard.tasks.filter((task) => String(task.status) === filter);

  const handleCreate = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!projectId) return;
    if (form.title.trim().length < 2) {
      notify("任务名称至少 2 个字符");
      return;
    }
    setBusy(true);
    try {
      await createTask(projectId, {
        title: form.title.trim(),
        description: form.description,
        stage: form.stage,
        assignee: form.assignee,
        assignee_member_id: form.assignee_member_id || null,
        // datetime-local 是本地时间，转 ISO 再发（否则服务端按 UTC 解析会偏 8 小时）
        deadline: form.deadline ? new Date(form.deadline).toISOString() : null,
        priority: form.priority,
        requires_review: true,
        allow_future_data: false,
        input_artifacts: inputArtifactIds,
      });
      setModalOpen(false);
      setForm({ title: "", description: "", stage: "modeling", priority: "medium", assignee: "Unassigned", assignee_member_id: "", deadline: "" });
      setInputArtifactIds([]);
      await refresh();
      notify(inputArtifactIds.length ? `任务已创建（输入 ${inputArtifactIds.length} 份已批准成果物）` : "任务已创建");
    } catch (error) {
      // 服务端原因直达：例如名称过短的 422 校验详情、非法阶段的 400。
      notify(errorMessage(error, "任务创建失败"));
    } finally {
      setBusy(false);
    }
  };

  const runTransition = async (task: Task, target: TaskStatus, label: string) => {
    setBusy(true);
    try {
      await updateTask(task.id, { status: target });
      await refresh();
      notify(`「${task.title}」已${label}`);
    } catch (error) {
      // 非法流转/审批边界等由服务端判定，这里展示真实原因（invalid_task_transition 等）。
      notify(errorMessage(error, "任务更新失败"));
    } finally {
      setBusy(false);
      setConfirmAction(null);
    }
  };

  const requestTransition = (task: Task, action: (typeof TASK_TRANSITIONS)[string][number]) => {
    if (action.confirm) {
      setConfirmAction({ task, target: action.target, label: action.label, hint: action.hint, danger: action.danger });
      return;
    }
    void runTransition(task, action.target, action.label);
  };

  /** 派单：空串 = 取消指派（回到先到先得）。 */
  const changeDispatch = async (task: Task, assigneeMemberId: string) => {
    setAssigning(true);
    try {
      await updateTask(task.id, { assignee_member_id: assigneeMemberId });
      await refresh();
      const name = members.find((member) => member.member_id === assigneeMemberId)?.display_name;
      notify(assigneeMemberId ? `已派给 ${name ?? assigneeMemberId}（只有其名下设备能领取）` : "已取消指派：回到谁先轮到谁跑");
    } catch (error) {
      notify(errorMessage(error, "派单失败"));
    } finally {
      setAssigning(false);
    }
  };

  /** 改截止时间（空串 = 清除）。 */
  const changeDeadline = async (task: Task, deadline: string) => {
    setAssigning(true);
    try {
      await updateTask(task.id, { deadline });
      await refresh();
      notify(deadline ? `截止时间已设为 ${formatTime(deadline)}` : "已清除截止时间");
    } catch (error) {
      notify(errorMessage(error, "改期失败"));
    } finally {
      setAssigning(false);
    }
  };

  const changeAssignee = async (task: Task, assignee: string) => {
    setAssigning(true);
    try {
      await updateTask(task.id, { assignee });
      await refresh();
      notify(assignee === "Unassigned" ? "已取消指派" : `已指派给 ${assignee}`);
    } catch (error) {
      notify(errorMessage(error, "改派失败"));
    } finally {
      setAssigning(false);
    }
  };

  /** 一键把"缺执行方式"的任务全部设为 Codex：模板包一次生成十几条任务，逐个点太费事。 */
  const startAllWithCodex = async () => {
    const targets = dashboard.tasks.filter((task) => diagnosisOf(task).state === "no_executor");
    if (!targets.length) return;
    setBusy(true);
    let done = 0;
    const failures: string[] = [];
    for (const task of targets) {
      try {
        const policy: Record<string, unknown> = { worker_executor: "codex" };
        const criteria = (task.acceptance_criteria ?? []).slice(0, 5);
        if (criteria.length) {
          const lines = criteria.map((item) => `- ${item}`);
          policy.worker_prompt = [task.title, "", "完成标准：", ...lines].join("\n");
        }
        await updateTask(task.id, { resource_policy: policy });
        done += 1;
      } catch (error) {
        failures.push(`${task.title}：${errorMessage(error, "失败")}`);
      }
    }
    await refresh();
    setBusy(false);
    notify(
      failures.length
        ? `已启动 ${done} 个任务，${failures.length} 个失败（${failures[0]}）`
        : `已把 ${done} 个任务设为 Codex 执行：Agent 会按依赖顺序自动领取`,
    );
  };

  const openExecutor = (task: Task) => {
    const policy = (task.resource_policy ?? {}) as Record<string, unknown>;
    const command = policy.worker_command;
    const isCli = String(policy.worker_executor ?? "") === "cli";
    setExecutorForm({
      mode: isCli ? "cli" : Array.isArray(command) || typeof command === "string" ? "command" : "codex",
      prompt: String(policy.worker_prompt ?? ""),
      command: Array.isArray(command) ? command.join(" ") : String(command ?? ""),
      cli: isCli ? (Array.isArray(command) ? command.join(" ") : String(command ?? "")) : executorForm.cli,
      assignee: task.assignee && task.assignee !== "Unassigned" ? task.assignee : (agents.find((agent) => String(agent.status) === "online")?.agent_id ?? "Unassigned"),
    });
    setExecutorTarget(task);
  };

  const saveExecutor = async () => {
    if (!executorTarget) return;
    let policy: Record<string, unknown>;
    if (executorForm.mode === "codex") {
      policy = { worker_executor: "codex" };
      if (executorForm.prompt.trim()) policy.worker_prompt = executorForm.prompt.trim();
    } else if (executorForm.mode === "cli") {
      const template = executorForm.cli.trim().split(/\s+/).filter(Boolean);
      if (!template.length) {
        notify("请填写 CLI 命令模板（要含 {prompt} 占位符）");
        return;
      }
      if (!template.some((token) => token.includes("{prompt}"))) {
        notify("命令模板里要有 {prompt} 占位符：执行时会被替换成任务提示词");
        return;
      }
      policy = { worker_executor: "cli", worker_command: template };
      if (executorForm.prompt.trim()) policy.worker_prompt = executorForm.prompt.trim();
    } else {
      policy = { worker_command: executorForm.command.trim().split(/\s+/).filter(Boolean) };
      if (!(policy.worker_command as string[]).length) {
        notify("请填写要执行的命令");
        return;
      }
    }
    setBusy(true);
    try {
      await updateTask(executorTarget.id, { resource_policy: policy, assignee: executorForm.assignee });
      await refresh();
      notify(
        executorForm.mode === "codex"
          ? "已设为 Codex 执行：Agent 通常几秒内领取"
          : executorForm.mode === "cli"
            ? "已设为通用 CLI 执行（提示词会注入命令模板）：Agent 通常几秒内领取"
            : "已设为声明式命令：Agent 通常几秒内领取",
      );
      setExecutorTarget(null);
    } catch (error) {
      notify(errorMessage(error, "执行方式保存失败"));
    } finally {
      setBusy(false);
    }
  };

  const transitionButtons = (task: Task, compact = false) => {
    const actions = TASK_TRANSITIONS[String(task.status)] ?? [];
    return actions.map((action) => (
      <button
        key={action.target}
        className="text-button"
        disabled={busy}
        title={action.hint}
        data-testid={`task-transition-${action.target}`}
        onClick={() => requestTransition(task, action)}
      >
        {compact ? action.label : `${action.label} → ${action.target}`}
      </button>
    ));
  };

  return (
    <div className="page-content" id="tasks">
      <PageHeading
        hint={`共 ${dashboard.tasks.length} 个任务 · 内核连上平台后自己领取，不需要在这里点"开始"${projectId ? "" : "（等待项目）"}`}
        actions={
          <>
            <select value={filter} onChange={(event) => setFilter(event.target.value)} style={{ width: "auto" }} data-testid="task-filter">
              <option value="all">全部状态</option>
              {["READY", "RUNNING", "WAITING_REVIEW", "NEEDS_REVISION", "BLOCKED", "APPROVED"].map((status) => (
                <option key={status} value={status}>{status}</option>
              ))}
            </select>
            <button className="button button-primary" onClick={() => setModalOpen(true)} data-testid="task-create-open"><Plus size={15} /> 新建任务</button>
          </>
        }
      />

      {needsExecutor > 0 && (
        <div className="pack-missing" data-testid="tasks-start-hint">
          <strong>{needsExecutor} 个任务还没法执行：缺少执行方式</strong>
          <span>
            任务不是靠界面按钮启动的——Agent 会自己领取「待办」里的任务，但领走后需要知道
            <b>用什么执行</b>。设一次执行方式（Codex 或一条命令），它就会自动跑起来；
            设好之后不需要再点任何东西。
          </span>
          <div className="form-row">
            <button
              className="button button-primary"
              data-testid="tasks-start-all"
              disabled={busy}
              onClick={() => void startAllWithCodex()}
            >
              {busy ? <RefreshCcw size={16} className="spin" /> : <PlayCircle size={16} />} 一键全部设为 Codex（{needsExecutor} 个）
            </button>
            <button
              className="button button-secondary"
              data-testid="tasks-start-first"
              disabled={busy}
              onClick={() => { const target = dashboard.tasks.find((task) => diagnosisOf(task).state === "no_executor"); if (target) openExecutor(target); }}
            >
              只设置第一个（自定义指令）
            </button>
            <Link className="button button-secondary" href="/devices">接入/检查 Agent</Link>
          </div>
        </div>
      )}

      <Panel title="阶段看板" subtitle="按阶段分组查看任务分布" testId="task-board">
        {grouped.length ? (
          <div className="grid grid-4">
            {grouped.map((group) => (
              <div className="task-stage-column" key={group.stage}>
                <div className="task-stage-heading">
                  <strong>{STAGE_LABEL[group.stage] ?? group.stage}</strong>
                  <span>{group.tasks.length} 项</span>
                </div>
                <div className="list">
                  {group.tasks.slice(0, 6).map((task) => {
                    const diagnosis = diagnosisOf(task);
                    return (
                      <div className="list-item list-item-static task-board-item" key={task.id}>
                        <span className={`task-status-rail status-${String(task.status).toLowerCase()}`}>
                          {TASK_STATUS_LABEL[String(task.status)] ?? String(task.status)}
                        </span>
                        <div className="item-copy">
                          <strong>{task.title}</strong>
                          <small>{diagnosis.reason || diagnosis.label}</small>
                        </div>
                        <StatusPill status={String(task.status)} />
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : loading ? <LoadingSkeleton rows={3} label="正在加载任务" /> : <EmptyState>还没有任务；可在「建模模板包」一键物化四问流程</EmptyState>}
      </Panel>

      <Panel title="任务清单" subtitle="点击详情查看依赖、关联成果物与事件" testId="task-list">
        {visible.length ? (
          <div className="table">
            <div className="table-head"><span>任务</span><span>阶段</span><span>负责人</span><span>状态</span><span /></div>
            {visible.map((task) => (
              <div className="table-row" key={task.id} data-testid={`task-row-${task.id}`}>
                <div className="table-title task-table-title">
                  <span className={`task-status-rail status-${String(task.status).toLowerCase()}`}>
                    {TASK_STATUS_LABEL[String(task.status)] ?? String(task.status)}
                  </span>
                  <strong>{task.title}</strong>
                  <small>{task.description || "暂无说明"} · 更新于 {formatTime(task.updated_at)}</small>
                </div>
                <span className="chip" data-label="阶段">{STAGE_LABEL[task.stage] ?? task.stage}</span>
                <span className="hint" data-label="指派/执行">
                  {task.assignee_member_id ? `→ ${members.find((member) => member.member_id === task.assignee_member_id)?.display_name ?? task.assignee_member_id}` : task.assignee}
                </span>
                <span data-label="状态"><StatusPill status={String(task.status)} /></span>
                {task.deadline && (
                  <span className="hint" data-label="截止" data-testid={`task-deadline-${task.id}`}>
                    {new Date(task.deadline) < new Date() ? "⚠ 已过期 " : ""}
                    {formatTime(task.deadline)}
                  </span>
                )}
                <div className="row-actions">
                  {flags[task.id]?.evidence_missing ? (
                    <span className="badge status-neutral" data-testid={`task-gap-${task.id}`} title="显式证据要求还差几条；批准时会拦下">
                      缺证据 {flags[task.id].evidence_missing}
                    </span>
                  ) : null}
                  {flags[task.id]?.usage_overrun ? (
                    <span className="badge status-neutral" data-testid={`task-overrun-${task.id}`} title="本次执行超出预算；批准时会被门禁拦下">
                      超预算
                    </span>
                  ) : null}
                  <TaskRunHint diagnosis={diagnosisOf(task)} onSetExecutor={() => openExecutor(task)} />
                  <button className="text-button" data-testid={`task-detail-${task.id}`} onClick={() => setDetailId(task.id)}>详情</button>
                </div>
              </div>
            ))}
          </div>
        ) : loading ? <LoadingSkeleton rows={3} label="正在加载任务" /> : <EmptyState>没有符合条件的任务</EmptyState>}
      </Panel>

      {modalOpen && (
        <Modal
          title="新建任务"
          subtitle="任务会进入 READY 状态并写入事件时间线"
          testId="task-create-modal"
          onClose={() => setModalOpen(false)}
          actions={
            <>
              <button className="button button-secondary" type="button" onClick={() => setModalOpen(false)}>取消</button>
              <button className="button button-primary" type="submit" form="task-create-form" disabled={busy} data-testid="task-create-submit">
                {busy ? <RefreshCcw size={15} className="spin" /> : <Plus size={15} />} 创建任务
              </button>
            </>
          }
        >
          <form id="task-create-form" onSubmit={handleCreate} className="grid">
            <label className="field"><span>任务名称</span><input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="例如：核对问题三信息边界" /></label>
            <label className="field"><span>任务说明</span><textarea rows={3} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="写清目标、输入和完成标准" /></label>
            <div className="form-grid">
              <label className="field"><span>阶段</span>
                <select value={form.stage} onChange={(event) => setForm({ ...form, stage: event.target.value })}>
                  {STAGE_ORDER.map((stage) => <option key={stage} value={stage}>{STAGE_LABEL[stage]}</option>)}
                </select>
              </label>
              <label className="field"><span>优先级</span>
                <select value={form.priority} onChange={(event) => setForm({ ...form, priority: event.target.value })}>
                  <option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="critical">关键</option>
                </select>
              </label>
            </div>
            <label className="field"><span>指派给（派单）</span>
              <select value={form.assignee_member_id} onChange={(event) => setForm({ ...form, assignee_member_id: event.target.value })} data-testid="task-create-dispatch">
                <option value="">不指派：谁先轮到谁跑</option>
                {members.map((member) => (
                  <option key={member.member_id} value={member.member_id}>
                    {member.display_name}（{member.role === "reviewer" ? "复核人" : member.role}）
                  </option>
                ))}
              </select>
              <small className="hint">派单后只有该成员名下的设备能领取；被派的人会在「我的任务」里看到它。</small>
            </label>
            <label className="field"><span>截止时间（可选）</span>
              <input
                type="datetime-local"
                value={form.deadline}
                onChange={(event) => setForm({ ...form, deadline: event.target.value })}
                data-testid="task-create-deadline"
              />
              <small className="hint">过期后任务不再被自动领取；有截止时间的任务在队列里优先。</small>
            </label>
            <label className="field"><span>执行者标注</span>
              <select value={form.assignee} onChange={(event) => setForm({ ...form, assignee: event.target.value })} data-testid="task-create-assignee">
                {assigneeOptions(agents, form.assignee).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <small className="hint">仅标注：平台在 Agent 领取时会把这里写成该 Agent 的 id；不限制谁能领。</small>
            </label>
            <div className="field">
              <span>输入成果物（可选，可多选）</span>
              {approvedArtifacts.length ? (
                <div className="list" style={{ maxHeight: 200, overflowY: "auto" }} data-testid="task-input-artifacts">
                  {approvedArtifacts.map((artifact) => {
                    const producer = artifact.task_id
                      ? dashboard.tasks.find((task) => task.id === artifact.task_id)?.title ?? `任务 ${String(artifact.task_id).slice(0, 8)}`
                      : "手工/模板创建";
                    return (
                      <label className="list-item list-item-static" key={artifact.id} style={{ cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={inputArtifactIds.includes(artifact.id)}
                          data-testid={`task-input-artifact-${artifact.id}`}
                          onChange={(event) =>
                            setInputArtifactIds((current) =>
                              event.target.checked ? [...current, artifact.id] : current.filter((id) => id !== artifact.id),
                            )
                          }
                        />
                        <div className="item-copy">
                          <strong>{artifact.name}</strong>
                          <small>v{artifact.version} · 产出：{producer} · {artifact.artifact_type}</small>
                        </div>
                        <StatusPill status={String(artifact.status)} />
                      </label>
                    );
                  })}
                </div>
              ) : (
                <div className="hint" data-testid="task-input-artifacts-empty">
                  暂时没有可用作输入的成果物：只有**已批准且允许下游**的内容才能进这里
                  （执行产出默认先进「待复核」，到审核门禁批准后就会出现）。
                </div>
              )}
            </div>
          </form>
        </Modal>
      )}

      {executorTarget && (
        <Modal
          title="设置执行方式"
          subtitle={`${executorTarget.title} · 设好后 Agent 会自动领取并执行`}
          testId="task-executor-modal"
          onClose={() => setExecutorTarget(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setExecutorTarget(null)}>取消</button>
              <button className="button button-primary" disabled={busy} data-testid="task-executor-save" onClick={() => void saveExecutor()}>
                {busy ? <RefreshCcw size={15} className="spin" /> : <PlayCircle size={15} />} 保存并交给 Agent
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <div className="hint">
              任务不会在网页里"点开始"就运行：平台是拉取模型，Agent 自己领「待办」里的任务。
              这里只需要告诉它<b>用什么执行</b>——设完通常几秒内被领走。
            </div>
            <label className="field"><span>执行体</span>
              <select value={executorForm.mode} data-testid="task-executor-mode" onChange={(event) => setExecutorForm({ ...executorForm, mode: event.target.value })}>
                <option value="codex">Codex CLI（推荐：装了 ChatGPT 桌面端即可）</option>
                <option value="cli">通用 CLI（workbuddy / zcode 等，命令模板带 {'{prompt}'} 占位符）</option>
                <option value="command">声明式命令（自己指定 argv）</option>
              </select>
            </label>
            {executorForm.mode === "codex" ? (
              <label className="field"><span>给 Codex 的指令</span>
                <textarea
                  rows={3}
                  value={executorForm.prompt}
                  data-testid="task-executor-prompt"
                  placeholder="留空则用「任务标题 + 说明 + 完成标准」拼成提示词"
                  onChange={(event) => setExecutorForm({ ...executorForm, prompt: event.target.value })}
                />
                <small className="hint">默认只读沙箱；需要写文件时在内核侧用 --codex-sandbox workspace-write 启动。</small>
              </label>
            ) : executorForm.mode === "cli" ? (
              <>
                <label className="field"><span>CLI 命令模板（空格分隔，含 {"{prompt}"} 占位符）</span>
                  <input
                    value={executorForm.cli}
                    data-testid="task-executor-cli"
                    placeholder="例如：workbuddy exec {prompt}"
                    onChange={(event) => setExecutorForm({ ...executorForm, cli: event.target.value })}
                  />
                  <small className="hint">
                    执行时 {"{prompt}"} 会被替换成下面的提示词；stdout 即结果、退出码即成败。zcode 换成你的真实调用方式（如 zcode run {`{prompt}`}）。
                  </small>
                </label>
                <label className="field"><span>提示词（留空则用「任务标题 + 说明 + 完成标准」）</span>
                  <textarea
                    rows={3}
                    value={executorForm.prompt}
                    data-testid="task-executor-cli-prompt"
                    placeholder="给这个 CLI 的指令"
                    onChange={(event) => setExecutorForm({ ...executorForm, prompt: event.target.value })}
                  />
                </label>
              </>
            ) : (
              <label className="field"><span>命令（空格分隔的 argv）</span>
                <input
                  value={executorForm.command}
                  data-testid="task-executor-command"
                  placeholder="例如：python -c print(1)"
                  onChange={(event) => setExecutorForm({ ...executorForm, command: event.target.value })}
                />
                <small className="hint">内核对声明式命令只允许工作区内的可执行文件。</small>
              </label>
            )}
            <label className="field"><span>交给哪个 Agent（可留空，任何有权限的在线 Agent 都能领）</span>
              <select value={executorForm.assignee} onChange={(event) => setExecutorForm({ ...executorForm, assignee: event.target.value })} data-testid="task-executor-assignee">
                {assigneeOptions(agents, executorForm.assignee).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
          </div>
        </Modal>
      )}

      {detailTask && (
        <Modal
          title={detailTask.title}
          subtitle={`${STAGE_LABEL[detailTask.stage] ?? detailTask.stage} · ${detailTask.priority} · 更新于 ${formatTime(detailTask.updated_at)}`}
          wide
          testId="task-detail-modal"
          onClose={() => setDetailId("")}
          actions={
            <>
              {transitionButtons(detailTask)}
              <button className="button button-secondary" onClick={() => setDetailId("")}>关闭</button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 14 }}>
            <div className="chips">
              <StatusPill status={String(detailTask.status)} />
              <span className="chip">阶段 {STAGE_LABEL[detailTask.stage] ?? detailTask.stage}</span>
              <span className="chip">优先级 {detailTask.priority}</span>
              <span className="chip">{detailTask.requires_review ? "需要人工复核" : "无需复核"}</span>
              <span className="chip">{detailTask.allow_future_data ? "允许未来数据" : "禁止未来数据"}</span>
            </div>

            <div className="field">
              <span>最终回答</span>
              {(() => {
                const runs = dashboard.runs.filter((run) => run.task_id === detailTask.id);
                if (!runs.length) return <div className="hint">这个任务还没有执行记录</div>;
                const latest = runs[0];
                const { answer, diagnostics } = splitRunSummary(latest.summary);
                return (
                  <div style={{ display: "grid", gap: 8 }} data-testid="task-runs">
                    {answer ? (
                      <pre className="code-block" style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", maxHeight: 260, overflowY: "auto" }}>
                        {answer}
                      </pre>
                    ) : (
                      <div className="hint">这次执行没有产出回答文本。诊断：{diagnostics || "（无）"}</div>
                    )}
                    <div className="hint">
                      {latest.agent_id} · {formatTime(latest.started_at)} · 状态 {String(latest.status)} · 输出 {latest.output_artifact_ids?.length ?? 0} 个成果物
                      {" · "}
                      <Link className="text-button" href="/runs">到运行控制台看原始输出 →</Link>
                    </div>
                  </div>
                );
              })()}
            </div>

            <div className="field">
              <span>指派给（派单）</span>
              <select
                value={detailTask.assignee_member_id ?? ""}
                disabled={assigning}
                data-testid="task-detail-dispatch"
                onChange={(event) => void changeDispatch(detailTask, event.target.value)}
              >
                <option value="">不指派：谁先轮到谁跑</option>
                {members.map((member) => (
                  <option key={member.member_id} value={member.member_id}>
                    {member.display_name}（{member.role === "reviewer" ? "复核人" : member.role}）
                  </option>
                ))}
              </select>
              <small className="hint">
                派单后只有该成员名下的设备能领取这个任务；不指派则保持先到先得。
                {detailTask.assignee_member_id ? ` 当前：${members.find((m) => m.member_id === detailTask.assignee_member_id)?.display_name ?? detailTask.assignee_member_id}` : ""}
              </small>
            </div>

            <div className="field">
              <span>截止时间</span>
              <input
                type="datetime-local"
                defaultValue={detailTask.deadline ? new Date(detailTask.deadline).toISOString().slice(0, 16) : ""}
                disabled={assigning}
                data-testid="task-detail-deadline"
                onBlur={(event) => {
                  const value = event.target.value;
                  const current = detailTask.deadline ? new Date(detailTask.deadline).toISOString().slice(0, 16) : "";
                  if (value === current) return;
                  void changeDeadline(detailTask, value ? new Date(value).toISOString() : "");
                }}
              />
              <small className="hint">留空 = 清除截止时间；过期任务不会被自动领取。</small>
            </div>

            <div className="field">
              <span>执行者标注</span>
              <select
                value={assigneeOptions(agents, detailTask.assignee).some((option) => option.value === detailTask.assignee) ? detailTask.assignee : "Unassigned"}
                disabled={assigning}
                data-testid="task-detail-assignee"
                onChange={(event) => void changeAssignee(detailTask, event.target.value)}
              >
                {assigneeOptions(agents, detailTask.assignee).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <small className="hint">
                改派只写入标注，不会把任务锁给某个人；平台在 Agent 领取时才把负责人写成该 Agent 的 id。
                当前标注：<code>{detailTask.assignee}</code>
              </small>
            </div>

            <div className="field">
              <span>任务说明</span>
              <div className="hint">{detailTask.description || "暂无说明"}</div>
            </div>

            {detailTask.blocked_reason && (
              <div className="pack-missing"><strong>阻塞原因</strong><span>{detailTask.blocked_reason}</span></div>
            )}

            {detailTask.acceptance_criteria?.length ? (
              <div className="field">
                <span>完成标准</span>
                <div className="list">
                  {detailTask.acceptance_criteria.map((item: string) => (
                    <div className="list-item list-item-static" key={item}>
                      <span className="icon-tile icon-tile-green"><CheckCircle2 size={14} /></span>
                      <div className="item-copy"><small>{item}</small></div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="field" data-testid="task-intent">
              <span>预算与证据要求（意图对象）</span>
              <div className="intent-grid">
                <label className="field">
                  <small className="hint">单次执行上限（秒）</small>
                  <input
                    type="number"
                    min={30}
                    max={86400}
                    value={intentForm.max_seconds}
                    data-testid="task-intent-seconds"
                    onChange={(event) => setIntentForm({ ...intentForm, max_seconds: event.target.value })}
                    placeholder="留空 = 不限"
                  />
                </label>
                <label className="field">
                  <small className="hint">允许领取次数</small>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    value={intentForm.max_attempts}
                    data-testid="task-intent-attempts"
                    onChange={(event) => setIntentForm({ ...intentForm, max_attempts: event.target.value })}
                    placeholder="留空 = 不限"
                  />
                </label>
                <label className="field">
                  <small className="hint">token 上限（未强制）</small>
                  <input
                    type="number"
                    min={1}
                    value={intentForm.max_tokens}
                    data-testid="task-intent-tokens"
                    onChange={(event) => setIntentForm({ ...intentForm, max_tokens: event.target.value })}
                    placeholder="只记录"
                  />
                </label>
              </div>
              {detail ? (
                <small className="hint" data-testid="task-intent-state">
                  已领取 {detail.budget_state.attempts}
                  {detail.budget_state.max_attempts ? ` / ${detail.budget_state.max_attempts}` : ""} 次
                  {detail.budget_state.exhausted ? "（预算已用尽，不再接受领取）" : ""}
                  {detail.budget_state.usage_reported_runs
                    ? ` · 已回报 ${detail.budget_state.tokens_used} tokens（${detail.budget_state.usage_reported_runs} 次执行有数）`
                    : " · 还没有执行体回报过用量"}
                  {detail.budget_state.max_tokens && !detail.budget_state.usage_reported_runs
                    ? "：不回报用量就无法判定 token 上限，超过时会在门禁留痕"
                    : ""}
                </small>
              ) : null}

              <div className="intent-evidence">
                {intentForm.evidence.map((item, index) => (
                  <div className="intent-evidence-row" key={`${item.evidence_type}-${index}`}>
                    <select
                      value={item.evidence_type}
                      data-testid={`task-intent-evidence-type-${index}`}
                      onChange={(event) => {
                        const next = [...intentForm.evidence];
                        next[index] = { ...next[index], evidence_type: event.target.value as EvidenceRequirement["evidence_type"] };
                        setIntentForm({ ...intentForm, evidence: next });
                      }}
                    >
                      <option value="artifact">成果物</option>
                      <option value="run">运行记录</option>
                      <option value="event">事件</option>
                      <option value="external_source">外部来源</option>
                    </select>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={item.min_count}
                      data-testid={`task-intent-evidence-count-${index}`}
                      onChange={(event) => {
                        const next = [...intentForm.evidence];
                        next[index] = { ...next[index], min_count: Number(event.target.value) || 1 };
                        setIntentForm({ ...intentForm, evidence: next });
                      }}
                    />
                    <input
                      value={item.note ?? ""}
                      placeholder="说明（可选）"
                      data-testid={`task-intent-evidence-note-${index}`}
                      onChange={(event) => {
                        const next = [...intentForm.evidence];
                        next[index] = { ...next[index], note: event.target.value };
                        setIntentForm({ ...intentForm, evidence: next });
                      }}
                    />
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setIntentForm({ ...intentForm, evidence: intentForm.evidence.filter((_, i) => i !== index) })}
                    >
                      删除
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="text-button"
                  data-testid="task-intent-evidence-add"
                  onClick={() =>
                    setIntentForm({
                      ...intentForm,
                      evidence: [...intentForm.evidence, { evidence_type: "run", min_count: 1, note: "" }],
                    })
                  }
                >
                  + 添加证据要求
                </button>
              </div>
              {detail?.evidence_gaps.length ? (
                <div className="pack-missing" data-testid="task-intent-gaps">
                  <strong>证据缺口</strong>
                  <span>
                    {detail.evidence_gaps
                      .filter((gap) => gap.missing > 0)
                      .map((gap) => `${gap.evidence_type} 需 ${gap.required} 条、现有 ${gap.present} 条`)
                      .join("；") || "已满足"}
                  </span>
                </div>
              ) : null}
              <div className="link-row">
                <button
                  type="button"
                  className="button button-primary"
                  disabled={intentBusy}
                  data-testid="task-intent-save"
                  onClick={() => void saveIntent(detailTask)}
                >
                  {intentBusy ? <RefreshCcw size={14} className="spin" /> : null} 保存预算与证据要求
                </button>
                <small className="hint">
                  秒上限与领取次数是强制的（租约不会越过秒上限、次数用尽后不再接受领取）；
                  token 上限在执行体回报用量后即可判定（超了会在门禁留痕）；证据要求在批准时校验，缺证据不会自动放过门禁。
                </small>
              </div>
            </div>

            <div className="field">
              <span>依赖任务（{detailTask.dependency_task_ids?.length ?? 0}）</span>
              {detailTask.dependency_task_ids?.length ? (
                <div className="list">
                  {detailTask.dependency_task_ids.map((dependencyId) => {
                    const dependency = dashboard.tasks.find((task) => task.id === dependencyId);
                    return (
                      <div className="list-item list-item-static" key={dependencyId}>
                        <div className="item-copy">
                          <strong>{dependency?.title ?? `未知任务 ${dependencyId.slice(0, 8)}`}</strong>
                          <small>{dependency ? `${STAGE_LABEL[dependency.stage] ?? dependency.stage} · ${dependency.assignee}` : "该任务不在当前项目任务列表中"}</small>
                        </div>
                        {dependency && <StatusPill status={String(dependency.status)} />}
                      </div>
                    );
                  })}
                </div>
              ) : <div className="hint">无前置依赖</div>}
            </div>

            <div className="field">
              <span>关联成果物</span>
              {(() => {
                const produced = dashboard.artifacts.filter((artifact) => artifact.task_id === detailTask.id);
                const consumed = dashboard.artifacts.filter((artifact) => (detailTask.input_artifacts ?? []).includes(artifact.id));
                if (!produced.length && !consumed.length) return <div className="hint">暂无关联成果物</div>;
                return (
                  <div className="list">
                    {produced.map((artifact) => (
                      <div className="list-item list-item-static" key={artifact.id}>
                        <span className="icon-tile icon-tile-blue"><ArrowUpRight size={14} /></span>
                        <div className="item-copy"><strong>{artifact.name}</strong><small>产出 · {artifact.artifact_type} · v{artifact.version}</small></div>
                        <StatusPill status={String(artifact.status)} />
                      </div>
                    ))}
                    {consumed.map((artifact) => (
                      <div className="list-item list-item-static" key={`in-${artifact.id}`}>
                        <span className="icon-tile"><ArrowUpRight size={14} /></span>
                        <div className="item-copy"><strong>{artifact.name}</strong><small>输入 · {artifact.artifact_type} · v{artifact.version}</small></div>
                        <StatusPill status={String(artifact.status)} />
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>

            <div className="field">
              <span>交接（{reviewCenter.handoffs.filter((handoff) => handoff.task_id === detailTask.id).length}）</span>
              {reviewCenter.handoffs.filter((handoff) => handoff.task_id === detailTask.id).length ? (
                <div className="list">
                  {reviewCenter.handoffs.filter((handoff) => handoff.task_id === detailTask.id).map((handoff) => (
                    <div className="list-item list-item-static" key={handoff.id}>
                      <div className="item-copy">
                        <strong>{handoff.objective.slice(0, 60)}</strong>
                        <small>来自 {handoff.sender_agent_id} · {handoff.handoff_type}</small>
                      </div>
                      <StatusPill status={String(handoff.status)} />
                    </div>
                  ))}
                </div>
              ) : <div className="hint">该任务暂无交接记录</div>}
            </div>

            <div className="field">
              <span>执行过程（平台可见的实时反馈）</span>
              {(() => {
                const progress = dashboard.events.filter(
                  (event) =>
                    (event.event_type.startsWith("agent.process.") ||
                      event.event_type.startsWith("agent.agent.") ||
                      event.event_type.startsWith("agent.tool.") ||
                      event.event_type.startsWith("agent.file.")) &&
                    String(event.payload?.event?.payload?.task_id ?? "") === detailTask.id,
                );
                if (!progress.length) {
                  return (
                    <div className="hint">
                      还没有过程上报。任务被领取后，执行体的回复片段、工具调用与文件修改会以事件形式陆续出现在这里
                      （约每 1.5 秒一条、最多 40 条）；终态结果在下方"执行结果"里。
                    </div>
                  );
                }
                return (
                  <div className="list" data-testid="task-progress-list">
                    {progress.map((event) => {
                      const presented = presentEvent(event);
                      return (
                        <div className="list-item list-item-static" key={event.id}>
                          <span className="icon-tile"><PlayCircle size={14} /></span>
                          <div className="item-copy">
                            <strong>{presented.title}</strong>
                            <small>{presented.detail || event.actor} · {formatTime(event.created_at)}</small>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
            </div>

            <div className="field">
              <span>事件（最近 5 条）</span>
              {(() => {
                const events = dashboard.events
                  .filter((event) => event.object_id === detailTask.id || String(event.payload?.task_id ?? "") === detailTask.id)
                  .slice(-5)
                  .reverse();
                if (!events.length) return <div className="hint">暂无该任务的事件</div>;
                return (
                  <div className="list">
                    {events.map((event) => (
                      <div className="list-item list-item-static" key={event.id}>
                        <div className="item-copy"><strong>{event.event_type}</strong><small>{event.actor} · {formatTime(event.created_at)}</small></div>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>

            {(TASK_TRANSITIONS[String(detailTask.status)] ?? []).length === 0 && (
              <div className="hint">
                <Ban size={13} style={{ display: "inline", marginRight: 4 }} />
                {String(detailTask.status) === "APPROVED"
                  ? "已批准的任务不可再改状态；如需修改，请提交复核意见。"
                  : "已取消的任务不可再改状态。"}
              </div>
            )}
          </div>
        </Modal>
      )}

      {confirmAction && (
        <ConfirmDialog
          title={`确认${confirmAction.label}？`}
          description={
            <>
              <strong>{confirmAction.task.title}</strong>
              <div style={{ marginTop: 6 }}>{confirmAction.hint}</div>
              <div className="hint" style={{ marginTop: 6 }}>状态将变为 {confirmAction.target}。</div>
            </>
          }
          confirmLabel={confirmAction.label}
          tone={confirmAction.danger ? "danger" : "default"}
          busy={busy}
          testId="task-transition-confirm"
          onCancel={() => setConfirmAction(null)}
          onConfirm={() => void runTransition(confirmAction.task, confirmAction.target, confirmAction.label)}
        />
      )}
    </div>
  );
}
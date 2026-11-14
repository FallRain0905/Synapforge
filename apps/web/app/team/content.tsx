"use client";

import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, Loader2, RefreshCcw, ShieldCheck, UserMinus, UserPlus, Users } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Modal, Panel, StatusPill } from "../../components/ui";
import {
  CapabilityCatalog,
  MemberWorkload,
  ProjectMemberView,
  Team,
  TeamMemberRow,
  TeamThroughput,
  addTeamMember,
  addProjectMember,
  createTeam,
  errorMessage,
  getCapabilityCatalog,
  getTeamThroughput,
  getTeamWorkload,
  listOrganizations,
  listProjectMembers,
  listProjectEvents,
  listTeamMembers,
  listTeams,
  removeProjectMember,
  removeTeamMember,
  updateProjectMember,
  updateProjectTeam,
} from "../../lib/api";
import { Event as TimelineEvent } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";

const ROLE_LABEL: Record<string, string> = {
  owner: "负责人",
  project_lead: "项目负责人",
  contributor: "成员",
  reviewer: "复核人",
  observer: "观察者",
};

/**
 * 团队与成员（P2）。
 *
 * 三块：成员工作量（谁在忙什么）、当前项目的成员管理（改角色/移出）、团队（建队与成员）。
 * 移出项目会**连带撤销该成员在本项目的设备/Agent 授权**——界面必须说清楚，否则用户
 * 会以为"移出只是看不见了"，而他的机器还在领任务。
 */
export default function TeamPage() {
  const { account, ready, authenticated } = useAuth();
  const { projectId, project, notify } = useWorkspace();
  const isAdmin = Boolean(account?.is_admin);
  const [workload, setWorkload] = useState<MemberWorkload[]>([]);
  const [members, setMembers] = useState<ProjectMemberView[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamMembers, setTeamMembers] = useState<Record<string, TeamMemberRow[]>>({});
  const [organizationId, setOrganizationId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [removeTarget, setRemoveTarget] = useState<ProjectMemberView | null>(null);
  const [teamForm, setTeamForm] = useState({ name: "" });
  const [addForm, setAddForm] = useState<{ team: Team | null; memberId: string; role: string }>({ team: null, memberId: "", role: "contributor" });
  const [catalog, setCatalog] = useState<CapabilityCatalog>({ agents: [], packages: [], unmet_tasks: [] });
  const [expandedAgents, setExpandedAgents] = useState<Record<string, boolean>>({});
  const toggleAgent = useCallback((agentId: string) => {
    setExpandedAgents((current) => ({ ...current, [agentId]: !current[agentId] }));
  }, []);
  const [throughput, setThroughput] = useState<TeamThroughput>({ days: 14, daily: [], members: [] });
  const [membershipEvents, setMembershipEvents] = useState<TimelineEvent[]>([]);
  const [projectTeamId, setProjectTeamId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [workloadRows, organizations] = await Promise.all([getTeamWorkload(), listOrganizations()]);
      setWorkload(workloadRows);
      const orgId = organizations[0]?.id ?? "";
      setOrganizationId(orgId);
      const [catalogData, throughputData] = await Promise.all([
        getCapabilityCatalog().catch(() => ({ agents: [], packages: [], unmet_tasks: [] }) as CapabilityCatalog),
        getTeamThroughput(14).catch(() => ({ days: 14, daily: [], members: [] }) as TeamThroughput),
      ]);
      setCatalog(catalogData);
      setThroughput(throughputData);
      if (projectId) {
        setMembers(await listProjectMembers(projectId).catch(() => []));
        // 成员变更审计：读项目事件流里的三类成员事件
        const events = await listProjectEvents(projectId, 60, true).catch(() => [] as TimelineEvent[]);
        setMembershipEvents(events.filter((event) => String(event.event_type).startsWith("project.member_")).reverse());
      }
      if (orgId) {
        const teamList = await listTeams(orgId).catch(() => [] as Team[]);
        setTeams(teamList);
        const entries = await Promise.all(teamList.map(async (team) => [team.id, await listTeamMembers(team.id).catch(() => [] as TeamMemberRow[])] as const));
        setTeamMembers(Object.fromEntries(entries));
      }
    } catch (error) {
      notify(errorMessage(error, "团队数据读取失败"));
    } finally {
      setLoading(false);
    }
  }, [notify, projectId]);

  useEffect(() => {
    if (!ready || !authenticated) return;
    void load();
  }, [ready, authenticated, load]);


  const changeRole = async (member: ProjectMemberView, role: string) => {
    if (!projectId) return;
    setBusy(member.member_id);
    try {
      await updateProjectMember(projectId, member.member_id, role);
      notify(`已把 ${member.display_name} 的角色改为「${ROLE_LABEL[role] ?? role}」`);
      await load();
    } catch (error) {
      notify(errorMessage(error, "改角色失败"));
    } finally {
      setBusy("");
    }
  };

  const confirmRemove = async () => {
    if (!projectId || !removeTarget) return;
    setBusy(removeTarget.member_id);
    try {
      const result = await removeProjectMember(projectId, removeTarget.member_id);
      notify(
        `已移出 ${removeTarget.display_name}：撤销 ${result.revoked_grants} 项设备/Agent 授权、释放 ${result.released_tasks} 个派单任务`,
      );
      setRemoveTarget(null);
      await load();
    } catch (error) {
      notify(errorMessage(error, "移出项目失败"));
    } finally {
      setBusy("");
    }
  };

  const createNewTeam = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!organizationId || teamForm.name.trim().length < 2) {
      notify("团队名称至少 2 个字符");
      return;
    }
    setBusy("team");
    try {
      await createTeam(organizationId, teamForm.name.trim());
      setTeamForm({ name: "" });
      notify("团队已创建");
      await load();
    } catch (error) {
      notify(errorMessage(error, "建团队失败"));
    } finally {
      setBusy("");
    }
  };

  const submitAddMember = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!addForm.team || !addForm.memberId) {
      notify("先选成员");
      return;
    }
    setBusy("add-team-member");
    try {
      const result = await addTeamMember(addForm.team.id, addForm.memberId, addForm.role);
      notify(`已加入「${addForm.team.name}」，同时加入该团队 ${result.joined_projects} 个项目`);
      setAddForm({ team: null, memberId: "", role: "contributor" });
      await load();
    } catch (error) {
      notify(errorMessage(error, "加入团队失败"));
    } finally {
      setBusy("");
    }
  };

  const leaveTeam = async (team: Team, row: TeamMemberRow) => {
    setBusy(row.member_id);
    try {
      const result = await removeTeamMember(team.id, row.member_id);
      notify(`已把 ${row.display_name} 移出「${team.name}」，同时移出该团队 ${result.left_projects} 个项目`);
      await load();
    } catch (error) {
      notify(errorMessage(error, "移出团队失败"));
    } finally {
      setBusy("");
    }
  };

  /** 项目改归团队。 */
  const changeProjectTeam = async (teamId: string) => {
    if (!projectId) return;
    setBusy("project-team");
    try {
      await updateProjectTeam(projectId, teamId || null);
      setProjectTeamId(teamId);
      notify(teamId ? "项目已归到该团队（团队成员不会被自动带入）" : "项目已脱离团队");
    } catch (error) {
      notify(errorMessage(error, "改项目团队失败"));
    } finally {
      setBusy("");
    }
  };

  /** 从审计里恢复一个被移出的成员：按当时的角色加回去。 */
  const restoreMember = async (event: TimelineEvent) => {
    if (!projectId) return;
    const memberId = String(event.payload?.member_id ?? "");
    const role = String(event.payload?.role ?? "contributor");
    if (!memberId) return;
    setBusy(memberId);
    try {
      await addProjectMember(projectId, memberId, role);
      notify(`已按原角色「${ROLE_LABEL[role] ?? role}」恢复该成员`);
      await load();
    } catch (error) {
      notify(errorMessage(error, "恢复成员失败"));
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    // 项目归属团队：随项目切换同步（project.team_id 由工作区上下文带出来）
    setProjectTeamId(project?.team_id ?? "");
  }, [project?.team_id]);

  const loadOf = (row: MemberWorkload) => row.assigned_open + row.running;
  const busiest = [...workload].sort((a, b) => loadOf(b) - loadOf(a))[0];
  const idle = workload.filter((row) => row.status === "active" && loadOf(row) === 0 && row.agents > 0).length;

  return (
    <div className="page-content" id="team">
      <PageHeading
        hint="谁在忙什么、当前项目有谁、有哪些团队"
        actions={
          <button className="button button-secondary" onClick={() => void load()} disabled={loading} data-testid="team-refresh">
            {loading ? <Loader2 size={15} className="spin" /> : <RefreshCcw size={15} />} 刷新
          </button>
        }
      />

      <section className="metrics-grid">
        <Metric label="成员" value={workload.length} detail={`${workload.filter((row) => row.status === "active").length} 人在职`} />
        <Metric label="在忙的机器" value={workload.reduce((sum, row) => sum + row.running, 0)} detail="正在执行任务" />
        <Metric label="派出去尚未完成" value={workload.reduce((sum, row) => sum + row.assigned_open, 0)} detail="指派给人的待办" tone={busiest && loadOf(busiest) > 3 ? "warning" : "default"} />
        <Metric label="有机器但闲着" value={idle} detail="可以派活的成员" />
      </section>

      <Panel
        title="成员工作量"
        subtitle={busiest ? `最忙：${busiest.display_name}（${loadOf(busiest)} 项在身）` : "谁身上有多少活"}
        testId="team-workload"
      >
        {loading ? (
          <LoadingSkeleton rows={3} label="正在加载成员" />
        ) : !workload.length ? (
          <EmptyState><Users size={16} /> 还没有成员</EmptyState>
        ) : (
          <div className="table">
            <div className="table-head"><span>成员</span><span>占用</span><span>机器</span><span>团队</span><span /></div>
            {workload.map((row) => (
              <div className="table-row" key={row.member_id} data-testid={`workload-${row.member_id}`}>
                <div className="table-title">
                  <strong>
                    {row.display_name}
                    {row.is_admin && <span className="badge status-violet" style={{ marginLeft: 8 }}>管理员</span>}
                  </strong>
                  <small>{row.email} · 参与 {row.projects} 个项目 · 累计完成 {row.completed} 个任务</small>
                </div>
                <span className="chip" data-label="占用">
                  派单 {row.assigned_open} · 执行中 {row.running}
                </span>
                <span className="hint" data-label="机器">
                  {row.agents} 个 Agent / {row.devices_active}在线
                </span>
                <span className="hint" data-label="团队">{row.teams.length ? row.teams.join("、") : "—"}</span>
                <div className="row-actions">
                  <StatusPill status={row.status === "active" ? "APPROVED" : "BLOCKED"} label={row.status === "active" ? "在职" : "已停用"} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title={project ? `${project.name} 的成员` : "项目成员"}
        subtitle="改角色或移出项目；移出会撤销该成员在本项目的设备与 Agent 授权"
        testId="team-project-members"
        actions={
          isAdmin && projectId ? (
            <select
              value={projectTeamId}
              disabled={busy === "project-team"}
              data-testid="project-team-select"
              onChange={(event) => void changeProjectTeam(event.target.value)}
              style={{ width: "auto" }}
            >
              <option value="">不归属团队</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>{team.name}</option>
              ))}
            </select>
          ) : null
        }
      >
        {!projectId ? (
          <EmptyState>先在顶栏选一个项目</EmptyState>
        ) : loading ? (
          <LoadingSkeleton rows={2} label="正在加载项目成员" />
        ) : (
          <div className="table">
            <div className="table-head"><span>成员</span><span>角色</span><span>状态</span><span /></div>
            {members.map((member) => (
              <div className="table-row" key={member.member_id} data-testid={`project-member-${member.member_id}`}>
                <div className="table-title">
                  <strong>{member.display_name}</strong>
                  <small>{member.email}</small>
                </div>
                <span data-label="角色">
                  {isAdmin ? (
                    <select
                      value={member.role}
                      disabled={busy === member.member_id}
                      data-testid={`member-role-${member.member_id}`}
                      onChange={(event) => void changeRole(member, event.target.value)}
                      style={{ width: "auto" }}
                    >
                      {Object.entries(ROLE_LABEL).map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                  ) : (
                    <span className="chip">{ROLE_LABEL[member.role] ?? member.role}</span>
                  )}
                </span>
                <span data-label="状态">
                  <StatusPill status={member.status === "active" ? "APPROVED" : "BLOCKED"} label={member.status === "active" ? "在职" : member.status} />
                </span>
                <div className="row-actions">
                  {isAdmin && (
                    <button
                      className="text-button"
                      disabled={busy === member.member_id || member.member_id === account?.member.id}
                      onClick={() => setRemoveTarget(member)}
                      data-testid={`member-remove-${member.member_id}`}
                    >
                      <UserMinus size={13} /> 移出项目
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        {!isAdmin && <small className="hint">只有管理员能改项目角色或移出成员（查看不受限）。</small>}
      </Panel>

      <Panel title="团队" subtitle="团队是协作单元：加入团队会自动加入该团队的项目" testId="team-list">
        {isAdmin && (
          <form className="form-row" onSubmit={createNewTeam}>
            <input
              value={teamForm.name}
              onChange={(event) => setTeamForm({ name: event.target.value })}
              placeholder="新团队名称，例如：2026 国赛 A 队"
              data-testid="team-create-name"
            />
            <button className="button button-primary" type="submit" disabled={busy === "team"} data-testid="team-create">
              {busy === "team" && <Loader2 size={15} className="spin" />} <ShieldCheck size={15} /> 建团队
            </button>
          </form>
        )}
        {loading ? (
          <LoadingSkeleton rows={2} label="正在加载团队" />
        ) : !teams.length ? (
          <EmptyState><Activity size={16} /> 还没有团队{isAdmin ? "——建一个然后把队友加进去" : ""}</EmptyState>
        ) : (
          teams.map((team) => (
            <div key={team.id} data-testid={`team-${team.id}`}>
              <div className="team-heading">
                <strong>{team.name}</strong>
                <small>{(teamMembers[team.id] ?? []).length} 人</small>
                {isAdmin && (
                  <button className="text-button" onClick={() => setAddForm({ team, memberId: "", role: "contributor" })} data-testid={`team-add-open-${team.id}`}>
                    <UserPlus size={13} /> 加人
                  </button>
                )}
              </div>
              <div className="list">
                {(teamMembers[team.id] ?? []).map((row) => (
                  <div className="list-item list-item-static" key={row.member_id}>
                    <span className="icon-tile"><Users size={15} /></span>
                    <div className="item-copy">
                      <strong>{row.display_name}</strong>
                      <small>
                        {row.email} · {ROLE_LABEL[row.role] ?? row.role}
                      </small>
                    </div>
                    {isAdmin && (
                      <div className="list-actions">
                        <button
                          className="text-button"
                          disabled={busy === row.member_id}
                          onClick={() => void leaveTeam(team, row)}
                          data-testid={`team-remove-${team.id}-${row.member_id}`}
                        >
                          移出团队
                        </button>
                      </div>
                    )}
                  </div>
                ))}
                {!(teamMembers[team.id] ?? []).length && <small className="hint">还没有成员</small>}
              </div>
            </div>
          ))
        )}
      </Panel>

      <Panel
        title="成员变更审计"
        subtitle="谁在什么时候被加入、改角色或移出；移出的可按原角色一键恢复"
        testId="team-audit"
      >
        {!projectId ? (
          <EmptyState>先在顶栏选一个项目</EmptyState>
        ) : !membershipEvents.length ? (
          <EmptyState>这个项目还没有成员变更记录</EmptyState>
        ) : (
          <div className="list">
            {membershipEvents.slice(0, 12).map((event) => {
              const memberId = String(event.payload?.member_id ?? "");
              // 姓名优先取事件里的，其次从成员工作量表查（被移出的人也在表里），最后退回 id
              const displayName =
                (event.payload?.display_name ? String(event.payload.display_name) : "") ||
                workload.find((row) => row.member_id === memberId)?.display_name ||
                memberId;
              const role = event.payload?.role ? String(event.payload.role) : "";
              const removed = String(event.event_type) === "project.member_removed";
              const inProject = members.some((member) => member.member_id === memberId);
              return (
                <div className="list-item list-item-static" key={event.id}>
                  <span className={`icon-tile ${removed ? "icon-tile-red" : "icon-tile-green"}`}>
                    {removed ? <UserMinus size={15} /> : <UserPlus size={15} />}
                  </span>
                  <div className="item-copy">
                    <strong>
                      {removed ? "移出 " : String(event.event_type) === "project.member_role_changed" ? "改角色 " : "加入 "}
                      {displayName}
                      {role ? `（${ROLE_LABEL[role] ?? role}）` : ""}
                    </strong>
                    <small>
                      {formatTime(event.created_at)}
                      {removed && event.payload?.revoked_grants !== undefined
                        ? ` · 撤销 ${event.payload.revoked_grants} 项授权 · 释放 ${event.payload.released_tasks ?? 0} 个派单`
                        : ""}
                    </small>
                  </div>
                  <div className="list-actions">
                    {removed && !inProject && isAdmin ? (
                      <button className="text-button" disabled={busy === memberId} onClick={() => void restoreMember(event)} data-testid={`member-restore-${memberId}`}>
                        按原角色恢复
                      </button>
                    ) : (
                      <span className="badge status-neutral">{inProject ? "当前在项目内" : "已移出"}</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      <Panel
        title="能力目录"
        subtitle="技能（带版本）与授权范围分栏；没人能跑的任务旁边给出「这几台差哪一项」"
        testId="team-capabilities"
      >
        {catalog.unmet_tasks.length > 0 && (
          <div className="pack-missing" data-testid="capability-unmet">
            <AlertTriangle size={14} /> 有 {catalog.unmet_tasks.length} 个任务没人能跑：
            {catalog.unmet_tasks.slice(0, 3).map((item) => `${item.title}（缺 ${item.missing_capabilities.join("、")}）`).join("；")}
            。要么给机器声明这些技能，要么改任务的要求。
          </div>
        )}
        {catalog.unmet_tasks.some((item) => item.candidates.length > 0) && (
          <div className="candidate-hints" data-testid="capability-candidates">
            {catalog.unmet_tasks
              .filter((item) => item.candidates.length > 0)
              .slice(0, 5)
              .map((item) => (
                <div className="candidate-hint" key={item.task_id}>
                  <strong>{item.title}</strong>
                  <small>
                    缺 {item.missing_capabilities.join("、")}
                    {item.candidates[0] ? ` · 最接近的是 ${item.candidates[0].display_name}（${item.candidates[0].reason}）` : ""}
                  </small>
                  <span className="chips">
                    {item.candidates
                      // 同一个展示名的多台机器合并成一条，避免"三行一模一样的芯片"
                      .filter((candidate, index, all) => all.findIndex((other) => other.display_name === candidate.display_name) === index)
                      .slice(0, 3)
                      .map((candidate) => (
                        <span className={`chip ${candidate.missing_skills.length ? "chip-muted" : ""}`} key={candidate.agent_id}>
                          {candidate.display_name}
                          {candidate.missing_skills.length ? ` 缺 ${candidate.missing_skills.join("、")}` : " 全满足"}
                          {candidate.online ? "" : " · 离线"}
                        </span>
                      ))}
                  </span>
                </div>
              ))}
          </div>
        )}
        {catalog.packages.length > 0 && (
          <div className="package-rollup" data-testid="capability-packages">
            <div className="package-head">执行体程序包（同一执行体多实例）</div>
            {catalog.packages.map((item) => (
              <div className="package-row" key={item.package_id} data-testid={`package-${item.package_id}`}>
                <span className="package-id">
                  {item.package_id}
                  {item.source === "reported" ? (
                    <span className="badge status-neutral">内核上报</span>
                  ) : (
                    <span className="badge status-neutral" title="平台按设备探测值推断，不是内核自报">推断</span>
                  )}
                </span>
                <span className="hint">
                  {item.instances} 台（{item.instances_online} 在线）
                </span>
                <span className="hint">
                  {item.total ? `成功率 ${(item.success_rate ?? 0).toFixed(2)}（${item.total} 次）` : "还没有执行记录"}
                </span>
                <span className="chips">
                  {item.skills.slice(0, 4).map((skill) => (
                    <span className="chip" key={skill}>{skill}</span>
                  ))}
                </span>
              </div>
            ))}
          </div>
        )}
        {!catalog.agents.length ? (
          <EmptyState>还没有执行体接入；接入后这里会显示它声明的技能卡</EmptyState>
        ) : (
          <div className="table">
            <div className="table-head"><span>执行体</span><span>技能</span><span>包 / 实例</span><span>设备 · 归属</span><span /></div>
            {catalog.agents.map((agent) => {
              const expanded = Boolean(expandedAgents[agent.agent_id]);
              return (
                <div className="table-row" key={agent.agent_id} data-testid={`capability-${agent.agent_id}`}>
                  <div className="table-title">
                    <strong>{agent.display_name}</strong>
                    <small>
                      {agent.agent_id}
                      {agent.last_seen ? ` · 最近心跳 ${formatTime(agent.last_seen)}` : ""}
                      {agent.runs_total ? ` · 成功率 ${(agent.success_rate ?? 0).toFixed(2)}（${agent.runs_total} 次）` : ""}
                    </small>
                  </div>
                  <span className="chips" data-label="技能">
                    {agent.capabilities.length ? (
                      <>
                        {(expanded ? agent.capabilities : agent.capabilities.slice(0, 8)).map((capability) => (
                          <span className="chip" key={capability}>
                            {capability}
                            {agent.skill_versions[capability] ? <em className="chip-version">@{agent.skill_versions[capability]}</em> : null}
                          </span>
                        ))}
                        {agent.capabilities.length > 8 && !expanded ? <span className="hint">共 {agent.capabilities.length} 项</span> : null}
                      </>
                    ) : (
                      <span className="hint">未声明任何技能</span>
                    )}
                    {agent.cards.length > 0 || agent.scope_capabilities.length > 0 ? (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => toggleAgent(agent.agent_id)}
                        data-testid={`capability-toggle-${agent.agent_id}`}
                      >
                        {expanded ? "收起" : "详情"}
                      </button>
                    ) : null}
                  </span>
                  <span className="hint" data-label="包 / 实例">
                    {agent.package_id ? (
                      <>
                        {agent.package_id}
                        {agent.package_source === "inferred" ? <span className="hint">（推断）</span> : null}
                        <br />
                        <small>{agent.instance_id}</small>
                      </>
                    ) : (
                      "—"
                    )}
                  </span>
                  <span className="hint" data-label="设备 · 归属">
                    {agent.devices.length} 台（{agent.devices_online} 在线）
                    <br />
                    <small>{agent.owner_name}</small>
                  </span>
                  <div className="row-actions">
                    <StatusPill status={agent.agent_status === "online" ? "APPROVED" : "DRAFT"} label={agent.agent_status} />
                  </div>
                  {expanded && (
                    <div className="agent-cards" data-testid={`capability-cards-${agent.agent_id}`}>
                      {agent.cards.length ? (
                        agent.cards.map((card) => (
                          <div className="agent-card" key={`${card.skill}@${card.version}`}>
                            <strong>
                              {card.skill}
                              {card.version ? `@${card.version}` : ""}
                            </strong>
                            <small>
                              输入 {card.inputs.length ? card.inputs.join("、") : "—"} · 产出{" "}
                              {card.outputs.length ? card.outputs.join("、") : "—"}
                              {card.description ? ` · ${card.description}` : ""}
                            </small>
                          </div>
                        ))
                      ) : (
                        <small className="hint">
                          这个执行体只声明了技能名（老写法），没有能力卡；可以给注册命令加 --capability-card 补上版本与输入输出。
                        </small>
                      )}
                      {agent.scope_capabilities.length > 0 && (
                        <small className="hint" data-testid={`capability-scopes-${agent.agent_id}`}>
                          授权范围（不参与任务匹配）：{agent.scope_capabilities.join("、")}
                        </small>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      <Panel title={`近 ${throughput.days} 天吞吐`} subtitle="按执行完成时间统计（数据来自 Run 的执行归属）" testId="team-throughput">
        {!throughput.daily.length ? (
          <EmptyState>这段时间还没有执行记录</EmptyState>
        ) : (
          <>
            <div className="throughput-chart" data-testid="throughput-chart">
              {throughput.daily.map((day) => {
                const peak = Math.max(...throughput.daily.map((item) => item.total), 1);
                const height = Math.max(6, Math.round((day.total / peak) * 72));
                const failedHeight = day.total ? Math.round((day.failed / day.total) * height) : 0;
                return (
                  <div className="throughput-bar" key={day.day} title={`${day.day}：成功 ${day.succeeded} · 失败 ${day.failed}`}>
                    <div className="throughput-bar-stack" style={{ height }}>
                      {failedHeight > 0 && <span className="throughput-bar-failed" style={{ height: failedHeight }} />}
                    </div>
                    <small>{day.day.slice(5)}</small>
                  </div>
                );
              })}
            </div>
            <div className="list">
              {throughput.members.slice(0, 6).map((row) => (
                <div className="list-item list-item-static" key={row.member_id}>
                  <span className="icon-tile icon-tile-blue"><Activity size={15} /></span>
                  <div className="item-copy">
                    <strong>{row.display_name}</strong>
                    <small>完成 {row.succeeded} · 失败 {row.failed}</small>
                  </div>
                  <span className="badge status-neutral">{row.total} 次执行</span>
                </div>
              ))}
            </div>
          </>
        )}
      </Panel>

      <Panel title="口径提醒" subtitle="当前的产品决定，避免误解" testId="team-notes">
        <div className="list">
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-amber"><AlertTriangle size={15} /></span>
            <div className="item-copy">
              <strong>移出项目不是"看不见"那么简单</strong>
              <small>会同时撤销该成员名下设备/Agent 在这个项目的授权（否则他的机器还会继续领任务），并把他名下未完成的派单任务释放回公共队列。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-blue"><Users size={15} /></span>
            <div className="item-copy">
              <strong>新建项目会自动带上现有成员</strong>
              <small>创建者是项目负责人，其余在职成员以"成员"身份加入，避免"建完项目队友看不见"。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-amber"><AlertTriangle size={15} /></span>
            <div className="item-copy">
              <strong>项目改归团队不会自动带人</strong>
              <small>改归属只写 project.team_id；要让某队成员都进来，请把成员加入该团队（入队会自动加入团队的全部项目）。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile"><ShieldCheck size={15} /></span>
            <div className="item-copy">
              <strong>一个部署 = 一个组织</strong>
              <small>组织内可以有多个团队（本轮实现的协作单元）；跨组织多租户隔离尚未做（运行时是 SQLite，RLS 只在 PostgreSQL 路径生效）。</small>
            </div>
          </div>
        </div>
      </Panel>

      {removeTarget && (
        <ConfirmDialog
          title="移出项目成员"
          description={
            <span>
              把 <strong>{removeTarget.display_name}</strong> 移出「{project?.name ?? "当前项目"}」？
              会同时撤销他在本项目的设备/Agent 授权，并释放派给他但未完成的任务。
            </span>
          }
          tone="danger"
          confirmLabel="移出"
          busy={busy === removeTarget.member_id}
          onCancel={() => setRemoveTarget(null)}
          onConfirm={() => void confirmRemove()}
        />
      )}

      {addForm.team && (
        <Modal title={`把成员加入「${addForm.team.name}」`} subtitle="加入团队会同时加入该团队的所有项目" onClose={() => setAddForm({ team: null, memberId: "", role: "contributor" })}>
          <form className="auth-form" onSubmit={submitAddMember} data-testid="team-add-form">
            <label className="field">
              <span>成员</span>
              <select value={addForm.memberId} onChange={(event) => setAddForm({ ...addForm, memberId: event.target.value })} data-testid="team-add-member">
                <option value="">选择成员…</option>
                {workload
                  .filter((row) => row.status === "active")
                  .map((row) => (
                    <option key={row.member_id} value={row.member_id}>
                      {row.display_name}（{row.email}）
                    </option>
                  ))}
              </select>
            </label>
            <label className="field">
              <span>团队角色</span>
              <select value={addForm.role} onChange={(event) => setAddForm({ ...addForm, role: event.target.value })} data-testid="team-add-role">
                {Object.entries(ROLE_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            <div className="modal-actions" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="button button-secondary" type="button" onClick={() => setAddForm({ team: null, memberId: "", role: "contributor" })}>取消</button>
              <button className="button button-primary" type="submit" disabled={busy === "add-team-member"} data-testid="team-add-submit">
                {busy === "add-team-member" && <Loader2 size={15} className="spin" />} 加入
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
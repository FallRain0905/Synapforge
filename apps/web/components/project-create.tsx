"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, FolderPlus, RefreshCcw } from "lucide-react";
import { ConfirmDialog, Modal } from "./ui";
import { CompetitionPackSummary, Team, createProject, errorMessage, getCompetitionPacks, listOrganizations, listTeams } from "../lib/api";
import { useWorkspace } from "../lib/workspace";

/**
 * 新建项目：补齐产品第一个动作的入口。
 *
 * 成功后自动选中新项目并跳到总览——用户不需要再去找刚建的项目。
 */
export function CreateProjectModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const { notify, refresh, selectProject } = useWorkspace();
  const [packs, setPacks] = useState<CompetitionPackSummary[]>([]);
  const [form, setForm] = useState({
    name: "",
    competition_pack: "",
    problem_code: "",
    description: "",
    goal: "",
    target_member_count: "",
    task_mode: "manual" as "manual" | "hybrid" | "auto",
    team_id: "",
  });
  const [teams, setTeams] = useState<Team[]>([]);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const dirty = Boolean(form.name.trim() || form.description.trim() || form.goal.trim());
  const requestClose = () => {
    if (busy) return;
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };

  useEffect(() => {
    if (!open) return;
    setFailure("");
    void (async () => {
      try {
        const list = await getCompetitionPacks();
        setPacks(list);
        // 团队下拉：项目可以归到某个团队（团队成员随后自动获得该项目）
        const organizations = await listOrganizations().catch(() => []);
        if (organizations[0]) setTeams(await listTeams(organizations[0].id).catch(() => []));
        setForm((current) => ({
          ...current,
          competition_pack: current.competition_pack || list[0]?.pack_id || "cumcm-2026",
          problem_code: current.problem_code || list[0]?.default_problem_code || "",
        }));
      } catch {
        // 模板包列表拉不到不影响建项目：后端有默认 pack。
        setForm((current) => ({ ...current, competition_pack: current.competition_pack || "cumcm-2026" }));
      }
    })();
  }, [open]);

  const selectedPack = useMemo(
    () => packs.find((pack) => pack.pack_id === form.competition_pack) ?? null,
    [packs, form.competition_pack],
  );

  const submit = async () => {
    const name = form.name.trim();
    if (name.length < 2) {
      setFailure("项目名称至少 2 个字符");
      return;
    }
    setBusy(true);
    setFailure("");
    try {
      const project = await createProject({
        name,
        competition_pack: form.competition_pack || "cumcm-2026",
        problem_code: form.problem_code || null,
        description: form.description.trim(),
        goal: form.goal.trim(),
        target_member_count: form.target_member_count ? Number(form.target_member_count) : null,
        task_mode: form.task_mode,
        team_id: form.team_id || null,
      });
      selectProject(project.id);
      await refresh(project.id);
      notify(`项目「${project.name}」已创建`);
      onClose();
      setForm({
        name: "",
        competition_pack: form.competition_pack,
        problem_code: form.problem_code,
        description: "",
        goal: "",
        target_member_count: "",
        task_mode: "manual",
        team_id: form.team_id,
      });
      // 新建后直接进工作区：这是主工作区域，不用先去总览再找
      router.push(`/workspace?project=${project.id}`);
    } catch (error) {
      // 服务端原因直接呈现（例如名称过短的 422 校验详情）。
      setFailure(errorMessage(error, "项目创建失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="新建项目"
      subtitle="项目是任务、成果物与知识库的归属边界"
      onClose={busy ? () => undefined : requestClose}
      testId="create-project-modal"
      actions={
        <>
          <button className="button button-secondary" onClick={requestClose} disabled={busy}>取消</button>
          <button className="button button-primary" onClick={() => void submit()} disabled={busy} data-testid="create-project-submit">
            {busy ? <RefreshCcw size={15} className="spin" /> : <FolderPlus size={15} />} 创建项目
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        <label className="field"><span>项目名称（必填，至少 2 个字符）</span>
          <input
            value={form.name}
            autoFocus
            placeholder="例如：2026 国赛 C 题 · 风光储能协同优化"
            data-testid="create-project-name"
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            onKeyDown={(event) => { if (event.key === "Enter" && !busy) void submit(); }}
          />
        </label>

        <div className="form-row">
          <label className="field" style={{ flex: "1 1 220px" }}><span>竞赛模板包</span>
            <select
              value={form.competition_pack}
              data-testid="create-project-pack"
              onChange={(event) => {
                const pack = packs.find((item) => item.pack_id === event.target.value);
                setForm({
                  ...form,
                  competition_pack: event.target.value,
                  problem_code: pack?.default_problem_code ?? form.problem_code,
                });
              }}
            >
              {packs.length
                ? packs.map((pack) => <option key={pack.pack_id} value={pack.pack_id}>{pack.display_name} v{pack.version}</option>)
                : <option value={form.competition_pack}>{form.competition_pack}</option>}
            </select>
          </label>

          <label className="field" style={{ flex: "0 1 160px" }}><span>题号</span>
            <select
              value={form.problem_code}
              data-testid="create-project-problem"
              onChange={(event) => setForm({ ...form, problem_code: event.target.value })}
            >
              <option value="">未指定</option>
              {(selectedPack?.problem_codes ?? ["A", "B", "C", "D", "E"]).map((code) => <option key={code} value={code}>{code} 题</option>)}
            </select>
          </label>

            <label className="field">
              <span>归属团队（可选）</span>
              <select value={form.team_id} onChange={(event) => setForm({ ...form, team_id: event.target.value })} data-testid="project-create-team">
                <option value="">不指定</option>
                {teams.map((team) => (
                  <option key={team.id} value={team.id}>{team.name}</option>
                ))}
              </select>
              <small className="hint">团队是协作单元：加入团队的成员会自动加入该团队下的项目。</small>
            </label>
        </div>

        <label className="field"><span>项目说明</span>
          <input
            value={form.description}
            placeholder="一句话说明这个项目要解决什么"
            data-testid="create-project-description"
            onChange={(event) => setForm({ ...form, description: event.target.value })}
          />
        </label>

        <label className="field"><span>项目目标（推荐填写：它会成为工作区标题下的一句话）</span>
          <input
            value={form.goal}
            placeholder="例如：拿到国赛一等奖；把风光储能协同优化做到可复现"
            data-testid="create-project-goal"
            onChange={(event) => setForm({ ...form, goal: event.target.value })}
          />
        </label>

        <div className="form-row">
          <label className="field" style={{ flex: "0 1 160px" }}><span>目标人数</span>
            <input
              type="number"
              min={1}
              max={200}
              value={form.target_member_count}
              placeholder="例如：4"
              data-testid="create-project-members"
              onChange={(event) => setForm({ ...form, target_member_count: event.target.value })}
            />
          </label>

          <label className="field" style={{ flex: "1 1 220px" }}><span>任务推进方式</span>
            <select
              value={form.task_mode}
              data-testid="create-project-mode"
              onChange={(event) => setForm({ ...form, task_mode: event.target.value as "manual" | "hybrid" | "auto" })}
            >
              <option value="manual">队长派单（默认）</option>
              <option value="hybrid">派单 + 成员认领</option>
              <option value="auto">模板全自动（可选，队长可随时暂停）</option>
            </select>
            <small className="hint">默认不是全自动：任务由队长分配、成员在「我的任务」查看。全自动按模板阶段推进。</small>
          </label>
        </div>

        {selectedPack && (
          <div className="hint">
            <CheckCircle2 size={13} style={{ display: "inline", marginRight: 4, color: "var(--green)" }} />
            {selectedPack.display_name} 内置 {selectedPack.template_count} 个模板骨架、{selectedPack.dag_task_count} 个流程任务；下一步可在「建模模板包」一键物化。
          </div>
        )}
        {failure && <div className="pack-missing" data-testid="create-project-error"><strong>创建失败</strong><span>{failure}</span></div>}
      </div>

      {confirmDiscard && (
        <ConfirmDialog
          title="放弃已填写的内容？"
          description="项目还没有创建，关闭后名称与说明不会保留。"
          confirmLabel="放弃并关闭"
          tone="danger"
          onCancel={() => setConfirmDiscard(false)}
          onConfirm={() => { setConfirmDiscard(false); onClose(); }}
          testId="create-project-discard"
        />
      )}
    </Modal>
  );
}
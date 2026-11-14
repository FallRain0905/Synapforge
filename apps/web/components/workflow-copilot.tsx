"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, Eye, Play, Plus, Send, Sparkles } from "lucide-react";
import { Markdown } from "./markdown";
import { EmptyState, LoadingSkeleton, Panel, StatusPill } from "./ui";
import { formatChoiceAnswer, parseChoiceResponse, type ChoiceRequest } from "../lib/agent-choice";
import {
  answerWorkflowCopilotChoice,
  createWorkflowCopilotSession,
  errorMessage,
  getWorkflowCopilotSession,
  listWorkflowCopilotSessions,
  previewWorkflowCopilotDraft,
  publishWorkflowCopilotDraft,
  runWorkflowCopilotPublished,
  sendWorkflowCopilotTurn,
  type WorkflowCopilotSession,
  type WorkflowCopilotTurnResult,
} from "../lib/api";

function latestChoice(content: string): ChoiceRequest | null {
  return parseChoiceResponse(content).choice;
}

export function WorkflowCopilot({ projectId, notify }: { projectId: string; notify: (message: string) => void }) {
  const [sessions, setSessions] = useState<WorkflowCopilotSession[] | null>(null);
  const [session, setSession] = useState<WorkflowCopilotSession | null>(null);
  const [message, setMessage] = useState("");
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const refreshSessions = async () => {
    const next = await listWorkflowCopilotSessions(projectId);
    setSessions(next);
    if (!session && next[0]) await openSession(next[0].id);
  };

  const openSession = async (id: string) => {
    setBusy("读取会话");
    try {
      setSession(await getWorkflowCopilotSession(id));
      setPreview(null);
      setAnswers({});
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 会话读取失败"));
    } finally {
      setBusy("");
    }
  };

  useEffect(() => {
    if (!projectId) return;
    void refreshSessions().catch((failure) => setError(errorMessage(failure, "Copilot 会话读取失败")));
    // 会话切换由用户显式触发；项目变化时重新加载列表。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const createSession = async () => {
    setBusy("创建会话");
    try {
      const created = await createWorkflowCopilotSession({ title: "工作流 Copilot 会话", project_id: projectId });
      setSession(created);
      setSessions((current) => [created, ...(current ?? [])]);
      setPreview(null);
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 会话创建失败"));
    } finally {
      setBusy("");
    }
  };

  const applyResult = (result: WorkflowCopilotTurnResult) => {
    setSession((current) => ({ ...(current ?? result.session), ...result.session, turns: [...(current?.turns ?? []), result.turn] }));
    setAnswers({});
    setError("");
  };

  const send = async () => {
    if (!session || !message.trim()) return;
    setBusy("对话");
    try {
      applyResult(await sendWorkflowCopilotTurn(session.id, message.trim()));
      setMessage("");
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 回复失败"));
    } finally {
      setBusy("");
    }
  };

  const submitChoice = async (choice: ChoiceRequest) => {
    if (!session) return;
    setBusy("提交选择");
    try {
      const formatted = formatChoiceAnswer(choice, answers);
      applyResult(await answerWorkflowCopilotChoice(session.id, choice.id, answers, formatted));
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 选择提交失败"));
    } finally {
      setBusy("");
    }
  };

  const previewDraft = async () => {
    if (!session?.draft_valid) return;
    setBusy("预览");
    try {
      const result = await previewWorkflowCopilotDraft(session.id);
      setPreview(result.preview);
      notify("预览完成：没有创建任务、Run 或事件");
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 草稿预览失败"));
    } finally {
      setBusy("");
    }
  };

  const publish = async () => {
    if (!session?.draft_valid || !window.confirm("确认发布当前草稿为新的工作流版本？发布后版本不可变。")) return;
    setBusy("发布");
    try {
      const result = await publishWorkflowCopilotDraft(session.id, session.draft_revision, true);
      setSession(result.session);
      notify(result.idempotent ? "工作流已发布（重复确认，已幂等处理）" : "工作流已发布；启动运行需要第二次确认");
      await refreshSessions();
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 发布失败"));
    } finally {
      setBusy("");
    }
  };

  const run = async () => {
    if (!session?.published_workflow_id || !window.confirm("确认在当前项目启动 Run？这会创建真实任务骨架。")) return;
    setBusy("启动");
    try {
      const result = await runWorkflowCopilotPublished(session.id, {
        project_id: projectId,
        published_revision: session.published_revision,
        confirmed: true,
      });
      notify(`Run 已启动：${result.run_id.slice(0, 8)}；任务结果仍需执行与审核`);
    } catch (failure) {
      setError(errorMessage(failure, "Copilot 启动运行失败"));
    } finally {
      setBusy("");
    }
  };

  const currentChoice = useMemo(() => {
    const turns = session?.turns ?? [];
    const last = turns[turns.length - 1];
    return last?.status === "completed" ? latestChoice(last.content) : null;
  }, [session?.turns]);

  return (
    <Panel
      title="工作流 Copilot"
      subtitle="通过对话和结构化选择生成工作流；草稿、预览、发布和 Run 分开确认"
      testId="workflow-copilot"
      actions={<button className="button button-primary" type="button" onClick={() => void createSession()} disabled={Boolean(busy)}><Plus size={15} /> 新建会话</button>}
    >
      <div className="grid grid-main-side" style={{ alignItems: "start" }}>
        <div className="list" data-testid="workflow-copilot-sessions">
          {sessions === null ? <LoadingSkeleton rows={3} /> : sessions.length ? sessions.map((item) => (
            <button key={item.id} type="button" className={`list-item list-item-static ${session?.id === item.id ? "is-selected" : ""}`} onClick={() => void openSession(item.id)}>
              <span className="icon-tile icon-tile-blue"><Sparkles size={15} /></span>
              <span className="item-copy"><strong>{item.title || "未命名会话"}</strong><small>草稿修订 {item.draft_revision} · {item.updated_at.slice(0, 16).replace("T", " ")}</small></span>
              {item.draft_valid ? <StatusPill status="APPROVED" label="可预览" /> : <StatusPill status="DRAFT" label="对话中" />}
            </button>
          )) : <EmptyState>从新建会话开始，让 Copilot 先询问目标、角色、节点和验收条件。</EmptyState>}
        </div>

        <div className="grid" style={{ gap: 12 }}>
          {error ? <div className="pack-missing" data-testid="workflow-copilot-error"><strong>操作未完成</strong><span>{error}</span></div> : null}
          {!session ? <EmptyState>选择或新建一个 Copilot 会话。</EmptyState> : (
            <>
              <div className="list" data-testid="workflow-copilot-turns">
                {(session.turns ?? []).map((turn) => (
                  <div className="list-item list-item-static" key={turn.id}>
                    <div className="item-copy" style={{ width: "100%" }}>
                      <small>第 {turn.seq} 轮 · {turn.via || "平台模型"}{turn.model ? ` · ${turn.model}` : ""}</small>
                      <Markdown text={parseChoiceResponse(turn.content).markdown} />
                    </div>
                  </div>
                ))}
                {!session.turns?.length ? <EmptyState>告诉 Copilot 你想自动化的生产流程。</EmptyState> : null}
              </div>

              {currentChoice ? <ChoiceForm choice={currentChoice} answers={answers} setAnswers={setAnswers} onSubmit={() => void submitChoice(currentChoice)} disabled={Boolean(busy)} /> : null}

              <div className="form-row">
                <textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：为数学建模论文建立审题、建模、编程、复核和交付流程" rows={3} disabled={Boolean(busy)} data-testid="workflow-copilot-input" />
                <button className="button button-primary" type="button" onClick={() => void send()} disabled={Boolean(busy) || !message.trim()}><Send size={15} /> 发送</button>
              </div>

              <div className="form-row" style={{ justifyContent: "flex-end" }}>
                <button className="button button-secondary" type="button" onClick={() => void previewDraft()} disabled={Boolean(busy) || !session.draft_valid}><Eye size={15} /> 预览草稿</button>
                <button className="button button-secondary" type="button" onClick={() => void publish()} disabled={Boolean(busy) || !session.draft_valid}><Check size={15} /> 确认发布</button>
                <button className="button button-primary" type="button" onClick={() => void run()} disabled={Boolean(busy) || !session.published_workflow_id}><Play size={15} /> 确认启动 Run</button>
              </div>

              {preview ? <pre className="code-block" data-testid="workflow-copilot-preview">{JSON.stringify(preview, null, 2)}</pre> : null}
              {session.validation_errors?.length ? <div className="hint" data-testid="workflow-copilot-validation">校验提示：{session.validation_errors.join("；")}</div> : null}
            </>
          )}
        </div>
      </div>
    </Panel>
  );
}

function ChoiceForm({ choice, answers, setAnswers, onSubmit, disabled }: { choice: ChoiceRequest; answers: Record<string, string | string[]>; setAnswers: (value: Record<string, string | string[]>) => void; onSubmit: () => void; disabled: boolean }) {
  const complete = choice.questions.every((question) => !question.required || (Array.isArray(answers[question.id]) ? answers[question.id].length > 0 : Boolean(String(answers[question.id] ?? "").trim())));
  return (
    <div className="agent-choice" data-testid={`workflow-copilot-choice-${choice.id}`}>
      <div className="agent-choice-head"><Sparkles size={16} /><div><strong>{choice.title}</strong>{choice.description ? <p>{choice.description}</p> : null}</div></div>
      <div className="agent-choice-questions">
        {choice.questions.map((question) => (
          <fieldset className="agent-choice-question" key={question.id}><legend>{question.label}{question.required ? <em>必选</em> : null}</legend>
            {question.type === "text" ? <textarea rows={2} value={String(answers[question.id] ?? "")} onChange={(event) => setAnswers({ ...answers, [question.id]: event.target.value })} disabled={disabled} /> : <div className="agent-choice-options">{(question.options ?? []).map((option) => {
              const current = answers[question.id];
              const checked = Array.isArray(current) ? current.includes(option.value) : current === option.value;
              return <label key={option.value} className={checked ? "is-checked" : ""}><input type={question.type === "multiple" ? "checkbox" : "radio"} checked={checked} disabled={disabled} onChange={() => {
                if (question.type === "single") setAnswers({ ...answers, [question.id]: option.value });
                else { const selected = Array.isArray(current) ? current : []; setAnswers({ ...answers, [question.id]: selected.includes(option.value) ? selected.filter((item) => item !== option.value) : [...selected, option.value] }); }
              }} /><span>{option.label}</span></label>;
            })}</div>}
          </fieldset>
        ))}
      </div>
      <button className="button button-primary" type="button" disabled={!complete || disabled} onClick={onSubmit}><Send size={15} /> 提交选择</button>
    </div>
  );
}

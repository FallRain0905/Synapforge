"use client";

/**
 * 「转入项目生产」对话框（W1.2 B 侧）：把一次对话的产出建成正式任务。
 *
 * 契约（A 的 promote API）：产物只能从**本会话**轮次的 outputs 里挑（服务端校验，
 * 防跨会话夹带）；target_agent_id 只生成任务描述里的**结构化交接块**，不是硬性钉定
 * ——所以响应 handoff_note 非空时**原文展示**，并把"未硬性钉定"如实说清楚，
 * 绝不把"交给另一个 Agent"演成"已经派给了他"。
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, CheckCircle2 } from "lucide-react";
import { Modal } from "./ui";
import {
  promoteMyAgentConversation,
  errorMessage,
  MyAgentPromoteResult,
  MyAgentEndpoint,
  MyAgentTurn,
} from "../lib/api";

type PromoteOutputOption = { artifact_id: string; name: string; turn_seq: number };

export function PromoteDialog({
  conversationId,
  conversationTitle,
  agents,
  turns,
  onClose,
  onError,
}: {
  conversationId: string;
  conversationTitle: string;
  agents: MyAgentEndpoint[];
  turns: MyAgentTurn[];
  onClose: () => void;
  onError: (message: string) => void;
}) {
  const [title, setTitle] = useState(() => `${(conversationTitle || "对话产出").slice(0, 120)}（转自对话）`);
  const [description, setDescription] = useState("");
  const [targetAgentId, setTargetAgentId] = useState("");
  const [handoffContext, setHandoffContext] = useState("");
  const [selectedOutputs, setSelectedOutputs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<MyAgentPromoteResult | null>(null);

  const outputOptions = useMemo<PromoteOutputOption[]>(() => {
    const seen = new Set<string>();
    const options: PromoteOutputOption[] = [];
    for (const turn of turns) {
      for (const output of turn.outputs ?? []) {
        if (!output.artifact_id || seen.has(output.artifact_id)) continue;
        seen.add(output.artifact_id);
        options.push({ artifact_id: output.artifact_id, name: output.name, turn_seq: turn.seq });
      }
    }
    return options;
  }, [turns]);

  const submit = async () => {
    if (title.trim().length < 2) {
      onError("任务标题至少 2 个字");
      return;
    }
    setBusy(true);
    try {
      const promoted = await promoteMyAgentConversation(conversationId, {
        title: title.trim(),
        description: description.trim(),
        output_artifact_ids: selectedOutputs,
        target_agent_id: targetAgentId || null,
        handoff_context: handoffContext.trim() || null,
      });
      setResult(promoted);
    } catch (error) {
      onError(errorMessage(error, "转入项目生产失败"));
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <Modal title="已转入项目生产" subtitle="对话产出已成为正式任务，后续走任务板与审核" onClose={onClose} testId="my-agent-promote-done">
        <div className="empty-cta">
          <CheckCircle2 size={22} />
          <strong>{result.task.title}</strong>
          <small>
            已挂上 {result.attached_artifact_ids.length} 个产出附件。
            任务在「任务与流程」里推进；完成后成果物照常走审核门禁。
          </small>
          {result.handoff_note ? (
            <div className="hint" style={{ textAlign: "left", whiteSpace: "pre-wrap" }} data-testid="my-agent-promote-handoff-note">
              {result.handoff_note}
              <br />
              <small>
                说明：这是写入任务描述的交接块，<b>不是硬性钉定</b>——任务仍按能力匹配由 Agent 领取。
              </small>
            </div>
          ) : null}
          <div className="form-row" style={{ justifyContent: "center" }}>
            <Link className="button button-primary" href="/tasks" data-testid="my-agent-promote-goto-tasks">
              查看任务 <ArrowUpRight size={15} />
            </Link>
            <button type="button" className="button button-secondary" onClick={onClose}>
              继续对话
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title="转入项目生产"
      subtitle="对话产出 → 任务输入附件 + 新任务；产物只能从本会话的产出里挑"
      onClose={onClose}
      testId="my-agent-promote-dialog"
    >
      <div className="form-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 10 }}>
        <label style={{ display: "grid", gap: 4 }}>
          <span>任务标题（必填）</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} data-testid="my-agent-promote-title" maxLength={180} />
        </label>
        <label style={{ display: "grid", gap: 4 }}>
          <span>任务说明（要做什么、验收口径；留空则只带产出）</span>
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={3}
            maxLength={8000}
            data-testid="my-agent-promote-description"
          />
        </label>
        <div style={{ display: "grid", gap: 4 }}>
          <span>带上哪些产出作为任务输入（最多 10 个）</span>
          {outputOptions.length ? (
            <div style={{ display: "grid", gap: 4, maxHeight: 160, overflow: "auto" }}>
              {outputOptions.map((option) => (
                <label key={option.artifact_id} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input
                    type="checkbox"
                    checked={selectedOutputs.includes(option.artifact_id)}
                    onChange={(event) =>
                      setSelectedOutputs((current) => {
                        if (event.target.checked) {
                          return current.length >= 10 ? current : [...current, option.artifact_id];
                        }
                        return current.filter((item) => item !== option.artifact_id);
                      })
                    }
                    data-testid={`my-agent-promote-output-${option.artifact_id}`}
                  />
                  <span>
                    {option.name} <small>（第 {option.turn_seq} 轮产出）</small>
                  </span>
                </label>
              ))}
            </div>
          ) : (
            <small>这一轮对话还没有产出文件；可以直接建任务（比如把对话里的结论交给下一个 Agent 处理）。</small>
          )}
        </div>
        <label style={{ display: "grid", gap: 4 }}>
          <span>交给哪个 Agent（可选）</span>
          <select value={targetAgentId} onChange={(event) => setTargetAgentId(event.target.value)} data-testid="my-agent-promote-target">
            <option value="">不指定：按能力匹配，谁的执行体合适谁领取</option>
            {agents.map((agent) => (
              <option key={agent.agent_id ?? agent.device_id} value={agent.agent_id ?? ""}>
                {agent.device_name || agent.device_id} · {agent.executor || "执行体"}
              </option>
            ))}
          </select>
        </label>
        {targetAgentId ? (
          <label style={{ display: "grid", gap: 4 }}>
            <span>交接说明（写进任务描述的交接块：上下文、注意点、建议下一步）</span>
            <textarea
              value={handoffContext}
              onChange={(event) => setHandoffContext(event.target.value)}
              rows={3}
              maxLength={4000}
              data-testid="my-agent-promote-handoff"
            />
            <small>如实说明：这是任务描述里的交接块，不是硬性派发——目标 Agent 并不会被强制指派。</small>
          </label>
        ) : null}
        <div className="form-row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="button button-primary" onClick={() => void submit()} disabled={busy} data-testid="my-agent-promote-submit">
            {busy ? "创建中…" : "创建任务"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

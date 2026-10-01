"use client";

/**
 * 工作流定义编辑器（W4.2 前置骨架）。
 *
 * 字段集以 docs/WORKFLOW_SCHEMA.md v1 §3 为准（不从 API 响应反推）；校验规则**不重复实现**
 * ——服务端按 schema §4 八条校验并逐条返回 "id: 原因"，这里只做展示（422 → errors 数组）。
 * 两处契约红线的 UI 落实：
 * - role_binding 互斥（§4 规则 6）：auto/hybrid 显示必填下拉、manual 隐藏并说明原因；
 * - budget 只收 {max_seconds, max_attempts, max_tokens}；retry_policy 只有 backoff_seconds
 *   （max_attempts 写进 retry 服务端会报错，表单根本不提供这个字段）。
 * 交付适配器下拉取 delivery_adapters[].id（两层引用：节点→id→kind→注册表），不直接列 kind。
 */

import { useMemo, useState } from "react";
import { Modal } from "./ui";
import {
  createWorkflow,
  createWorkflowVersion,
  WorkflowValidationError,
  WorkflowDefinition,
  WorkflowNodeSpec,
  WorkflowPackage,
} from "../lib/api";

const MODES: WorkflowNodeSpec["mode"][] = ["auto", "hybrid", "manual"];
const ON_FAILS = ["escalate_human", "retry"];
const HUMAN_INTERVENTIONS: NonNullable<WorkflowNodeSpec["human_intervention"]>[] = ["none", "before", "after", "approval_gate"];
const ARTIFACT_TYPES = ["document", "figure", "code", "result_table", "review_report", "paper_source", "compiled_pdf", "submission_bundle"];

function blankDefinition(): WorkflowDefinition {
  return {
    schema_version: 1,
    workflow: { key: "", name: "", description: "", inputs: [] },
    stages: [],
    role_bindings: [],
    nodes: [],
    gate_policies: [],
    delivery_adapters: [],
    handoff_contracts: [],
  };
}

function Row({ label, children, required }: { label: string; children: React.ReactNode; required?: boolean }) {
  return (
    <label style={{ display: "grid", gap: 3, minWidth: 0 }}>
      <span style={{ fontSize: 12 }}>{label}{required ? " *" : ""}</span>
      {children}
    </label>
  );
}

export function WorkflowEditor({
  editing,
  initialDefinition,
  onClose,
  onSaved,
  notify,
}: {
  /** 发布新版本时传已有包（key 锁定）；新建传 null */
  editing: WorkflowPackage | null;
  initialDefinition: WorkflowDefinition | null;
  onClose: () => void;
  onSaved: (pkg: WorkflowPackage) => void;
  notify: (message: string) => void;
}) {
  const base = useMemo(() => initialDefinition ?? blankDefinition(), [initialDefinition]);
  const [key, setKey] = useState(base.workflow.key);
  const [name, setName] = useState(base.workflow.name);
  const [description, setDescription] = useState(base.workflow.description ?? "");
  const [inputs, setInputs] = useState(base.workflow.inputs ?? []);
  const [stages, setStages] = useState(base.stages);
  const [roles, setRoles] = useState(base.role_bindings);
  const [gatePolicies, setGatePolicies] = useState(base.gate_policies ?? []);
  const [deliveryAdapters, setDeliveryAdapters] = useState(base.delivery_adapters ?? []);
  const [handoffContracts, setHandoffContracts] = useState(base.handoff_contracts ?? []);
  const [nodes, setNodes] = useState(base.nodes);
  const [submitting, setSubmitting] = useState(false);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);

  const allOutputSources = useMemo(() => {
    const sources: { kind: "from_output"; node_id: string; name: string }[] = [];
    for (const node of nodes) for (const output of node.outputs ?? []) sources.push({ kind: "from_output", node_id: node.id, name: output.name });
    return sources;
  }, [nodes]);

  const patchNode = (index: number, patch: Partial<WorkflowNodeSpec>) => {
    setNodes((current) => current.map((node, i) => (i === index ? { ...node, ...patch } : node)));
  };

  const submit = async () => {
    setValidationErrors([]);
    if (!key.trim() || !name.trim()) {
      setValidationErrors(["workflow.key / workflow.name 必填（前端预检，完整规则由服务端校验）"]);
      return;
    }
    const definition: WorkflowDefinition = {
      schema_version: 1,
      workflow: { key: key.trim(), name: name.trim(), description: description.trim(), inputs },
      stages,
      role_bindings: roles,
      nodes,
      gate_policies: gatePolicies,
      delivery_adapters: deliveryAdapters,
      handoff_contracts: handoffContracts,
    };
    setSubmitting(true);
    try {
      const saved = editing
        ? await createWorkflowVersion(editing.id, definition)
        : await createWorkflow(definition);
      notify(editing ? `「${saved.name}」新版本已发布（旧版本只读，运行绑定不受影响）` : `工作流包「${saved.name}」已创建`);
      onSaved(saved);
    } catch (error) {
      if (error instanceof WorkflowValidationError) {
        setValidationErrors(error.errors); // 服务端逐条 "id: 原因"，原样展示
      } else {
        notify(error instanceof Error ? error.message : "保存失败");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      wide
      title={editing ? `发布新版本 · ${editing.name}` : "新建工作流包"}
      subtitle="定义只描述「要做什么、谁做、门槛是什么」；执行落到平台既有 Task/Run/Artifact。校验由服务端逐条反馈"
      onClose={onClose}
      testId="workflow-editor"
    >
      {validationErrors.length ? (
        <div className="pack-missing" data-testid="workflow-editor-errors">
          <strong>定义校验未通过（{validationErrors.length} 条）</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {validationErrors.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div style={{ display: "grid", gap: 14 }}>
        <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
          <Row label="key（全局唯一、稳定不变）" required>
            <input value={key} disabled={Boolean(editing)} onChange={(event) => setKey(event.target.value)} data-testid="workflow-editor-key" />
          </Row>
          <Row label="名称" required>
            <input value={name} onChange={(event) => setName(event.target.value)} data-testid="workflow-editor-name" />
          </Row>
          <Row label="说明">
            <input value={description} onChange={(event) => setDescription(event.target.value)} />
          </Row>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>输入声明</strong>
          {inputs.map((input, index) => (
            <div key={index} style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                placeholder="name（模板里 {{input.name}}）"
                value={input.name}
                onChange={(event) => setInputs((c) => c.map((item, i) => (i === index ? { ...item, name: event.target.value } : item)))}
                style={{ flex: 1 }}
              />
              <input
                placeholder="提示（给人看）"
                value={input.hint ?? ""}
                onChange={(event) => setInputs((c) => c.map((item, i) => (i === index ? { ...item, hint: event.target.value } : item)))}
                style={{ flex: 2 }}
              />
              <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12 }}>
                <input
                  type="checkbox"
                  checked={Boolean(input.required)}
                  onChange={(event) => setInputs((c) => c.map((item, i) => (i === index ? { ...item, required: event.target.checked } : item)))}
                />
                必填
              </label>
              <button type="button" className="text-button" onClick={() => setInputs((c) => c.filter((_, i) => i !== index))}>移除</button>
            </div>
          ))}
          <button type="button" className="text-button" onClick={() => setInputs((c) => [...c, { name: "", required: false }])}>+ 添加输入</button>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>阶段（只做展示归组，不表达执行顺序——顺序只看节点的 depends_on）</strong>
          {stages.map((stage, index) => (
            <div key={index} style={{ display: "flex", gap: 8 }}>
              <input placeholder="id" value={stage.id} onChange={(event) => setStages((c) => c.map((item, i) => (i === index ? { ...item, id: event.target.value } : item)))} style={{ width: 120 }} />
              <input placeholder="标题" value={stage.title} onChange={(event) => setStages((c) => c.map((item, i) => (i === index ? { ...item, title: event.target.value } : item)))} style={{ width: 140 }} />
              <input placeholder="目标" value={stage.goal ?? ""} onChange={(event) => setStages((c) => c.map((item, i) => (i === index ? { ...item, goal: event.target.value } : item)))} style={{ flex: 1 }} />
              <button type="button" className="text-button" onClick={() => setStages((c) => c.filter((_, i) => i !== index))}>移除</button>
            </div>
          ))}
          <button type="button" className="text-button" onClick={() => setStages((c) => [...c, { id: "", title: "" }])}>+ 添加阶段</button>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>角色绑定</strong>
          {roles.map((role, index) => (
            <div key={index} style={{ display: "flex", gap: 8 }}>
              <input placeholder="id" value={role.id} onChange={(event) => setRoles((c) => c.map((item, i) => (i === index ? { ...item, id: event.target.value } : item)))} style={{ width: 120 }} />
              <input placeholder="role_name（执行体侧角色名）" value={role.role_name} onChange={(event) => setRoles((c) => c.map((item, i) => (i === index ? { ...item, role_name: event.target.value } : item)))} style={{ width: 180 }} />
              <input placeholder="说明" value={role.description ?? ""} onChange={(event) => setRoles((c) => c.map((item, i) => (i === index ? { ...item, description: event.target.value } : item)))} style={{ flex: 1 }} />
              <input
                placeholder="能力要求（逗号分隔，如 files.write）"
                value={(role.capability_requirements ?? []).join(",")}
                onChange={(event) => setRoles((c) => c.map((item, i) => (i === index ? { ...item, capability_requirements: event.target.value.split(",").map((s) => s.trim()).filter(Boolean) } : item)))}
                style={{ width: 220 }}
              />
              <button type="button" className="text-button" onClick={() => setRoles((c) => c.filter((_, i) => i !== index))}>移除</button>
            </div>
          ))}
          <button type="button" className="text-button" onClick={() => setRoles((c) => [...c, { id: "", role_name: "" }])}>+ 添加角色</button>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>门禁策略（acceptance.py 五类字面语法，逐行一条；{"{any}/{all}"} 组合暂走"从已有定义编辑"）</strong>
          {gatePolicies.map((policy, index) => (
            <div key={index} style={{ display: "grid", gap: 4, border: "1px solid var(--line, #ddd)", padding: 8, borderRadius: 6 }}>
              <div style={{ display: "flex", gap: 8 }}>
                <input placeholder="id" value={policy.id} onChange={(event) => setGatePolicies((c) => c.map((item, i) => (i === index ? { ...item, id: event.target.value } : item)))} style={{ width: 160 }} />
                <select
                  value={policy.on_block}
                  onChange={(event) => setGatePolicies((c) => c.map((item, i) => (i === index ? { ...item, on_block: event.target.value as typeof policy.on_block } : item)))}
                  style={{ width: 180 }}
                >
                  <option value="blocked">blocked：停下来等输入</option>
                  <option value="escalate_human">escalate_human：升级人工</option>
                </select>
                <button type="button" className="text-button" onClick={() => setGatePolicies((c) => c.filter((_, i) => i !== index))}>移除</button>
              </div>
              <textarea
                placeholder={"每行一条，例如：\nfile:outputs/outline.md non-empty\nartifact:outline approved"}
                value={policy.spec.filter((leaf): leaf is string => typeof leaf === "string").join("\n")}
                onChange={(event) =>
                  setGatePolicies((c) =>
                    c.map((item, i) =>
                      i === index
                        ? { ...item, spec: event.target.value.split("\n").map((line) => line.trim()).filter(Boolean) }
                        : item,
                    ),
                  )
                }
                rows={3}
              />
            </div>
          ))}
          <button type="button" className="text-button" onClick={() => setGatePolicies((c) => [...c, { id: "", spec: [], on_block: "blocked" }])}>+ 添加门禁</button>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>交付适配器（节点下拉取这里的 id；kind 才对注册表）</strong>
          {deliveryAdapters.map((adapter, index) => (
            <div key={index} style={{ display: "flex", gap: 8 }}>
              <input placeholder="id" value={adapter.id} onChange={(event) => setDeliveryAdapters((c) => c.map((item, i) => (i === index ? { ...item, id: event.target.value } : item)))} style={{ width: 160 }} />
              <input placeholder="kind（注册表名，如 paper_compile）" value={adapter.kind} onChange={(event) => setDeliveryAdapters((c) => c.map((item, i) => (i === index ? { ...item, kind: event.target.value } : item)))} style={{ flex: 1 }} />
              <button type="button" className="text-button" onClick={() => setDeliveryAdapters((c) => c.filter((_, i) => i !== index))}>移除</button>
            </div>
          ))}
          <button type="button" className="text-button" onClick={() => setDeliveryAdapters((c) => [...c, { id: "", kind: "", config: {} }])}>+ 添加交付适配器</button>
        </section>

        <section style={{ display: "grid", gap: 6 }}>
          <strong>节点（执行单元；依赖必须无环、全部可达——服务端校验）</strong>
          {nodes.map((node, index) => (
            <div key={index} style={{ display: "grid", gap: 6, border: "1px solid var(--line, #ddd)", padding: 10, borderRadius: 6 }} data-testid={`workflow-editor-node-${index}`}>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Row label="id" required>
                  <input value={node.id} onChange={(event) => patchNode(index, { id: event.target.value })} style={{ width: 140 }} />
                </Row>
                <Row label="标题" required>
                  <input value={node.title} onChange={(event) => patchNode(index, { title: event.target.value })} style={{ width: 160 }} />
                </Row>
                <Row label="阶段">
                  <select value={node.stage_id} onChange={(event) => patchNode(index, { stage_id: event.target.value })} style={{ width: 140 }}>
                    <option value="">未分组</option>
                    {stages.map((stage) => (
                      <option key={stage.id} value={stage.id}>{stage.title || stage.id}</option>
                    ))}
                  </select>
                </Row>
                <Row label="执行模式" required>
                  <select value={node.mode} onChange={(event) => patchNode(index, { mode: event.target.value as WorkflowNodeSpec["mode"], role_binding: event.target.value === "manual" ? undefined : node.role_binding })} style={{ width: 110 }}>
                    {MODES.map((mode) => (
                      <option key={mode} value={mode}>{mode}</option>
                    ))}
                  </select>
                </Row>
                {node.mode === "manual" ? (
                  <span style={{ alignSelf: "end", fontSize: 12 }}>人工节点不绑角色（互斥校验：路由到成员/公共队列）</span>
                ) : (
                  <Row label="角色绑定 *" required>
                    <select value={node.role_binding ?? ""} onChange={(event) => patchNode(index, { role_binding: event.target.value || undefined })} style={{ width: 150 }}>
                      <option value="">（必选）</option>
                      {roles.map((role) => (
                        <option key={role.id} value={role.id}>{role.role_name || role.id}</option>
                      ))}
                    </select>
                  </Row>
                )}
                <span style={{ alignSelf: "end" }}>
                  <button type="button" className="text-button" onClick={() => setNodes((c) => c.filter((_, i) => i !== index))}>移除节点</button>
                </span>
              </div>
              <Row label="目标">
                <input value={node.goal ?? ""} onChange={(event) => patchNode(index, { goal: event.target.value })} />
              </Row>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 12 }}>
                <span>依赖（depends_on）：</span>
                {nodes.filter((_, i) => i !== index).map((other) => (
                  <label key={other.id || other.title} style={{ display: "flex", gap: 3, alignItems: "center" }}>
                    <input
                      type="checkbox"
                      checked={node.depends_on.includes(other.id)}
                      onChange={(event) =>
                        patchNode(index, {
                          depends_on: event.target.checked
                            ? [...node.depends_on, other.id]
                            : node.depends_on.filter((id) => id !== other.id),
                        })
                      }
                    />
                    {other.title || other.id || "（未命名）"}
                  </label>
                ))}
                {!nodes.filter((_, i) => i !== index).length ? <span>（无其他节点）</span> : null}
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 12 }}>
                <span>输入来源：</span>
                {allOutputSources.filter((source) => source.node_id !== node.id).map((source) => {
                  const marked = (node.inputs ?? []).some((item) => "from_output" in item && item.from_output === source.name);
                  return (
                    <label key={`${source.node_id}:${source.name}`} style={{ display: "flex", gap: 3, alignItems: "center" }}>
                      <input
                        type="checkbox"
                        checked={marked}
                        onChange={(event) => {
                          const current = node.inputs ?? [];
                          patchNode(index, {
                            inputs: event.target.checked
                              ? [...current, { from_output: source.name }]
                              : current.filter((item) => !("from_output" in item && item.from_output === source.name)),
                          });
                        }}
                      />
                      {source.name}
                    </label>
                  );
                })}
                {inputs.filter((input) => input.name).map((input) => {
                  const marked = (node.inputs ?? []).some((item) => "input" in item && item.input === input.name);
                  return (
                    <label key={`workflow-input-${input.name}`} style={{ display: "flex", gap: 3, alignItems: "center" }}>
                      <input
                        type="checkbox"
                        checked={marked}
                        onChange={(event) => {
                          const current = node.inputs ?? [];
                          patchNode(index, {
                            inputs: event.target.checked
                              ? [...current, { input: input.name }]
                              : current.filter((item) => !("input" in item && item.input === input.name)),
                          });
                        }}
                      />
                      {`{{input.${input.name}}}`}
                    </label>
                  );
                })}
              </div>
              <div style={{ fontSize: 12 }}>
                产出（name · 类型 · 路径相对工作区根，禁绝对路径与 ..）：
                {(node.outputs ?? []).map((output, outputIndex) => (
                  <div key={outputIndex} style={{ display: "flex", gap: 6, marginTop: 4 }}>
                    <input placeholder="name" value={output.name} onChange={(event) => patchNode(index, { outputs: (node.outputs ?? []).map((item, i) => (i === outputIndex ? { ...item, name: event.target.value } : item)) })} style={{ width: 130 }} />
                    <select value={output.artifact_type} onChange={(event) => patchNode(index, { outputs: (node.outputs ?? []).map((item, i) => (i === outputIndex ? { ...item, artifact_type: event.target.value } : item)) })} style={{ width: 150 }}>
                      {ARTIFACT_TYPES.map((artifactType) => (
                        <option key={artifactType} value={artifactType}>{artifactType}</option>
                      ))}
                    </select>
                    <input placeholder="outputs/xxx.md（可选）" value={output.path ?? ""} onChange={(event) => patchNode(index, { outputs: (node.outputs ?? []).map((item, i) => (i === outputIndex ? { ...item, path: event.target.value || undefined } : item)) })} style={{ flex: 1 }} />
                    <button type="button" className="text-button" onClick={() => patchNode(index, { outputs: (node.outputs ?? []).filter((_, i) => i !== outputIndex) })}>移除</button>
                  </div>
                ))}
                <button type="button" className="text-button" onClick={() => patchNode(index, { outputs: [...(node.outputs ?? []), { name: "", artifact_type: "document" }] })}>+ 添加产出</button>
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                <Row label="门禁">
                  <select value={node.gate_policy ?? ""} onChange={(event) => patchNode(index, { gate_policy: event.target.value || undefined })} style={{ width: 150 }}>
                    <option value="">无</option>
                    {gatePolicies.map((policy) => (
                      <option key={policy.id} value={policy.id}>{policy.id}</option>
                    ))}
                  </select>
                </Row>
                <Row label="交付适配器">
                  <select value={node.delivery_adapter ?? ""} onChange={(event) => patchNode(index, { delivery_adapter: event.target.value || undefined })} style={{ width: 150 }}>
                    <option value="">无</option>
                    {deliveryAdapters.map((adapter) => (
                      <option key={adapter.id} value={adapter.id}>{adapter.id}（{adapter.kind}）</option>
                    ))}
                  </select>
                </Row>
                <Row label="失败处置 on_fail">
                  <select value={node.on_fail ?? "escalate_human"} onChange={(event) => patchNode(index, { on_fail: event.target.value || undefined })} style={{ width: 150 }}>
                    {ON_FAILS.map((onFail) => (
                      <option key={onFail} value={onFail}>{onFail}</option>
                    ))}
                    {roles.map((role) => (
                      <option key={`handoff:${role.id}`} value={`handoff:${role.id}`}>{`handoff:${role.role_name || role.id}`}</option>
                    ))}
                  </select>
                </Row>
                <Row label="人工介入">
                  <select value={node.human_intervention ?? "none"} onChange={(event) => patchNode(index, { human_intervention: event.target.value as WorkflowNodeSpec["human_intervention"] })} style={{ width: 140 }}>
                    {HUMAN_INTERVENTIONS.map((option) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </Row>
                <Row label="预算 max_seconds">
                  <input type="number" min={0} value={node.budget?.max_seconds ?? ""} onChange={(event) => patchNode(index, { budget: { ...node.budget, max_seconds: event.target.value ? Number(event.target.value) : undefined } })} style={{ width: 110 }} />
                </Row>
                <Row label="预算 max_attempts">
                  <input type="number" min={0} value={node.budget?.max_attempts ?? ""} onChange={(event) => patchNode(index, { budget: { ...node.budget, max_attempts: event.target.value ? Number(event.target.value) : undefined } })} style={{ width: 110 }} />
                </Row>
                <Row label="预算 max_tokens">
                  <input type="number" min={0} value={node.budget?.max_tokens ?? ""} onChange={(event) => patchNode(index, { budget: { ...node.budget, max_tokens: event.target.value ? Number(event.target.value) : undefined } })} style={{ width: 110 }} />
                </Row>
                <Row label="重试退避（秒）">
                  <input type="number" min={0} value={node.retry_policy?.backoff_seconds ?? ""} onChange={(event) => patchNode(index, { retry_policy: event.target.value ? { backoff_seconds: Number(event.target.value) } : undefined })} style={{ width: 110 }} />
                </Row>
                <Row label="交接契约">
                  <select value={node.handoff_contract ?? ""} onChange={(event) => patchNode(index, { handoff_contract: event.target.value || undefined })} style={{ width: 160 }}>
                    <option value="">无</option>
                    {handoffContracts.map((contract) => (
                      <option key={contract.id} value={contract.id}>{contract.id}</option>
                    ))}
                  </select>
                </Row>
              </div>
            </div>
          ))}
          <button
            type="button"
            className="text-button"
            data-testid="workflow-editor-add-node"
            onClick={() =>
              setNodes((c) => [
                ...c,
                { id: "", stage_id: "", title: "", goal: "", depends_on: [], mode: "auto", role_binding: roles[0]?.id, outputs: [] },
              ])
            }
          >
            + 添加节点
          </button>
        </section>

        <div className="form-row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="button button-secondary" onClick={onClose} disabled={submitting}>取消</button>
          <button type="button" className="button button-primary" onClick={() => void submit()} disabled={submitting} data-testid="workflow-editor-submit">
            {submitting ? "提交中…" : editing ? "发布新版本" : "创建工作流包"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

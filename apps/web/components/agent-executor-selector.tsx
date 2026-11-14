"use client";

/**
 * 执行体/模型选择器（共享组件，2026-10-02）——「我的智能体」与「任务」页同一套。
 *
 * 两组模型，**明确分组、绝不静默混合**：
 * - 「平台渠道」：成员组织里启用渠道的模型（额度记到 agent 的 owner 名下）。能不能用
 *   取决于执行体侧的 provider 配置与授权（`llm.invoke`）——`available=false` 时如实给出
 *   "配置不可用"的说明，而不是把模型塞进选项里让人点了跑不通；
 * - 「执行体本地」：执行体心跳真探测到的本地模型（`opencode models`）。探不到就是没有，
 *   不显示假选项。
 *
 * 模型值的约定（与后端 chat_loop 的口径一致）：
 * - 平台渠道模型选择后上送的是**裸渠道模型名**（不带 provider 前缀）——执行体在跑的时候
 *   自己加 `synapforge/` 前缀并经项目隔离配置走平台代理；
 * - 本地模型上送完整 `provider/model` 串（执行体按它原样 `-m`）。
 */

import { Bot, Cloud, Server } from "lucide-react";

export type ExecutorSelectorEndpoint = {
  device_id: string;
  device_name: string;
  agent_id: string | null;
  executor: string;
  online: boolean;
  models: string[];
  default_model: string;
  platform_models: { model: string }[];
  platform_models_available: boolean;
  platform_provider: string;
};

export const PLATFORM_PROVIDER_ALIAS = "synapforge";

export type ExecutorModelOption = {
  /** 提交/保存用的值：平台渠道模型 = 裸渠道名；本地模型 = `provider/model` 完整串。 */
  value: string;
  label: string;
  group: "platform" | "local";
};

/** 组装选项：平台渠道在前、本地在后；配置不可用就不给平台组的选项（说明放 hint 里）。 */
export function executorModelOptions(endpoint: ExecutorSelectorEndpoint | null): ExecutorModelOption[] {
  if (!endpoint) return [];
  const options: ExecutorModelOption[] = [];
  if (endpoint.platform_models_available) {
    for (const item of endpoint.platform_models ?? []) {
      if (item.model) {
        options.push({ value: item.model, label: item.model, group: "platform" });
      }
    }
  }
  for (const model of endpoint.models ?? []) {
    if (model) options.push({ value: model, label: model, group: "local" });
  }
  return options;
}

/**
 * 选择器当前的"配置不可用"说明：null = 可用（或没有平台渠道可谈）。
 * 有渠道模型但执行体侧不可配置时返回人话说明（页面直接展示，不吞掉原因）。
 */
export function platformUnavailableHint(endpoint: ExecutorSelectorEndpoint | null): string {
  if (!endpoint || endpoint.platform_models_available) return "";
  if (!(endpoint.platform_models ?? []).length) return "";
  return "平台渠道有可用模型，但这台执行体还没接上（需要含 llm.invoke 的项目授权，内核会自动取配置；老内核请升级到当前版本）";
}

type Props = {
  endpoint: ExecutorSelectorEndpoint | null;
  value: string;
  onChange: (value: string) => void;
  /** 置灰条件（例如会话执行中）；默认可选。 */
  disabled?: boolean;
  testId?: string;
  /** 选项为空时占位文案（如实说明，不放假选项）。 */
  emptyLabel?: string;
};

export function AgentExecutorSelector({ endpoint, value, onChange, disabled, testId, emptyLabel }: Props) {
  const options = executorModelOptions(endpoint);
  const unavailable = platformUnavailableHint(endpoint);
  const platformCount = options.filter((item) => item.group === "platform").length;
  return (
    <label className="composer-picker" title={unavailable || "平台渠道模型消耗平台免费额度；执行体本地模型由执行体自己配置"}>
      {endpoint?.platform_models_available ? <Cloud size={14} /> : <Bot size={14} />}
      <select value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled} data-testid={testId}>
        <option value="">{emptyLabel ?? "（执行体默认）"}</option>
        {platformCount > 0 ? (
          <optgroup label={`平台渠道（${PLATFORM_PROVIDER_ALIAS}，消耗平台额度）`}>
            {options
              .filter((item) => item.group === "platform")
              .map((item) => (
                <option key={`platform-${item.value}`} value={item.value}>
                  {item.label}
                </option>
              ))}
          </optgroup>
        ) : null}
        {options.some((item) => item.group === "local") ? (
          <optgroup label="执行体本地">
            {options
              .filter((item) => item.group === "local")
              .map((item) => (
                <option key={`local-${item.value}`} value={item.value}>
                  {item.label}
                </option>
              ))}
          </optgroup>
        ) : null}
        {!options.length ? <option value="">{emptyLabel ?? "（没有可选模型）"}</option> : null}
      </select>
      {unavailable ? <Server size={11} aria-hidden /> : null}
    </label>
  );
}

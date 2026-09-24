export type ChoiceOption = { value: string; label: string; description?: string };
export type ChoiceQuestion = {
  id: string;
  label: string;
  type: "single" | "multiple" | "text";
  required?: boolean;
  placeholder?: string;
  options?: ChoiceOption[];
};
export type ChoiceRequest = { id: string; title: string; description?: string; questions: ChoiceQuestion[] };

const CHOICE_BLOCK = /```synapforge-choice\s*\n([\s\S]*?)\n```\s*$/i;

export function validChoice(value: unknown): value is ChoiceRequest {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ChoiceRequest>;
  if (
    typeof candidate.id !== "string" ||
    !candidate.id.trim() ||
    typeof candidate.title !== "string" ||
    !candidate.title.trim() ||
    !Array.isArray(candidate.questions) ||
    candidate.questions.length < 1 ||
    candidate.questions.length > 20
  ) {
    return false;
  }
  const questionIds = new Set<string>();
  return candidate.questions.every((question) => {
    if (!question || typeof question !== "object") return false;
    const item = question as Partial<ChoiceQuestion>;
    if (
      typeof item.id !== "string" ||
      !item.id.trim() ||
      questionIds.has(item.id) ||
      typeof item.label !== "string" ||
      !item.label.trim() ||
      !["single", "multiple", "text"].includes(item.type ?? "")
    ) {
      return false;
    }
    questionIds.add(item.id);
    if (item.type === "text") return !item.options || item.options.length === 0;
    if (!Array.isArray(item.options) || item.options.length < 1 || item.options.length > 12) return false;
    const values = new Set<string>();
    return item.options.every((option) => {
      if (!option || typeof option.value !== "string" || !option.value.trim() || typeof option.label !== "string" || !option.label.trim()) return false;
      if (values.has(option.value)) return false;
      values.add(option.value);
      return true;
    });
  });
}

export function parseChoiceResponse(raw: string): { markdown: string; choice: ChoiceRequest | null } {
  const match = raw.match(CHOICE_BLOCK);
  if (!match || match.index === undefined) return { markdown: raw, choice: null };
  try {
    const parsed: unknown = JSON.parse(match[1]);
    if (!validChoice(parsed)) return { markdown: raw, choice: null };
    return { markdown: raw.slice(0, match.index).trimEnd(), choice: parsed };
  } catch {
    // Protocol JSON invalid: show the original fenced block as regular Markdown; never fabricate controls.
    return { markdown: raw, choice: null };
  }
}

export function formatChoiceAnswer(choice: ChoiceRequest, answers: Record<string, string | string[]>): string {
  const lines = choice.questions.map((question) => {
    const raw = answers[question.id];
    if (Array.isArray(raw)) {
      const labels = raw.map((value) => question.options?.find((option) => option.value === value)?.label || value);
      return `${question.label}：${labels.length ? labels.join("、") : "未选择"}`;
    }
    const label = question.options?.find((option) => option.value === raw)?.label || String(raw || "未填写");
    return `${question.label}：${label}`;
  });
  return `用户已完成「${choice.title}」的选择：\n${lines.map((line, index) => `${index + 1}. ${line}`).join("\n")}\n\n请按以上选择继续执行，不要重复询问已经回答的问题。`;
}
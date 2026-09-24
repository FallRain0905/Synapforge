"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, CircleHelp, Send } from "lucide-react";
import { formatChoiceAnswer, parseChoiceResponse, type ChoiceRequest } from "../lib/agent-choice";
import { Markdown } from "./markdown";

/**
 * 显示回复里的 Markdown；若末尾带了有效的 `synapforge-choice` JSON fence，解析成真实表单。
 * 普通编号列表不会被猜成表单，原始 HTML 也不会由 Markdown renderer 执行。
 */
export function AgentResponse({
  text,
  disabled = false,
  onSubmitChoice,
}: {
  text: string;
  disabled?: boolean;
  onSubmitChoice?: (message: string) => Promise<boolean | void> | boolean | void;
}) {
  const { markdown, choice } = useMemo(() => parseChoiceResponse(text), [text]);
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  // 新 choice block = 新一组选项；服务端刷新同一条回复时不清空用户正在选的内容。
  useEffect(() => {
    setAnswers({});
    setSubmitting(false);
    setSubmitted(false);
  }, [choice?.id]);

  const complete = Boolean(
    choice?.questions.every((question) => {
      if (!question.required) return true;
      const value = answers[question.id];
      return Array.isArray(value) ? value.length > 0 : Boolean(String(value || "").trim());
    }),
  );

  const submit = async () => {
    if (!choice || !onSubmitChoice || !complete || submitting || submitted) return;
    setSubmitting(true);
    try {
      const sent = await onSubmitChoice(formatChoiceAnswer(choice, answers));
      if (sent !== false) setSubmitted(true);
    } catch {
      // 发送失败时保留选项，让用户可以重试；不把失败显示成已提交。
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Markdown text={markdown} />
      {choice ? (
        <section className="agent-choice" aria-label={choice.title} data-testid={`agent-choice-${choice.id}`}>
          <header className="agent-choice-head">
            <CircleHelp size={16} />
            <div>
              <strong>{choice.title}</strong>
              {choice.description ? <p>{choice.description}</p> : null}
            </div>
          </header>
          <div className="agent-choice-questions">
            {choice.questions.map((question, index) => (
              <fieldset key={question.id} className="agent-choice-question">
                <legend>
                  <span>{index + 1}</span>
                  {question.label}
                  {question.required ? <em>必选</em> : null}
                </legend>
                {question.type === "text" ? (
                  <textarea
                    rows={2}
                    value={String(answers[question.id] || "")}
                    placeholder={question.placeholder || "补充说明（可选）"}
                    disabled={disabled || submitted}
                    onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
                  />
                ) : (
                  <div className="agent-choice-options">
                    {(question.options || []).map((option) => {
                      const current = answers[question.id];
                      const checked = Array.isArray(current) ? current.includes(option.value) : current === option.value;
                      return (
                        <label key={option.value} className={checked ? "is-checked" : ""}>
                          <input
                            type={question.type === "multiple" ? "checkbox" : "radio"}
                            name={`choice-${choice.id}-${question.id}`}
                            value={option.value}
                            checked={checked}
                            disabled={disabled || submitted}
                            onChange={() =>
                              setAnswers((state) => {
                                if (question.type === "single") return { ...state, [question.id]: option.value };
                                const selected = Array.isArray(state[question.id]) ? (state[question.id] as string[]) : [];
                                const next = selected.includes(option.value)
                                  ? selected.filter((item) => item !== option.value)
                                  : [...selected, option.value];
                                return { ...state, [question.id]: next };
                              })
                            }
                          />
                          <span>
                            <strong>{option.label}</strong>
                            {option.description ? <small>{option.description}</small> : null}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </fieldset>
            ))}
          </div>
          <button
            className="button button-primary"
            disabled={!complete || disabled || submitting || submitted}
            onClick={() => void submit()}
          >
            {submitted ? <Check size={15} /> : <Send size={15} />}
            {submitted ? "已提交选择" : submitting ? "提交中…" : "提交选择"}
          </button>
        </section>
      ) : null}
    </>
  );
}
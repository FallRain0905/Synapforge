// 结构化选择卡协议验证：解析、回发文案与安全边界。
// 被测对象是**真正上线的那份代码**（lib/agent-choice.ts）——先用仓库里的 tsc 编成 JS 再导入，
// 不复制一份实现来测（复制品测过了，线上那份照样可能坏）。
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(webRoot, "lib", "agent-choice.ts");
const tsc = join(webRoot, "node_modules", "typescript", "bin", "tsc");
if (!existsSync(tsc)) throw new Error(`找不到本地 tsc：${tsc}`);

const outDir = mkdtempSync(join(tmpdir(), "agent-choice-"));
let choice;
try {
  execFileSync(process.execPath, [tsc, source, "--outDir", outDir, "--target", "es2022", "--module", "es2022", "--moduleResolution", "bundler", "--skipLibCheck"], { stdio: "inherit" });
  choice = await import(pathToFileURL(join(outDir, "agent-choice.js")).href);
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
const { parseChoiceResponse, formatChoiceAnswer, validChoice } = choice;

const failures = [];
function check(name, condition, detail = "") {
  if (condition) {
    console.log(`[PASS] ${name}`);
  } else {
    failures.push(name);
    console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

const fence = (body, tail = "\n") => "```synapforge-choice\n" + body + "\n```" + tail;
const valid = {
  id: "topic-scope",
  title: "确认主题范围",
  description: "两处都能做，选一个我往下写",
  questions: [
    { id: "topic", label: "主题范围", type: "single", required: true, options: [{ value: "llm", label: "LLM 多智能体协作" }, { value: "marl", label: "多智能体强化学习 MARL" }] },
    { id: "format", label: "产物格式", type: "multiple", options: [{ value: "md", label: "Markdown 表格" }, { value: "bib", label: "BibTeX" }] },
    { id: "extra", label: "其他要求", type: "text", placeholder: "可选补充" },
  ],
};

// 1) 正常解析：正文留在 Markdown 里、协议块被摘掉、卡片结构逐字段可读
const parsed = parseChoiceResponse(`先解释一句为什么需要你拍板。\n\n${fence(JSON.stringify(valid))}`);
check("正文与卡片分离", parsed.choice !== null && parsed.markdown === "先解释一句为什么需要你拍板。");
check("卡片字段完整", parsed.choice?.questions.length === 3 && parsed.choice.questions[2].type === "text");
check("结尾空行不影响解析", parseChoiceResponse(`正文\n${fence(JSON.stringify(valid), "\n\n")}`).choice !== null);

// 2) 不该变成控件的东西（安全边界：只有**末尾**的合法协议块才是控件）
const notCards = {
  "普通编号列表不成卡": "请选一个：\n1. LLM 多智能体协作\n2. MARL\n",
  "别的 json fence 不成卡": "```json\n" + JSON.stringify(valid) + "\n```\n",
  "协议块不在末尾不成卡": fence(JSON.stringify(valid)) + "\n后面还有一段普通解释。\n",
  "坏 JSON 不成卡且不吞原文": `正文\n${fence('{"id":"x","title":"y",,}')}`,
  "缺 questions 不成卡": fence(JSON.stringify({ id: "x", title: "y" })),
  "questions 不是数组不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: {} })),
  "questions 为空不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [] })),
  "questions 超过 20 条不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: Array.from({ length: 21 }, (_, i) => ({ id: `q${i}`, label: `问题${i}`, type: "text" })) })),
  "问题 id 重复不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "text" }, { id: "q", label: "b", type: "text" }] })),
  "问题 id 为空不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "  ", label: "a", type: "text" }] })),
  "未知 type 不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "select", options: [{ value: "1", label: "1" }] }] })),
  "text 类型带 options 不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "text", options: [{ value: "1", label: "1" }] }] })),
  "选项 value 重复不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "single", options: [{ value: "1", label: "A" }, { value: "1", label: "B" }] }] })),
  "选项超过 12 个不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "single", options: Array.from({ length: 13 }, (_, i) => ({ value: String(i), label: `选项${i}` })) }] })),
  "选项 label 缺失不成卡": fence(JSON.stringify({ id: "x", title: "y", questions: [{ id: "q", label: "a", type: "single", options: [{ value: "1" }] }] })),
  "卡片 id 为空不成卡": fence(JSON.stringify({ id: "", title: "y", questions: [{ id: "q", label: "a", type: "text" }] })),
  "顶层是数组不成卡": fence(JSON.stringify([valid])),
};
for (const [name, raw] of Object.entries(notCards)) {
  const result = parseChoiceResponse(raw);
  check(name, result.choice === null && result.markdown === raw);
}

// 3) 恶意/奇怪内容：解析器只做数据校验，不解释 HTML；标签里的标签原样保留、不生成额外控件
const hostile = {
  id: "x",
  title: "<img src=x onerror=alert(1)>",
  questions: [{ id: "q", label: "<script>alert(2)</script> 选这个", type: "single", required: true, options: [{ value: "<b>v</b>", label: "<i>加粗</i>" }] }],
};
const hostileParsed = parseChoiceResponse(fence(JSON.stringify(hostile)));
check("HTML 只当文本、不改变结构", hostileParsed.choice?.questions.length === 1 && hostileParsed.choice.questions[0].options.length === 1);
check("HTML 原样保留（由渲染层转义，不在解析层吞掉）", hostileParsed.choice?.title === "<img src=x onerror=alert(1)>");
check("validChoice 不接受非对象", [null, 42, "x", [1], undefined].every((value) => validChoice(value) === false));

// 4) 回发文案：选项回的是可读标签、没选的说"未选择"、附一句"别再重复问"
const message = formatChoiceAnswer(valid, { topic: "marl", format: ["bib", "md"], extra: "" });
check("回发带标题与逐项答案", message.includes("确认主题范围") && message.includes("主题范围：多智能体强化学习 MARL"));
check("多选按标签列出", message.includes("产物格式：BibTeX、Markdown 表格"));
check("空文本项写成未填写", message.includes("其他要求：未填写"));
check("空多选写成未选择", formatChoiceAnswer(valid, { topic: "llm", format: [], extra: "x" }).includes("产物格式：未选择"));
check("未知 value 退回原始值（不编造标签）", formatChoiceAnswer(valid, { topic: "other", format: ["zzz"], extra: "" }).includes("其他格式：zzz") === false && formatChoiceAnswer(valid, { topic: "other", format: ["zzz"], extra: "" }).includes("主题范围：other"));
check("回发要求继续执行、不重复问", message.includes("不要重复询问已经回答的问题"));

if (failures.length) {
  console.error(`AGENT_CHOICE_FAILED（${failures.length} 项未通过：${failures.join("、")}）`);
  process.exit(1);
}
console.log("AGENT_CHOICE_OK");
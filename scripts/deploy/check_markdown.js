// Markdown 渲染器的独立校验：从 workbench.html 里抠出 mdInline/renderMarkdown
// 两个函数在 node 里跑，覆盖语法与 XSS 两个面。
// 用法：node scripts/deploy/check_markdown.js
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const HTML = path.join(__dirname, "..", "..", "apps", "local", "synapforge",
  "studio", "static", "workbench.html");
const source = fs.readFileSync(HTML, "utf8");

function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`找不到函数 ${name}`);
  let depth = 0;
  let i = source.indexOf("{", start);
  const from = i;
  for (; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return source.slice(start, i + 1);
}

const sandbox = { console };
vm.createContext(sandbox);
const code = [
  extract("esc"),
  extract("mdInline"),
  extract("mdSplitRow"),
  extract("renderMarkdown"),
  "this.esc = esc; this.mdInline = mdInline; this.renderMarkdown = renderMarkdown;",
].join("\n\n");
vm.runInContext(code, sandbox);
const { renderMarkdown } = sandbox;

let pass = 0;
const failures = [];
function check(name, actual, expected) {
  const ok = typeof expected === "function" ? expected(actual) : actual === expected;
  if (ok) { pass += 1; return; }
  failures.push({ name, actual, expected });
}

// ---- 语法 ----
check("粗体", renderMarkdown("**粗**"), (out) => out.includes("<strong>粗</strong>"));
check("斜体", renderMarkdown("*斜*"), (out) => out.includes("<em>斜</em>"));
check("删除线", renderMarkdown("~~删~~"), (out) => out.includes("<del>删</del>"));
check("行内码", renderMarkdown("看 `x=1` 这里"),
  (out) => out.includes("<code>x=1</code>"));
check("行内码里的星号不被当语法", renderMarkdown("`**not bold**`"),
  (out) => out.includes("<code>**not bold**</code>") && !out.includes("<strong>"));
check("标题降一级", renderMarkdown("# 一级"), (out) => out.includes("<h2>一级</h2>"));
check("三级标题", renderMarkdown("### 三级"), (out) => out.includes("<h4>三级</h4>"));
check("无序列表", renderMarkdown("- a\n- b"),
  (out) => out.includes("<ul><li>a</li><li>b</li></ul>"));
check("有序列表", renderMarkdown("1. a\n2. b"),
  (out) => out.includes("<ol><li>a</li><li>b</li></ol>"));
check("引用", renderMarkdown("> 引用"), (out) => out.includes("<blockquote>引用</blockquote>"));
check("分隔线", renderMarkdown("---"), "<hr>");
check("段落", renderMarkdown("一句话"), "<p>一句话</p>");
check("段落内换行变 br", renderMarkdown("a\nb"), (out) => out.includes("a<br>b"));
check("围栏代码块", renderMarkdown("```js\nlet a=1;\n```"),
  (out) => out.includes('<pre class="md-code" data-lang="js"><code>let a=1;</code></pre>'));
check("代码块内不解析 Markdown", renderMarkdown("```\n**x**\n```"),
  (out) => out.includes("**x**") && !out.includes("<strong>"));
check("流式未闭合代码块", renderMarkdown("```js\nlet a=1;"),
  (out) => out.includes("<code>let a=1;</code>"));
check("表格", renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |"),
  (out) => out.includes("<th>a</th>") && out.includes("<td>1</td>"));
check("空输入", renderMarkdown(""), "");
check("空白输入", renderMarkdown("   \n  "), "");

// ---- 安全 ----
check("脚本标签被转义", renderMarkdown("<script>alert(1)</script>"),
  (out) => !out.includes("<script") && out.includes("&lt;script&gt;"));
check("img onerror 被转义", renderMarkdown('<img src=x onerror="alert(1)">'),
  (out) => !out.includes("<img") && out.includes("&lt;img"));
check("javascript: 链接降级成文本", renderMarkdown("[点我](javascript:alert(1))"),
  (out) => !out.includes("href=") && out.includes("javascript:alert(1)"));
check("data: 链接降级", renderMarkdown("[x](data:text/html,<b>)"),
  (out) => !out.includes("href="));
check("https 链接保留", renderMarkdown("[a](https://x.com)"),
  (out) => out.includes('href="https://x.com"') && out.includes("noopener"));
check("站内相对链接保留", renderMarkdown("[a](/studio)"),
  (out) => out.includes('href="/studio"'));
check("代码块里的尖括号被转义", renderMarkdown("```\n<script>\n```"),
  (out) => !out.includes("<script") && out.includes("&lt;script&gt;"));
// URL 里带引号或空格时正则不匹配 → 整段降级成纯文本（引号已被转义成 &quot;，
// 拼不出属性），所以判据是"没生成 href"，而不是"文里不能出现 onmouseover 字样"。
check("属性注入不生成链接", renderMarkdown('[a](https://x.com" onmouseover="alert(1))'),
  (out) => !out.includes("href=") && out.includes("&quot;"));

// ---- 真实样本 ----
const sample = `任务已完成并收尾：

- **任务**：撰写200字工具说明（id=25e8e02a），已 finish，状态 APPROVED
- **产出**：工作区文件 \`说明.md\`，已登记成果物

按规则处理完一项即停。`;
const rendered = renderMarkdown(sample);
check("真实样本：粗体生效", rendered, (out) => out.includes("<strong>任务</strong>"));
check("真实样本：行内码生效", rendered, (out) => out.includes("<code>说明.md</code>"));
check("真实样本：列表生效", rendered, (out) => out.includes("<ul>"));
check("真实样本：不再有字面星号", rendered, (out) => !out.includes("**"));

if (failures.length) {
  console.error(`FAIL ${failures.length} / ${pass + failures.length}`);
  for (const f of failures) {
    console.error(`\n✗ ${f.name}`);
    console.error(`  实际：${JSON.stringify(f.actual).slice(0, 300)}`);
    if (typeof f.expected !== "function") console.error(`  期望：${JSON.stringify(f.expected)}`);
  }
  process.exit(1);
}
console.log(`OK ${pass} 项全部通过`);

"""角色提示词 lint（MY-AGENT M-6）：把"提示词要足够详细"变成可执行的判据。

为什么需要它：机制是现成的（opencode 的 `--agent` + 一个 markdown 文件），角色好不好用**几乎全看提示词**。
所以提示词必须和代码一样过检查——不是评审时"看着还行"，而是九条机械判据全绿才算交付。

九条判据（与 `docs/MY_AGENT_M6_AGENT_ROLES_EXECUTION_PLAN.md` §4.4 一一对应）：
  1. frontmatter 必含 `description` 与 `mode`；**禁止**写 `model`（会与平台的模型下拉打架）
  2. 九段骨架标题齐全（固定措辞，缺一段即失败）
  3. 硬规则段 ≥8 条，其中 ≥6 条以「禁止/必须/不得/一律」开头
  4. 输出格式段含至少一个围栏代码块（可复制骨架）
  5. 自检清单 ≥5 条，其中 ≥3 条含具体阈值或字段名（数字 / 下划线字段 / 反引号）
  6. 正文长度落在 3,000–12,000 字符（**每步都重发系统提示**，太长是每步都付的代价）
  7. 泄漏检查：不得出现仓库内路径（`_utils/`、`scripts/`、`figures/`、`CLAUDE.md`、`user_data/`）
  8. 「不假装」条款必须存在（如实标注 / 不许编造 / 拿不到就说拿不到 …）
  9. 公式与 LaTeX 片段成对闭合（`\\begin{}` 与 `\\end{}` 数量一致）

用法：
    python scripts/prompt_lint.py                      # 检查 deploy/cloud-agent/roles/*.md
    python scripts/prompt_lint.py path/to/role.md …     # 检查指定文件
退出码：0 全绿；1 有失败项。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROLES_DIR = Path(__file__).resolve().parents[1] / "deploy" / "cloud-agent" / "roles"

# 本目录里**不是**角色提示词的文件（台账等）：默认检查时跳过
NON_ROLE_FILES = {"SOURCES.md", "README.md"}

# 九段骨架：标题措辞固定，lint 按前缀匹配（允许标题后补充说明，如「## 5 硬规则（禁令）」带尾巴）
REQUIRED_SECTIONS = (
    "## 1 角色定位",
    "## 2 启用与不启用",
    "## 3 输入与前置",
    "## 4 工作方法",
    "## 5 硬规则",
    "## 6 输出格式",
    "## 7 自检清单",
    "## 8 不假装条款",
    "## 9 边界与不做",
)

FORBIDDEN_PATHS = ("_utils/", "scripts/", "figures/", "CLAUDE.md", "user_data/")
HONESTY_MARKERS = ("如实标注", "不许编造", "不得编造", "拿不到就说", "不要假设", "不要假装", "不许假装", "如实说")
RULE_PREFIXES = ("禁止", "必须", "不得", "一律")
MIN_CHARS, MAX_CHARS = 3000, 12000


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _sections(body: str) -> dict[str, str]:
    """按 `## N 标题` 切段（`##` 级标题；内容到下一个 `##` 为止）。"""

    found: dict[str, str] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line.strip()
            found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return found


def _items(section: str) -> list[str]:
    """取该段里的列表条目（`- ` / `1. ` / `**1.**` 三种常见写法都算）。"""

    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if re.match(r"^([-*]\s+|\d+[.、)]\s+|(\*\*\d+[.、]\*\*)\s*)", stripped):
            items.append(stripped)
    return items


def lint(path: Path) -> list[str]:
    """返回失败项（空列表 = 全绿）。"""

    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    front, body = _split_frontmatter(text)

    # 1 frontmatter
    if not front.strip():
        problems.append("frontmatter 缺失（需要 `description` 与 `mode`）")
    else:
        if "description:" not in front:
            problems.append("frontmatter 缺 `description`（平台的角色说明就取自它）")
        if "mode:" not in front:
            problems.append("frontmatter 缺 `mode`（用 `primary`）")
        if re.search(r"^model\s*:", front, re.MULTILINE):
            problems.append("frontmatter 写了 `model`：会与页面选的模型打架，禁止")
        description = next(
            (line.split(":", 1)[1].strip() for line in front.splitlines() if line.strip().startswith("description:")),
            "",
        )
        if description:
            if " · " not in description:
                # 页面下拉取「 · 」前的短名当标签（放不下长句）；没有分隔符就会退回显示英文 id
                problems.append("`description` 要用「短名 · 一句话」格式（分隔符是**带空格的** ` · `）")
            else:
                short = description.split(" · ", 1)[0].strip()
                if len(short) > 8:
                    problems.append(f"`description` 里的短名「{short}」有 {len(short)} 字（≤8 才放得进工具条）")
                if "·" in short:
                    # 短名里再带「·」会被页面按分隔符切坏（实测：「论文·Word」在页面上只剩「论文」）
                    problems.append(f"`description` 的短名「{short}」里不能再出现「·」（会被当成分隔符切坏标签）")
                if not 20 <= len(description) <= 90:
                    problems.append(f"`description` 总长 {len(description)} 字（20–90 之间：太短说明不清、太长悬停看不完）")

    # 2 九段骨架。只认文件开头的一级 `## N`（代码块里引用其他提示词标题不算本文件的段落）。
    top_sections = [line.strip() for line in body.splitlines() if re.match(r"^## [1-9] ", line)]
    sections = _sections(body)
    for wanted in REQUIRED_SECTIONS:
        if not any(title.startswith(wanted) for title in top_sections):
            problems.append(f"缺段落：`{wanted}`")

    def section_of(prefix: str) -> str:
        for title, content in sections.items():
            if title.startswith(prefix):
                return content
        return ""

    # 3 硬规则
    rules = _items(section_of("## 5 硬规则"))
    if len(rules) < 8:
        problems.append(f"硬规则只有 {len(rules)} 条（要求 ≥8）")
    strong = [rule for rule in rules if any(re.sub(r"^[-*]\s+|\d+[.、)]\s+|\*\*", "", rule).startswith(prefix) for prefix in RULE_PREFIXES)]
    if len(strong) < 6:
        problems.append(f"以「禁止/必须/不得/一律」开头的硬规则只有 {len(strong)} 条（要求 ≥6）")

    # 4 输出格式有可复制骨架
    output_section = section_of("## 6 输出格式")
    if "```" not in output_section:
        problems.append("输出格式段没有围栏代码块（要有可直接复制的骨架）")

    # 5 自检清单可判定
    checks = _items(section_of("## 7 自检清单"))
    if len(checks) < 5:
        problems.append(f"自检清单只有 {len(checks)} 条（要求 ≥5）")
    concrete = [item for item in checks if re.search(r"\d", item) or "`" in item or re.search(r"[a-z_]{3,}_[a-z_]+", item)]
    if len(concrete) < 3:
        problems.append(f"含具体阈值/字段名的自检只有 {len(concrete)} 条（要求 ≥3）")

    # 6 体量
    length = len(body.strip())
    if length < MIN_CHARS:
        problems.append(f"正文只有 {length} 字符（要求 ≥{MIN_CHARS}）：太短通常意味着方法骨架没写全")
    if length > MAX_CHARS:
        problems.append(f"正文 {length} 字符（要求 ≤{MAX_CHARS}）：系统提示每步都重发，超了要拆")

    # 7 泄漏检查
    for token in FORBIDDEN_PATHS:
        if token in body:
            problems.append(f"出现仓库内路径 `{token}`：对话通道没有这些文件，会诱导角色去调用不存在的东西")

    # 8 不假装条款
    if not any(marker in body for marker in HONESTY_MARKERS):
        problems.append("没有「不假装」类条款（如实标注 / 不许编造 / 拿不到就说拿不到）")

    # 9 公式闭合
    begins = body.count("\\begin{")
    ends = body.count("\\end{")
    if begins != ends:
        problems.append(f"LaTeX 片段不配对：\\begin{{}} {begins} 处、\\end{{}} {ends} 处")

    return problems


def main(argv: list[str]) -> int:
    targets = [Path(item) for item in argv[1:]]
    if not targets:
        targets = sorted(path for path in ROLES_DIR.glob("*.md") if path.name not in NON_ROLE_FILES)
    if not targets:
        print(f"没有可检查的文件（{ROLES_DIR} 下没有 *.md）")
        return 1

    failed = 0
    for path in targets:
        problems = lint(path)
        status = "PASS" if not problems else "FAIL"
        print(f"[{status}] {path.name}")
        for problem in problems:
            print(f"    - {problem}")
        failed += 1 if problems else 0
    print(f"\n=== {len(targets)} 个文件，{failed} 个未通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
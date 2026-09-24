"""技能（skill）词表的归一化与版本匹配（AIP-1a）。

背景（见 `docs/AIP_1_PLAN.md` §3）：`required_capabilities` 与执行体自报的能力
此前都是**自由字符串**、按精确相等匹配，于是 `python` 与 `Python` 被当成两种能力，
而且没有版本概念。这里把"名字归一化 + 最小版本约束"做成**纯函数**：

- 声明侧（执行体上报）与要求侧（任务 `required_capabilities`）**都在读时**归一化，
  所以历史库里已有的 `Python` 不需要数据回填就能与 `python` 匹配；
- 版本只支持最小集合（无约束 / `*` / `X[.Y[.Z]]` / `>=X.Y.Z`），**不做** `^ ~ -` 区间与
  预发布标签——不支持的写法按**字面量精确比较**处理并可由调用方标注 `unparsed`，不静默放过。

技能域与**授权范围**（`task.claim`、`artifact.write`…）是两套词表：授权范围由
`_agent_scope_set` 承担，不参与任务匹配（`store.py` 里由 `SCOPE_PREFIXES` 判定）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 授权范围（scope）的命名前缀：这些值属于"能力令牌"域，不是"技能"域。
# 值形如 task.claim / artifact.write / run.create / handoff.accept / session.run。
SCOPE_PREFIXES: tuple[str, ...] = (
    "task.",
    "artifact.",
    "run.",
    "handoff.",
    "review.",
    "project.",
    "organization.",
    "device.",
    "session.",
    "terminal.",
)

_SKILL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.:-]{1,79}$")
_SEPARATORS = re.compile(r"[\s_]+")
_MULTI_DASH = re.compile(r"-{2,}")


def normalize_skill_id(raw: Any) -> str | None:
    """把任意写法收敛成规范技能 id；不合法返回 ``None``（调用方据此拒绝写入）。

    规则（对应计划 §3.2）：去空白 → 小写 → 空白与下划线折叠为 `-` → 折叠重复 `-`
    → 保留 `.`（命名空间）→ 校验字符集。
    """

    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    text = _SEPARATORS.sub("-", text)
    text = _MULTI_DASH.sub("-", text)
    text = text.strip(".-")
    if not _SKILL_PATTERN.match(text):
        return None
    return text


def is_scope_token(raw: Any) -> bool:
    """是否是授权范围值（`task.claim` 这类）——这类值不属于技能域。"""

    normalized = normalize_skill_id(raw)
    if normalized is None:
        return False
    return normalized.startswith(SCOPE_PREFIXES)


def normalize_requirement(raw: Any) -> str | None:
    """单条要求的归一化（保留版本约束）：`Python@>=3.11` → `python@3.11`。"""

    name, constraint, _ = split_requirement(raw)
    normalized = normalize_skill_id(name)
    if normalized is None:
        return None
    return f"{normalized}@{constraint}" if constraint else normalized


def normalize_capability_list(values: Iterable[Any] | None) -> list[str]:
    """要求侧列表的归一化：保序去重，丢弃非法项。"""

    result: list[str] = []
    for item in values or []:
        normalized = normalize_requirement(item)
        if normalized is None or normalized in result:
            continue
        result.append(normalized)
    return result


def split_requirement(raw: Any) -> tuple[str, str, bool]:
    """拆 `名字@约束` → (名字, 约束, 是否写了解析不了的约束)。

    约束为空表示"不要求版本"。名字里允许出现 `.`（命名空间）与 `-`；`@` 是唯一分隔符。
    """

    text = str(raw or "").strip()
    if "@" not in text:
        return text, "", False
    name, _, constraint = text.partition("@")
    constraint = constraint.strip()
    if not constraint:
        return name, "", False
    if constraint == "*":
        return name, "*", False
    candidate = constraint[2:].strip() if constraint.startswith(">=") else constraint
    if candidate.startswith("="):
        candidate = candidate[1:].strip()
    if _parse_version(candidate) is None:
        # 认不出的约束：按字面量比较（不是静默放过，调用方会标 unparsed）
        return name, constraint, True
    return name, candidate, False


def _parse_version(raw: str) -> tuple[int, ...] | None:
    if not raw:
        return None
    parts = raw.split(".")
    numbers: list[int] = []
    for part in parts:
        chunk = part.strip()
        if not chunk or not chunk.isdigit():
            return None
        numbers.append(int(chunk))
    return tuple(numbers) if numbers else None


def version_satisfies(requirement: str, provided: str, *, unparsed: bool = False) -> bool:
    """要求版本 vs 提供版本。

    - 要求为空 / `*` → 任意版本（含对方未声明版本）都满足；
    - 要求是数字版本 → 提供方必须有版本且 ≥ 要求（缺失段补 0）；
    - `unparsed=True`（约束写法不认识）→ 字面量精确比较。
    """

    required = (requirement or "").strip()
    if required.startswith(">="):
        required = required[2:].strip()
    elif required.startswith("="):
        required = required[1:].strip()
    if not required or required == "*":
        return True
    given = (provided or "").strip()
    if unparsed:
        return given == required
    wanted = _parse_version(required)
    if wanted is None:
        return given == required
    actual = _parse_version(given)
    if actual is None:
        # 提供方没声明版本 → 无法证明满足下限，如实判不满足
        return False
    width = max(len(wanted), len(actual))
    return actual + (0,) * (width - len(actual)) >= wanted + (0,) * (width - len(wanted))


def satisfies(requirement: str, provided_versions: dict[str, str]) -> bool:
    """一条要求是否被"技能名 → 版本"的提供方集合满足。"""

    name, constraint, unparsed = split_requirement(requirement)
    normalized = normalize_skill_id(name)
    if normalized is None:
        return False
    if normalized not in provided_versions:
        return False
    return version_satisfies(constraint, provided_versions.get(normalized, ""), unparsed=unparsed)


def unsatisfied_requirements(requirements: Iterable[Any], provided_versions: dict[str, str]) -> list[str]:
    """返回没被满足的要求（原始写法，便于界面直接显示"缺什么"）。"""

    missing: list[str] = []
    for item in requirements or []:
        text = str(item)
        if not satisfies(text, provided_versions):
            missing.append(text)
    return missing


def card_versions(cards: Iterable[Any]) -> dict[str, str]:
    """从能力卡列表（dict 或带属性的对象）汇总"技能名 → 版本"。

    同一个技能声明多个版本时取**最大**者（同一台机器装了两个版本，按能干活的最高版本算）。
    """

    versions: dict[str, str] = {}
    for card in cards or []:
        raw_name = card.get("skill") if isinstance(card, dict) else getattr(card, "skill", None)
        raw_version = card.get("version") if isinstance(card, dict) else getattr(card, "version", None)
        normalized = normalize_skill_id(raw_name)
        if normalized is None:
            continue
        version = str(raw_version or "").strip()
        current = versions.get(normalized, "")
        if not current or _version_key(version) > _version_key(current):
            versions[normalized] = version
    return versions


def _version_key(version: str) -> tuple[int, ...]:
    return _parse_version(version) or ()


def merge_version_maps(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """合并两个"技能名 → 版本"表（同名取版本更高者；未声明版本不覆盖已声明版本）。"""

    merged = dict(base)
    for skill, version in (extra or {}).items():
        current = merged.get(skill)
        if current is None:
            merged[skill] = version
            continue
        if not current and version:
            merged[skill] = version
        elif current and version and _version_key(version) > _version_key(current):
            merged[skill] = version
    return merged
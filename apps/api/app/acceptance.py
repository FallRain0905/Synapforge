"""确定性验收判定器（实施计划 W3.3，工作包 C）。

门禁判定只用**可判定的字面条件**，宁可 UNVERIFIED 也不猜成功：
未知语法、路径越界、探针失败、没有 recorded 输出的测试一律不通过。
这与「任务成功 ≠ 成果物已批准」的产品红线直接对应（规划 §7）。

条件语法（字符串字面量，忽略大小写）::

    file:<path> exists            文件存在
    file:<path> non-empty         文件存在且非空
    file_written:<path>           文件存在且可读回（持久化字节可取回）
    tests_passed:<command>        recorded 测试输出里出现显式 pass 摘要
    artifact:<type> approved      指定类型成果物已人工批准
    review:<role> concluded       指定角色复核已给出结论

设计借鉴：deer-flow ``subagents/acceptance_checks.py``（fail-closed、
三形状测试摘要、file_written=read-back）与 autogen ``_termination.py``
（条件 and/or 可组合）。本模块是纯逻辑：文件系统、测试记录、成果物与
复核状态全部通过注入接口（Protocol）提供，不 import 平台其余部分，
因此可独立单测，也可被执行体侧复用。

组合语义用三值逻辑（Kleene）：UNKNOWN 与 TRUE 的 AND 是 UNKNOWN，
与 FALSE 的 AND 是 FALSE——UNKNOWN 永远不可能冒充通过。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Mapping, Protocol, Sequence, Union

__all__ = [
    "VERDICT_HOLDS",
    "VERDICT_NOT_HOLDS",
    "VERDICT_UNVERIFIED",
    "FileProbe",
    "FileStat",
    "DecisionQuery",
    "TestRunLookup",
    "PathGuardProbe",
    "CriterionResult",
    "GateResult",
    "GateSpecError",
    "classify_test_output",
    "evaluate_criterion",
    "evaluate_all",
    "evaluate_gate",
]


VERDICT_HOLDS = "HOLDS"
VERDICT_NOT_HOLDS = "NOT_HOLDS"
VERDICT_UNVERIFIED = "UNVERIFIED"

#: 文件读回上限：file_written 只需要证明"字节可取回"，不需要装载整个交付物。
_READ_BACK_CAP_BYTES = 50_000

_DETAIL_MAX_CHARS = 200


class GateSpecError(ValueError):
    """门禁组合 spec 结构非法（调用方 bug，与 UNVERIFIED 的语义失败区分开）。"""


# --------------------------------------------------------------------------
# 注入接口
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileStat:
    """探针返回的文件元数据。

    ``size is None`` 表示"存在但大小无法测定"（非普通文件、受限挂载点等），
    判定器会据此降级为 UNVERIFIED 而不是猜一个结论。
    """

    exists: bool
    size: int | None = None


class FileProbe(Protocol):
    """文件探针：路径越界、权限不足等一切"无法判定"返回 None。"""

    def stat(self, path: str) -> FileStat | None: ...

    def read(self, path: str, max_bytes: int) -> bytes | None: ...


class TestRunLookup(Protocol):
    """按命令查找 recorded 测试输出（尾段即可）；没有记录返回 None。"""

    def lookup(self, command: str) -> str | None: ...


class DecisionQuery(Protocol):
    """成果物/复核状态查询；查不到或服务不可用返回 None。"""

    def artifact_approved(self, artifact_type: str) -> bool | None: ...

    def review_concluded(self, role: str) -> bool | None: ...


# --------------------------------------------------------------------------
# 判定结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionResult:
    """单个条件的判定结果（deer-flow AcceptanceLeaf 的平台版）。

    checked=False 时 holds 恒为 False——未验证的条件不允许"算它通过"。
    """

    criterion: str
    family: str  # file_exists | file_non_empty | file_written | tests_passed | artifact_approved | review_concluded | undecidable
    checked: bool
    holds: bool
    detail: str

    @property
    def verdict(self) -> str:
        if not self.checked:
            return VERDICT_UNVERIFIED
        return VERDICT_HOLDS if self.holds else VERDICT_NOT_HOLDS

    def _tri(self) -> int:
        """三值态：1=TRUE, 0=FALSE, -1=UNKNOWN。"""
        if not self.checked:
            return -1
        return 1 if self.holds else 0


@dataclass(frozen=True)
class GateResult:
    """一组条件的判定结果。

    ``verdict`` 是按组合语义（all/any）算出的门禁结论；
    ``all_hold`` 是 deer-flow 兼容的严格口径（每个叶子都 checked 且 holds），
    供审计对照——存在 any 组合时 ``all_hold`` 会比 ``verdict`` 更严。
    """

    verdict: str
    leaves: List[CriterionResult] = field(default_factory=list)
    unchecked: List[str] = field(default_factory=list)
    all_hold: bool = False

    @property
    def holds(self) -> bool:
        return self.verdict == VERDICT_HOLDS


def _tri_and(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    if a == -1 or b == -1:
        return -1
    return 1


def _tri_or(a: int, b: int) -> int:
    if a == 1 or b == 1:
        return 1
    if a == -1 or b == -1:
        return -1
    return 0


def _tri_verdict(tri: int) -> str:
    if tri == 1:
        return VERDICT_HOLDS
    if tri == 0:
        return VERDICT_NOT_HOLDS
    return VERDICT_UNVERIFIED


def _bound_detail(text: str) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= _DETAIL_MAX_CHARS:
        return cleaned
    return cleaned[: _DETAIL_MAX_CHARS - 3] + "..."


# --------------------------------------------------------------------------
# 语法解析
# --------------------------------------------------------------------------

_FILE_LEAF_RE = re.compile(r"^file:(?P<path>.+?)\s+(?P<mode>exists|non-empty)$", re.IGNORECASE)
_FILE_WRITTEN_RE = re.compile(r"^file_written:(?P<path>.+)$", re.IGNORECASE)
_TESTS_PASSED_RE = re.compile(r"^tests_passed:(?P<command>.+)$", re.IGNORECASE)
_ARTIFACT_RE = re.compile(r"^artifact:(?P<atype>.+?)\s+approved$", re.IGNORECASE)
_REVIEW_RE = re.compile(r"^review:(?P<role>.+?)\s+concluded$", re.IGNORECASE)

_MAX_CRITERION_CHARS = 512


def _normalize(criterion: str) -> str:
    return " ".join(criterion.split())


# --------------------------------------------------------------------------
# 测试输出三形状（deer-flow 语义：fail 优先，zero 否决，两者皆无 = UNVERIFIED）
# --------------------------------------------------------------------------

_TEST_PASS_SHAPE_RE = re.compile(
    r"\b[1-9]\d*\s+passed\b"  # pytest / jest: "5 passed"（0 不算过）
    r"|^OK$"  # unittest: 裸 OK 行
    r"|test result: ok"  # cargo test
    r"|^ok\s+\S"  # go test: "ok  \tpkg/path"
    r"|\bBUILD SUCCESS(?:FUL)?\b"  # maven / gradle
    r"|\ball tests passed\b",
    re.IGNORECASE | re.MULTILINE,
)

_TEST_ZERO_SHAPE_RE = re.compile(
    r"\b0\s+passed\b|\[no test files\]|\[no tests to run\]|\bRan 0 tests\b",
    re.IGNORECASE,
)

_TEST_FAIL_SHAPE_RE = re.compile(
    r"\b[1-9]\d*\s+failed\b"  # pytest / jest: "1 failed"
    r"|\b[1-9]\d*\s+errors?\b"  # pytest: "1 error"——收集期出错说明有一部分根本没跑
    r"|^FAILED\b"  # unittest 摘要行
    r"|^ERROR\s+\S"  # pytest 短摘要: "ERROR tests/unit/test_auth.py"
    r"|test result: FAILED"  # cargo test
    r"|^FAIL\s+\S"  # go test: "FAIL\tpkg/path"
    r"|\bBUILD FAILURE\b",
    re.IGNORECASE | re.MULTILINE,
)


def classify_test_output(output: str) -> str:
    """把 recorded 测试输出归类为 "pass" / "fail" / "none"。

    fail 形状优先于 pass 形状（输出里同时出现以失败为准）；
    zero 形状否决含计数的 pass 形状（"0 passed" 不是通过）。
    返回 "none" 表示输出里没有可判定的摘要——调用方应判 UNVERIFIED。
    """
    if _TEST_FAIL_SHAPE_RE.search(output):
        return "fail"
    if _TEST_ZERO_SHAPE_RE.search(output):
        return "none"
    if _TEST_PASS_SHAPE_RE.search(output):
        return "pass"
    return "none"


# --------------------------------------------------------------------------
# 单条件判定
# --------------------------------------------------------------------------


def _undecidable(criterion: str, detail: str) -> CriterionResult:
    return CriterionResult(
        criterion=criterion,
        family="undecidable",
        checked=False,
        holds=False,
        detail=_bound_detail(detail),
    )


def _judge_file_family(criterion: str, family: str, path: str, probe: FileProbe | None) -> CriterionResult:
    if probe is None:
        return _undecidable(criterion, "file probe not configured")
    st = probe.stat(path)
    if st is None:
        return CriterionResult(criterion, family, False, False, "path could not be probed (out of bounds or unavailable)")
    if not st.exists:
        return CriterionResult(criterion, family, True, False, "file does not exist")
    if st.size is None:
        return CriterionResult(criterion, family, False, False, "file size could not be established by a bounded probe")
    if family == "file_exists":
        return CriterionResult(criterion, family, True, True, f"exists, {st.size} bytes")
    if family == "file_non_empty":
        if st.size > 0:
            return CriterionResult(criterion, family, True, True, f"{st.size} bytes")
        return CriterionResult(criterion, family, True, False, "file is empty")
    # file_written：存在 + 读回（持久化的字节必须能取回）。
    content = probe.read(path, _READ_BACK_CAP_BYTES)
    if content is None:
        return CriterionResult(criterion, family, False, False, "read-back failed or probe could not read the file")
    return CriterionResult(criterion, family, True, True, f"read-back ok, {len(content)} bytes")


def evaluate_criterion(
    criterion: str,
    *,
    probe: FileProbe | None = None,
    tests: TestRunLookup | None = None,
    decisions: DecisionQuery | None = None,
) -> CriterionResult:
    """判定单个字面条件。任何无法判定的路径都落到 UNVERIFIED（fail-closed）。"""
    text = _normalize(str(criterion or ""))[:_MAX_CRITERION_CHARS]
    if not text:
        return _undecidable(text, "empty criterion")

    m = _FILE_LEAF_RE.match(text)
    if m is not None:
        family = "file_exists" if m.group("mode").lower() == "exists" else "file_non_empty"
        return _judge_file_family(text, family, m.group("path"), probe)

    m = _FILE_WRITTEN_RE.match(text)
    if m is not None:
        return _judge_file_family(text, "file_written", m.group("path"), probe)

    m = _TESTS_PASSED_RE.match(text)
    if m is not None:
        if tests is None:
            return _undecidable(text, "test run lookup not configured")
        output = tests.lookup(m.group("command"))
        if output is None:
            return CriterionResult(text, "tests_passed", False, False, "no recorded run for this command")
        shape = classify_test_output(output)
        if shape == "fail":
            return CriterionResult(text, "tests_passed", True, False, "fail shape in recorded test output")
        if shape == "pass":
            return CriterionResult(text, "tests_passed", True, True, "explicit pass summary in recorded test output")
        return CriterionResult(text, "tests_passed", False, False, "no recognizable pass/fail summary in recorded output")

    m = _ARTIFACT_RE.match(text)
    if m is not None:
        if decisions is None:
            return _undecidable(text, "decision source not configured")
        approved = decisions.artifact_approved(m.group("atype"))
        if approved is None:
            return CriterionResult(text, "artifact_approved", False, False, "artifact approval state could not be determined")
        detail = "artifact approved" if approved else "artifact not approved"
        return CriterionResult(text, "artifact_approved", True, bool(approved), detail)

    m = _REVIEW_RE.match(text)
    if m is not None:
        if decisions is None:
            return _undecidable(text, "decision source not configured")
        concluded = decisions.review_concluded(m.group("role"))
        if concluded is None:
            return CriterionResult(text, "review_concluded", False, False, "review state could not be determined")
        detail = "review concluded" if concluded else "review not concluded"
        return CriterionResult(text, "review_concluded", True, bool(concluded), detail)

    return _undecidable(text, "not deterministically checkable")


# --------------------------------------------------------------------------
# 组合与门禁 spec（JSON 友好，供未来 gate_policies JSON 列直接存储）
# --------------------------------------------------------------------------

GateSpec = Union[str, Sequence["GateSpec"], Mapping[str, Sequence["GateSpec"]]]


def _parse_spec(spec: GateSpec) -> tuple[str, list["GateSpec"]] | None:
    """返回 ("leaf"| "all" | "any", children)；叶子返回 None。"""
    if isinstance(spec, str):
        return None
    if isinstance(spec, Mapping):
        keys = set(spec.keys())
        if keys != {"all"} and keys != {"any"}:
            raise GateSpecError("composite spec must have exactly one of 'all' or 'any'")
        op = "all" if "all" in spec else "any"
        children = spec[op]
        if not isinstance(children, Sequence) or isinstance(children, str) or not children:
            raise GateSpecError(f"'{op}' requires a non-empty list")
        return op, list(children)
    if isinstance(spec, Sequence):
        if not spec:
            raise GateSpecError("composite list must be non-empty (use a bare string for a single condition)")
        return "all", list(spec)
    raise GateSpecError(f"unsupported spec node type: {type(spec).__name__}")


def evaluate_gate(
    spec: GateSpec,
    *,
    probe: FileProbe | None = None,
    tests: TestRunLookup | None = None,
    decisions: DecisionQuery | None = None,
) -> GateResult:
    """按嵌套 all/any spec 判定门禁。

    spec 形式（可直接存进 gate_policies 的 JSON 列）::

        "file:outputs/paper.pdf exists"
        ["file:outputs/paper.pdf exists", "artifact:paper approved"]
        {"all": ["file:outputs/model.py exists",
                 {"any": ["artifact:code approved", "review:critique concluded"]}]}
    """
    leaves: List[CriterionResult] = []

    def _walk(node: GateSpec) -> int:
        parsed = _parse_spec(node)
        if parsed is None:
            result = evaluate_criterion(node, probe=probe, tests=tests, decisions=decisions)
            leaves.append(result)
            return result._tri()
        op, children = parsed
        tri = -2
        for child in children:
            child_tri = _walk(child)
            tri = child_tri if tri == -2 else (_tri_and(tri, child_tri) if op == "all" else _tri_or(tri, child_tri))
        return tri

    tri = _walk(spec)
    unchecked = [leaf.criterion for leaf in leaves if not leaf.checked]
    all_hold = bool(leaves) and all(leaf.checked and leaf.holds for leaf in leaves)
    return GateResult(verdict=_tri_verdict(tri), leaves=leaves, unchecked=unchecked, all_hold=all_hold)


def evaluate_all(
    criteria: Sequence[str],
    *,
    probe: FileProbe | None = None,
    tests: TestRunLookup | None = None,
    decisions: DecisionQuery | None = None,
) -> GateResult:
    """平铺条件列表 = 隐式 all 组合（deer-flow AcceptanceVerdict 的对应物）。"""
    return evaluate_gate(list(criteria), probe=probe, tests=tests, decisions=decisions)


# --------------------------------------------------------------------------
# 参考探针：根目录约束包装（平台/执行体可以直接用，也可以另写）
# --------------------------------------------------------------------------


class PathGuardProbe:
    """把任意 FileProbe 约束在指定根目录内的包装器。

    相对路径相对 root 解析；解析后的真实路径必须仍落在 root 内，
    否则 stat/read 返回 None（判定器据此给 UNVERIFIED，而不是报错）。
    """

    def __init__(self, root: str, inner: FileProbe) -> None:
        self._root = os.path.realpath(root)
        self._inner = inner

    def _scoped(self, path: str) -> str | None:
        if not path or "\x00" in path:
            return None
        candidate = path if os.path.isabs(path) else os.path.join(self._root, path)
        resolved = os.path.realpath(candidate)
        if resolved != self._root and not resolved.startswith(self._root + os.sep):
            return None
        return resolved

    def stat(self, path: str) -> FileStat | None:
        scoped = self._scoped(path)
        if scoped is None:
            return None
        return self._inner.stat(scoped)

    def read(self, path: str, max_bytes: int) -> bytes | None:
        scoped = self._scoped(path)
        if scoped is None:
            return None
        return self._inner.read(scoped, max_bytes)

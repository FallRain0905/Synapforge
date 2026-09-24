"""把一次执行的产出变成平台成果物（CL-1，决策 D-CL-1/2/3/5/8/9）。

职责边界：

- **只报告，不判断**：产出是否可用由人工审核决定——这里创建的一律是 `PENDING_REVIEW`（D-CL-1）；
- **只收清单里的文件**：按 `workspace_scan` 的差分结果上传，不做目录同步、不扫全盘（D-CL-5）；
- **失败不判死**：上传失败只记录，**绝不**改变任务成败；本地持久化队列留待下次补传（D-CL-8、I4）；
- **回答也是内容**：回答写成 Markdown 文件后作为 `agent_answer` 入库（D-CL-3），可用
  `resource_policy.inline_answer_artifact=false` 关掉。

复用既有机制而不是新造轮子：`OutputDiscovery`（哈希/MIME/类型推断）、`ResultUploader`（本地队列）、
`AgentArtifactClient`（Agent 端成果物接口）——它们此前只在 Windows 会话 Worker 里被实例化，
常驻任务循环从来没接过（这正是本阶段要修的那半截）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

try:
    from .result_uploader import AgentArtifactClient, OutputDiscovery, ResultUploader
    from .workspace_scan import SnapshotDiff, diff, snapshot
except ImportError:  # 直接脚本执行
    from result_uploader import AgentArtifactClient, OutputDiscovery, ResultUploader
    from workspace_scan import SnapshotDiff, diff, snapshot

# 文件大小分级（D-CL-9）
MAX_DIRECT_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 回答文件放在工作区内的固定目录（属于平台自己的运行输出，已被扫描剪枝排除）
ANSWER_DIR_NAME = ".math-agent-platform/answers"


@dataclass
class CollectorConfig:
    workspace: Path
    url: str
    project_id: str
    agent_id: str
    project_token: str
    collect_files: bool = True
    include_answer: bool = True
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    # 上传队列的持久化位置：默认在平台状态目录（**不放工作区**，也不与执行体的 agentd.db 争锁）
    state_path: Path | None = None


@dataclass
class CollectorResult:
    artifact_ids: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    skipped_too_large: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diff: SnapshotDiff | None = None
    flushed_previous: int = 0

    def note(self) -> str:
        """给 Run 摘要用的一行说明（没有产出时也如实说）。"""

        if not self.artifact_ids and not self.skipped_too_large and not self.errors:
            return "产出 0 个成果物"
        parts = [f"产出 {len(self.artifact_ids)} 个成果物（待审）"]
        if self.skipped_too_large:
            parts.append(f"{len(self.skipped_too_large)} 个文件超过上传上限，仅登记未上传")
        if self.errors:
            parts.append(f"{len(self.errors)} 项上传失败（已入本地队列待补传）")
        if self.diff and (self.diff.before_truncated or self.diff.after_truncated):
            parts.append("工作区文件数超过扫描上限，产出清单可能不完整")
        return " | ".join(parts)


class _TaskShim:
    """`ResultUploader.queue_outputs` 需要一个带 `task_id`/`workspace_path` 的对象。"""

    def __init__(self, task_id: str | None, workspace_path: str) -> None:
        self.task_id = task_id
        self.workspace_path = workspace_path


class OutputCollector:
    """执行前记快照、执行后差分并把产出送到平台。"""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        client: Any | None = None,
        uploader: Any | None = None,
        state: Any | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._log = log or (lambda message: None)
        if uploader is not None:
            self._uploader = uploader
        else:
            try:
                from .local_state import LocalAgentState
            except ImportError:  # 直接脚本执行
                from local_state import LocalAgentState

            if state is None:
                state_path = config.state_path
                if state_path is None:
                    try:
                        from .sidecar_api import default_state_dir
                    except ImportError:
                        from sidecar_api import default_state_dir

                    state_path = default_state_dir() / "uploads.db"
                state = LocalAgentState(str(state_path))
            self._state = state
            artifact_client = client or AgentArtifactClient(config.url, config.project_token, config.agent_id)
            # 执行产出进"待审"（D-CL-1）
            self._uploader = ResultUploader(self._state, artifact_client, create_status="PENDING_REVIEW")
        self._before: dict[str, tuple[int, int]] = {}
        self._before_truncated = False

    # ---- 任务循环调用的两个钩子 -----------------------------------------

    async def before(self, task: dict) -> None:
        """执行前：记工作区快照 + 补传上一轮没送出去的产出。"""

        if not self.config.collect_files:
            return
        self._before, self._before_truncated = await asyncio.to_thread(snapshot, self.config.workspace)

    async def after(self, task: dict, run_id: str | None, success: bool, summary: str) -> CollectorResult:
        """执行后：差分 → 上传 → 返回成果物 id 列表（失败只记录，不抛）。"""

        result = CollectorResult()
        # 先补传历史失败（断网/平台重启后自愈），再处理本轮
        result.flushed_previous = self._flush_previous()
        if not run_id:
            return result
        try:
            result.diff = await asyncio.to_thread(self._diff_now)
            outputs = self._select_outputs(result, task, run_id, summary)
            if outputs:
                ids = self._upload(outputs, task, run_id)
                result.artifact_ids.extend(ids)
                result.uploaded.extend(item.relative_path for item in outputs if item.size_bytes <= self.config.max_upload_bytes)
        except Exception as error:  # noqa: BLE001 - 上传永不改变任务成败（I4）
            result.errors.append(f"{type(error).__name__}:{error}")
            self._log(f"[collector] 产出采集失败：{type(error).__name__}:{error}")
        return result

    # ---- 内部 -----------------------------------------------------------

    def _diff_now(self) -> SnapshotDiff:
        after, after_truncated = snapshot(self.config.workspace)
        return diff(self._before, after, before_truncated=self._before_truncated, after_truncated=after_truncated)

    def _select_outputs(self, result: CollectorResult, task: dict, run_id: str, summary: str) -> list[Any]:
        policy = task.get("resource_policy") if isinstance(task.get("resource_policy"), dict) else {}
        discovery = OutputDiscovery(self.config.workspace)
        selected: list[Any] = []

        if self.config.collect_files and result.diff is not None and policy.get("collect_outputs", True):
            for relative in result.diff.changed:
                path = Path(self.config.workspace) / relative
                try:
                    size = path.stat().st_size
                except OSError:
                    continue  # 已被删除/不可读：跳过
                if size > self.config.max_upload_bytes:
                    result.skipped_too_large.append(relative)
                    continue
                selected.append(discovery.discover([relative])[0])

        if self.config.include_answer and policy.get("inline_answer_artifact", True):
            answer_output = self._write_answer_file(run_id, summary)
            if answer_output is not None:
                selected.append(answer_output)
        return selected

    def _write_answer_file(self, run_id: str, summary: str) -> Any | None:
        """把回答写成工作区内的 Markdown 文件，再当成普通产出上传。

        回答是内容（D-CL-3）：写成文件而不是另走一条"文本入库"的接口，可以让哈希、版本、
        清单、审核全部复用既有链路。
        """

        answer = _answer_of(summary)
        if not answer:
            return None
        directory = Path(self.config.workspace) / ANSWER_DIR_NAME
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run_id}.md"
        path.write_text(answer, encoding="utf-8")
        relative = f"{ANSWER_DIR_NAME}/{run_id}.md"
        output = OutputDiscovery(self.config.workspace).discover([relative])[0]
        return _as_agent_answer(output)

    def _upload(self, outputs: list[Any], task: dict, run_id: str) -> list[str]:
        shim = _TaskShim(str(task.get("id") or "") or None, str(self.config.workspace))
        self._uploader.queue_outputs(self.config.project_id, run_id, shim, outputs)
        results = self._uploader.upload_pending(task_id=shim.task_id, run_id=run_id)
        return [item.artifact_id for item in results]

    def _flush_previous(self) -> int:
        """把之前失败/中断的上传再试一遍（按 run 分组，最多 5 轮，避免拖住任务收尾）。"""

        try:
            pending = self._uploader.state.list_uploads(["PENDING", "FAILED", "UPLOADING"])
        except Exception:  # noqa: BLE001 - 队列不可读不影响本轮
            return 0
        run_ids: list[str] = []
        for item in pending:
            run_id = str(item.get("run_id") or "")
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
        flushed = 0
        for run_id in run_ids[:5]:
            try:
                flushed += len(self._uploader.upload_pending(task_id=None, run_id=run_id))
            except Exception as error:  # noqa: BLE001
                self._log(f"[collector] 补传 {run_id} 失败：{type(error).__name__}")
        return flushed


# 没有回复文本时 `summarize_codex_result` 写的占位句（见 codex_executor）：
# 它是给界面看的说明，不是内容——不能变成"回答成果物"。
NO_ANSWER_PLACEHOLDER = "（本次执行没有产出回复文本）"


def _answer_of(summary: str) -> str:
    """从摘要里取回答（回答在前，`---` 之后是诊断）；没有真实回答就返回空串。"""

    text = (summary or "").strip()
    if not text:
        return ""
    marker = "\n---\n"
    if marker not in text:
        # 没有分隔符说明这条摘要只有诊断（声明式命令路径的 `exit=… | stdout: …`）：
        # 那不是"回答"，不能变成内容。
        return ""
    answer = text.split(marker, 1)[0].strip()
    if answer == NO_ANSWER_PLACEHOLDER:
        return ""
    return answer


def _as_agent_answer(output: Any) -> Any:
    """把回答文件的成果物类型改写为 `agent_answer`（其余字段保持发现结果）。"""

    return replace(output, artifact_type="agent_answer", name="Agent 回答")


__all__ = ["CollectorConfig", "CollectorResult", "MAX_DIRECT_BYTES", "MAX_UPLOAD_BYTES", "OutputCollector"]
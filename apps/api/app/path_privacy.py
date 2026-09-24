"""对外口径：执行体自报的宿主机绝对路径不能原样出现在成员的响应里（FM-0）。

库里存的是**真值**——`agents.local_workspace`、`runs.observed_input_files`、`artifacts.source_path`
都来自执行体在自己机器上看到的东西，边界判定（`boundary_gate`）、审计和排障都要用它们，所以库内保持原样。
但普通成员的响应是另一回事：一条 `C:\\Users\\某人\\Desktop\\…` 既泄露了那台机器的目录结构，
也让人误以为"平台能直接读那台机器"——实际上平台连不上任何人家的电脑，文件链路要靠 Agent 主动上报
（见 `docs/FILE_MANAGEMENT_AND_AGENT_DRIVE_EXECUTION_PLAN.md` 的 D3）。

对外只给两样东西：

- `workspace_identity`：工作区路径的 sha256 前 12 位。稳定、可比较（"这条记录和刚才那条是不是同一台机器"），
  但反推不出路径本身，也不会随机器换用户目录而变；
- 文件路径的**文件名**标签（`…/data.csv`）：成员需要知道"是哪个文件"，不需要知道它在谁的哪个盘。

设备侧接口（`/api/agent/*`、设备令牌调用的写接口）**不经过**这里：执行体要能回读自己上报的值来对账，
把它收窄会让"注册的工作区与实际 Runner cwd 一致"这件事变得不可验证。
"""

from __future__ import annotations

import hashlib

from .contracts import Agent, Artifact, Run

WORKSPACE_IDENTITY_PREFIX = "ws-"


def workspace_identity(raw: str | None) -> str | None:
    """工作区路径 → 稳定对外标识（`ws-` + sha256 前 12 位）；没上报就是 None。

    归一化只做两件不改语义的事：统一分隔符、去掉末尾分隔符。**不**做大小写折叠——
    同一台机器重复上报的路径本来就是同一个字符串，折了反而把 `…/Work` 与 `…/work` 混成一个。
    """

    if raw is None:
        return None
    normalized = str(raw).strip().replace("\\", "/").rstrip("/")
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{WORKSPACE_IDENTITY_PREFIX}{digest[:12]}"


def path_label(raw: str | None) -> str | None:
    """绝对路径 → 只留最后一段（`C:\\a\\b\\x.csv` → `…/x.csv`），空值原样返回 None。"""

    if raw is None:
        return None
    parts = [part for part in str(raw).replace("\\", "/").split("/") if part]
    if not parts:
        return None
    return f"…/{parts[-1]}"


def public_agent(agent: Agent) -> Agent:
    """成员可见的 Agent：不给绝对路径，改成稳定标识。"""

    return agent.model_copy(
        update={"local_workspace": None, "workspace_identity": workspace_identity(agent.local_workspace)}
    )


def public_run(run: Run) -> Run:
    """成员可见的 Run：观察到输入文件只留文件名（编号与边界结论不受影响）。

    边界结论里**也**嵌了一份原始路径（`information_boundary.observed_input_files` 与
    违规项里的 `path`）——同一个泄漏的第二条通路，所以一起收窄。
    """

    observed = [label for label in (path_label(value) for value in run.observed_input_files or []) if label]
    boundary = dict(run.information_boundary or {})
    if boundary:
        if boundary.get("observed_input_files"):
            boundary["observed_input_files"] = [
                label for label in (path_label(value) for value in boundary["observed_input_files"]) if label
            ]
        violations = boundary.get("violations")
        if isinstance(violations, list):
            boundary["violations"] = [
                {**item, "path": path_label(item.get("path"))} if isinstance(item, dict) and item.get("path") else item
                for item in violations
            ]
    return run.model_copy(update={"observed_input_files": observed, "information_boundary": boundary})


def public_artifact(artifact: Artifact) -> Artifact:
    """成员可见的成果物：来源路径只留文件名（内容 hash 与版本才是身份）。"""

    return artifact.model_copy(update={"source_path": path_label(artifact.source_path)})


__all__ = ["WORKSPACE_IDENTITY_PREFIX", "path_label", "public_agent", "public_artifact", "public_run", "workspace_identity"]
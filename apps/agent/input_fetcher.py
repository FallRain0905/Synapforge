"""把平台上的输入文件落到执行体工作目录（MY-AGENT M-3）。

为什么需要这一层：平台早就有 `input_artifacts`（任务）与「对话带附件」，但**内核从不读它**——
执行体在工作目录里看不到任何上传的文件，"帮我改这份文档"这类任务根本没法做。
这里负责把文件真正取下来，**且只落在一个受控目录**里：

- 目录固定为 `<workspace>/inputs/`（不放工作目录根，避免和产出文件混在一起）；
- 文件名只取 basename（挡路径穿越），重名自动加序号，空名回落到 `input-<id 前 8 位>`；
- 单个文件有大小上限（默认 64MB，与平台上传上限同量级），超限**拒绝**而不是截断；
- 下载失败**如实返回失败清单**（不静默跳过）：调用方把"哪些附件没拿到"写进提示词，
  执行体和人都会看到——缺文件比假装有文件好；
- 落盘同时写一份清单（FM-0）：`artifact id → inputs/<文件名> → sha256 → 字节数`。平台只发 id、
  磁盘上只有文件名，中间这一跳不留证，事后就无法回答"这次 Run 读到的到底是不是那个成果物"。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
INPUT_DIR_NAME = "inputs"
# 平台自己写元数据的目录（与 workspace_scan/输出采集的排除项一致：不会被当成产出收走）
MANIFEST_DIR_NAME = ".math-agent-platform"
MANIFEST_FILE_NAME = "inputs-manifest.json"
MANIFEST_SCHEMA = "inputs-manifest/1"


class InputFetchError(RuntimeError):
    pass


def _safe_name(name: str, artifact_id: str) -> str:
    """只取 basename 并清洗：路径分隔符、控制字符、`.`/`..` 一律处理掉。"""

    candidate = str(name or "").replace("\\", "/").split("/")[-1].strip()
    candidate = "".join(ch for ch in candidate if ch not in '\r\n\t\x00' and ch.isprintable())
    if not candidate or candidate in {".", ".."}:
        candidate = f"input-{artifact_id[:8]}"
    return candidate[:120]


def manifest_path(workspace: Path) -> Path:
    """输入清单的落点：与 run-manifests/answers 同一个平台元数据目录。"""

    return Path(workspace) / MANIFEST_DIR_NAME / MANIFEST_FILE_NAME


def read_inputs_manifest(workspace: Path) -> dict:
    """读回输入清单（读不到/坏掉一律当空：清单是**台账**，缺失不能拦住任务执行）。"""

    try:
        payload = json.loads(manifest_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("schema") == MANIFEST_SCHEMA else {}


def write_inputs_manifest(
    workspace: Path,
    entries: Iterable[dict],
    *,
    failures: Iterable[str] = (),
    log: Callable[[str], None] = print,
) -> Path | None:
    """把"这一轮哪些成果物落成了哪个文件"合并进清单，返回清单路径（写不进去就 None）。

    合并规则：先在旧清单里删掉文件已经不在了的条目（人不只在对话框里加附件，也会手工清理工作区），
    再按相对路径覆盖同名条目——**同一目录被反复物化时不留一串幽灵条目**。
    写失败不影响这一轮执行：文件已经落地了，清单只是台账（如实打日志，不假装写成功）。
    """

    path = manifest_path(workspace)
    previous = read_inputs_manifest(workspace)
    merged: dict[str, dict] = {}
    for item in previous.get("entries") or []:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("relative_path") or "")
        if relative and (Path(workspace) / relative).is_file():
            merged[relative] = item
    for item in entries:
        relative = str(item.get("relative_path") or "")
        if relative:
            merged[relative] = item
    payload = {
        "schema": MANIFEST_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "directory": INPUT_DIR_NAME,
        "entries": list(merged.values()),
        "failures": [str(item) for item in failures],
    }
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        log(f"[inputs] 清单写入失败（不影响本轮执行）：{error}")
        return None
    return path


def materialize_inputs(
    client: Any,
    files: Iterable[Any],
    *,
    workspace: Path,
    log: Callable[[str], None] = print,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[str], list[str]]:
    """把 files 逐个下到 `<workspace>/inputs/`，返回 `(落地文件名, 失败说明)`。

    `files` 里每项要有 `artifact_id` 与 `name`（平台 claim 响应给的就是这两样）。
    落地成功后写一份清单：`artifact id → inputs/<文件名> → sha256 → 字节数`。
    """

    directory = Path(workspace) / INPUT_DIR_NAME
    written: list[str] = []
    failed: list[str] = []
    entries: list[dict] = []
    for item in files:
        artifact_id = str(getattr(item, "artifact_id", "") or (item.get("artifact_id") if isinstance(item, dict) else ""))
        name = str(getattr(item, "name", "") or (item.get("name") if isinstance(item, dict) else ""))
        if not artifact_id:
            failed.append(f"{name or '未命名'}: 缺少 artifact_id")
            continue
        target_name = _safe_name(name, artifact_id)
        target = directory / target_name
        try:
            result = client.download(artifact_id, max_bytes=max_bytes)
            # 兼容两种返回：老的只有字节；现在带文件名（响应头里的真名优先）
            content, served_name = result if isinstance(result, tuple) else (result, "")
            # 名字优先级：**平台给的名字 > 响应头里的名字**（响应头可能只有 ASCII 兜底，
            # 中文名会退化成 artifact.md；任务侧平台不给名字，才用响应头）
            if not name and served_name:
                target_name = _safe_name(served_name, artifact_id)
        except InputFetchError as error:
            failed.append(f"{target_name}: {error}")
            continue
        except Exception as error:  # noqa: BLE001 - 任何下载失败都如实报告，不假装成功
            failed.append(f"{target_name}: {type(error).__name__}")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        # 真名拿到之后再定路径（上面 target 是用"平台给的名字"算的，可能已经被响应头里的真名换掉）
        target = directory / target_name
        # 重名不覆盖：加 -2 / -3 后缀（同一轮里两份同名文件都要能拿到）
        suffix = 2
        while target.exists():
            target = directory / f"{Path(target_name).stem}-{suffix}{Path(target_name).suffix}"
            suffix += 1
        try:
            target.write_bytes(content)
        except OSError as error:
            failed.append(f"{target_name}: 写入失败 {error.strerror}")
            continue
        written.append(target.name)
        entries.append(
            {
                "artifact_id": artifact_id,
                "name": target.name,
                "relative_path": f"{INPUT_DIR_NAME}/{target.name}",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "source": "input_artifacts",
                "materialized_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log(f"[inputs] 已落到 {INPUT_DIR_NAME}/{target.name}（{len(content)} 字节）")
    if entries or failed:
        write_inputs_manifest(workspace, entries, failures=failed, log=log)
    return written, failed


def prompt_with_inputs(prompt: str, written: list[str], failed: list[str]) -> str:
    """把"有哪些输入文件"写进提示词（执行体靠它知道去哪儿看；缺的也如实说）。"""

    lines: list[str] = []
    if written:
        lines.append(f"本轮提供了 {len(written)} 个文件，已放在工作目录的 {INPUT_DIR_NAME}/ 下：" + "、".join(written))
    if failed:
        lines.append("以下附件没有取到（不要假设它们存在）：" + "；".join(failed))
    if not lines:
        return prompt
    return f"[{chr(10).join(lines)}]{chr(10)}{prompt}"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "INPUT_DIR_NAME",
    "MANIFEST_DIR_NAME",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA",
    "InputFetchError",
    "manifest_path",
    "materialize_inputs",
    "prompt_with_inputs",
    "read_inputs_manifest",
    "write_inputs_manifest",
]
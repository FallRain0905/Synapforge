"""状态目录与配置解析。

凭据只存在于**仓库外**的状态目录里：本仓库托管在 GitHub 且是公开的。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "SYNAPFORGE_STATE_DIR"


def state_dir() -> Path:
    raw = os.environ.get(STATE_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".synapforge"


def db_path() -> Path:
    return state_dir() / "state.db"


def config_path() -> Path:
    return state_dir() / "config.json"


def artifacts_dir() -> Path:
    return state_dir() / "artifacts"


def workspace_dir() -> Path:
    return state_dir() / "workspace"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def llm_config() -> dict[str, str]:
    block = load_config().get("llm") or {}
    return {
        "base_url": str(block.get("base_url") or "").strip(),
        "api_key": str(block.get("api_key") or "").strip(),
        "model": str(block.get("model") or "").strip(),
    }

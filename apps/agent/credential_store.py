"""OS-backed storage for long-lived Agent credentials.

The production implementation uses the Windows Credential Manager through
the native Advapi32 API.  The in-memory implementation is intentionally
limited to tests and explicit development wiring; it is never selected by
the default factory.

POSIX headless 机器（云端执行体）没有凭据管理器，等价物是 **0600 文件 + 0700 目录**：
`FileCredentialStore` 就是它。这是对"桌面端不得把设备/项目令牌持久化到 Windows 凭据管理器之外"
那条约束的**限定例外**——该约束的意图是"别在 Windows 上到处撒密钥"，而 headless 没有别的选择；
所以文件后端**只在非 Windows 生效**（Windows 上必须显式 `backend="file"`），Windows 行为不变。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_ACCESS_DENIED = 5
ERROR_NOT_FOUND = 1168
ERROR_NO_SUCH_LOGON_SESSION = 1312
MAX_TARGET_LENGTH = 512
MAX_SECRET_BYTES = 512
# 文件后端的权限：文件 0600、目录 0700（组/其他一律无权限）
CREDENTIAL_FILE_MODE = 0o600
CREDENTIAL_DIRECTORY_MODE = 0o700


class CredentialStoreError(RuntimeError):
    """Base class for credential backend and policy failures."""


class CredentialNotFound(CredentialStoreError):
    pass


class CredentialPermissionDenied(CredentialStoreError):
    pass


class CredentialBackendUnavailable(CredentialStoreError):
    pass


class CredentialInvalid(CredentialStoreError):
    pass


class CredentialStore(Protocol):
    def put(self, target: str, secret: str) -> None: ...

    def get(self, target: str) -> str: ...

    def delete(self, target: str) -> bool: ...


def _validate_target(target: str) -> str:
    if not isinstance(target, str) or not target or len(target) > MAX_TARGET_LENGTH or "\x00" in target:
        raise CredentialInvalid("credential_target_invalid")
    return target


def _validate_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret or "\x00" in secret:
        raise CredentialInvalid("credential_secret_invalid")
    try:
        encoded = secret.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CredentialInvalid("credential_secret_invalid") from error
    if len(encoded) > MAX_SECRET_BYTES:
        raise CredentialInvalid("credential_secret_too_large")
    return encoded


def device_token_target(device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id or "\x00" in device_id or len(device_id) > 256:
        raise CredentialInvalid("device_id_invalid_for_credential_target")
    return f"MathAgentPlatform/device-token/{device_id}"


def project_token_target(project_id: str) -> str:
    """项目能力 Token 的凭据目标；与设备 Token 分开存放，便于单独轮换/撤销。"""

    if not isinstance(project_id, str) or not project_id or "\x00" in project_id or len(project_id) > 256:
        raise CredentialInvalid("project_id_invalid_for_credential_target")
    return f"MathAgentPlatform/project-token/{project_id}"


class InMemoryCredentialStore:
    """Explicit test/development store; never used by the production factory."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, target: str, secret: str) -> None:
        target = _validate_target(target)
        _validate_secret(secret)
        self._values[target] = secret

    def get(self, target: str) -> str:
        target = _validate_target(target)
        try:
            return self._values[target]
        except KeyError as error:
            raise CredentialNotFound("credential_not_found") from error

    def delete(self, target: str) -> bool:
        target = _validate_target(target)
        return self._values.pop(target, None) is not None


class FileCredentialStore:
    """POSIX 0600 文件后端：headless 机器（云端执行体）上替代 Windows 凭据管理器。

    落盘细节（每一条都是"别把密钥弄丢/弄漏"的防线）：

    - 文件名 = `sha256(target)[:32]`——**目标名不落明文**（目标名里带 project_id/device_id）；
    - 内容含 `target` 与 `secret`，读取时校验两者一致——防止把别的目标的文件当成这个目标的值；
    - 权限过宽（组或其他可读写）**直接拒绝**并报错，而不是默默使用；写入用"临时文件 + `os.replace`"，
      读到的要么是旧值要么是新值，不会是半截文件；
    - 目录首次写入时建为 0700。
    """

    def __init__(self, directory: str | os.PathLike[str], *, allow_windows: bool = False) -> None:
        if os.name == "nt" and not allow_windows:
            # Windows 上必须显式指定：默认路径永远是凭据管理器（见模块 docstring 的限定例外）
            raise CredentialBackendUnavailable("credential_file_backend_posix_only")
        self.directory = Path(directory)

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, CREDENTIAL_DIRECTORY_MODE)
        except OSError as error:
            raise CredentialStoreError(f"credential_directory_unusable:{error.strerror}") from error

    def _path(self, target: str) -> Path:
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.credential"

    def put(self, target: str, secret: str) -> None:
        target = _validate_target(target)
        _validate_secret(secret)
        self._ensure_directory()
        path = self._path(target)
        payload = json.dumps({"target": target, "secret": secret}, ensure_ascii=False)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, prefix=".tmp-", delete=False
        )
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.chmod(temporary, CREDENTIAL_FILE_MODE)
            os.replace(temporary, path)
        except OSError as error:
            raise CredentialStoreError(f"credential_write_failed:{error.strerror}") from error
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:  # 清理失败不影响写入结果
                    pass

    def get(self, target: str) -> str:
        target = _validate_target(target)
        path = self._path(target)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError as error:
            raise CredentialNotFound("credential_not_found") from error
        except OSError as error:
            raise CredentialStoreError(f"credential_read_failed:{error.strerror}") from error
        if os.name != "nt" and mode & 0o077:
            # Windows 上 mode 位是模拟的（只能表达只读），真实权限由 ACL 决定；
            # 文件后端在 Windows 属于显式 opt-in，所以这条校验只在 POSIX 生效。
            raise CredentialPermissionDenied("credential_file_permissions_too_open")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise CredentialNotFound("credential_not_found") from error
        except (OSError, ValueError) as error:
            raise CredentialInvalid("credential_file_unreadable") from error
        if not isinstance(data, dict) or data.get("target") != target:
            raise CredentialInvalid("credential_file_target_mismatch")
        secret = data.get("secret")
        if not isinstance(secret, str):
            raise CredentialInvalid("credential_secret_invalid")
        _validate_secret(secret)
        return secret

    def delete(self, target: str) -> bool:
        target = _validate_target(target)
        try:
            os.unlink(self._path(target))
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise CredentialStoreError(f"credential_delete_failed:{error.strerror}") from error


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


@dataclass(frozen=True)
class _WindowsCredentialApi:
    advapi32: object
    kernel32: object


class WindowsCredentialManager:
    """Windows Credential Manager adapter for generic UTF-8 secrets."""

    def __init__(self, api: _WindowsCredentialApi | None = None) -> None:
        if os.name != "nt":
            raise CredentialBackendUnavailable("credential_manager_windows_only")
        self._api = api or self._load_api()
        pointer = ctypes.POINTER(_CREDENTIALW)
        self._api.advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
        self._api.advapi32.CredWriteW.restype = ctypes.c_bool
        self._api.advapi32.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(pointer)]
        self._api.advapi32.CredReadW.restype = ctypes.c_bool
        self._api.advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self._api.advapi32.CredDeleteW.restype = ctypes.c_bool
        self._api.advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._api.advapi32.CredFree.restype = None
        self._api.kernel32.GetLastError.restype = ctypes.c_uint32

    @staticmethod
    def _load_api() -> _WindowsCredentialApi:
        try:
            return _WindowsCredentialApi(
                ctypes.WinDLL("Advapi32", use_last_error=True),
                ctypes.WinDLL("Kernel32", use_last_error=True),
            )
        except (AttributeError, OSError) as error:
            raise CredentialBackendUnavailable("credential_manager_unavailable") from error

    def put(self, target: str, secret: str) -> None:
        target = _validate_target(target)
        blob = _validate_secret(secret)
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _CREDENTIALW(
            Flags=0,
            Type=CRED_TYPE_GENERIC,
            TargetName=target,
            Comment=None,
            LastWritten=_FILETIME(),
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName="MathAgentPlatform",
        )
        if not self._api.advapi32.CredWriteW(ctypes.byref(credential), 0):
            self._raise_last_error("credential_write_failed")

    def get(self, target: str) -> str:
        target = _validate_target(target)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            self._raise_last_error("credential_read_failed")
        try:
            if not pointer or not pointer.contents.CredentialBlob:
                raise CredentialInvalid("credential_blob_missing")
            size = int(pointer.contents.CredentialBlobSize)
            if size <= 0 or size > MAX_SECRET_BYTES:
                raise CredentialInvalid("credential_blob_invalid")
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
            try:
                secret = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CredentialInvalid("credential_blob_not_utf8") from error
            _validate_secret(secret)
            return secret
        finally:
            self._api.advapi32.CredFree(pointer)

    def delete(self, target: str) -> bool:
        target = _validate_target(target)
        if self._api.advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            return True
        error_code = self._last_error()
        if error_code == ERROR_NOT_FOUND:
            return False
        self._raise_last_error("credential_delete_failed", error_code)
        return False

    def _last_error(self) -> int:
        return int(ctypes.get_last_error()) if hasattr(ctypes, "get_last_error") else int(self._api.kernel32.GetLastError())

    def _raise_last_error(self, operation: str, error_code: int | None = None) -> None:
        code = self._last_error() if error_code is None else error_code
        if code == ERROR_NOT_FOUND:
            raise CredentialNotFound("credential_not_found")
        if code == ERROR_ACCESS_DENIED:
            raise CredentialPermissionDenied("credential_permission_denied")
        if code == ERROR_NO_SUCH_LOGON_SESSION:
            raise CredentialBackendUnavailable("credential_persistence_unavailable")
        raise CredentialStoreError(f"{operation}:{code}")


def create_credential_store(
    *,
    backend: str = "auto",
    directory: str | os.PathLike[str] | None = None,
) -> CredentialStore:
    """选凭据后端。

    - `auto`（默认）：Windows 走凭据管理器，其它平台走 0600 文件后端（需要 `directory`）；
    - `windows`：Windows 凭据管理器（非 Windows 会抛 `CredentialBackendUnavailable`）；
    - `file`：文件后端（Windows 上也可用，但必须显式选它）；
    - `memory`：只给测试与显式开发接线，**永远不是默认**。
    """

    if backend == "memory":
        return InMemoryCredentialStore()
    if backend == "windows":
        return WindowsCredentialManager()
    if backend == "file":
        if directory is None:
            raise CredentialInvalid("credential_file_directory_required")
        return FileCredentialStore(directory, allow_windows=True)
    if backend == "auto":
        if os.name == "nt":
            return WindowsCredentialManager()
        if directory is None:
            raise CredentialInvalid("credential_file_directory_required")
        return FileCredentialStore(directory)
    raise CredentialBackendUnavailable("credential_backend_unsupported")


__all__ = [
    "CREDENTIAL_DIRECTORY_MODE",
    "CREDENTIAL_FILE_MODE",
    "CredentialBackendUnavailable",
    "CredentialInvalid",
    "CredentialNotFound",
    "CredentialPermissionDenied",
    "CredentialStore",
    "CredentialStoreError",
    "FileCredentialStore",
    "InMemoryCredentialStore",
    "WindowsCredentialManager",
    "create_credential_store",
    "device_token_target",
    "project_token_target",
]

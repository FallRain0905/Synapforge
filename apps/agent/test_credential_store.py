from __future__ import annotations

import ctypes
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentd import resolve_device_token
from credential_store import (
    CREDENTIAL_DIRECTORY_MODE,
    CREDENTIAL_FILE_MODE,
    CRED_PERSIST_LOCAL_MACHINE,
    CredentialBackendUnavailable,
    CredentialInvalid,
    CredentialNotFound,
    CredentialPermissionDenied,
    FileCredentialStore,
    InMemoryCredentialStore,
    WindowsCredentialManager,
    _CREDENTIALW,
    _FILETIME,
    create_credential_store,
    device_token_target,
)


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeCredentialApi:
    def __init__(self) -> None:
        self.values: dict[str, tuple[object, bytes, int]] = {}
        self.error = 0
        self.live: list[object] = []
        self.advapi32 = SimpleNamespace(
            CredWriteW=FakeFunction(self.write),
            CredReadW=FakeFunction(self.read),
            CredDeleteW=FakeFunction(self.delete),
            CredFree=FakeFunction(self.free),
        )
        self.kernel32 = SimpleNamespace(GetLastError=FakeFunction(lambda: self.error))

    def write(self, pointer, _flags):
        credential = ctypes.cast(pointer, ctypes.POINTER(_CREDENTIALW)).contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        self.values[credential.TargetName] = (credential, blob, credential.Persist)
        self.error = 0
        return True

    def read(self, target, _type, _flags, out_pointer):
        value = self.values.get(target)
        if value is None:
            ctypes.set_last_error(1168)
            return False
        source, blob, persist = value
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _CREDENTIALW(
            Flags=0,
            Type=1,
            TargetName=target,
            Comment=None,
            LastWritten=_FILETIME(),
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=persist,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName="MathAgentPlatform",
        )
        self.live.append((credential, buffer, source))
        output = ctypes.cast(out_pointer, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)))
        output[0] = ctypes.pointer(credential)
        ctypes.set_last_error(0)
        return True

    def delete(self, target, _type, _flags):
        if target not in self.values:
            ctypes.set_last_error(1168)
            return False
        del self.values[target]
        ctypes.set_last_error(0)
        return True

    def free(self, _pointer):
        return None


class CredentialStoreTests(unittest.TestCase):
    def test_memory_store_roundtrip_and_missing_secret(self) -> None:
        store = InMemoryCredentialStore()
        target = device_token_target("device-001")
        store.put(target, "dvc_secret")
        self.assertEqual(store.get(target), "dvc_secret")
        self.assertTrue(store.delete(target))
        self.assertFalse(store.delete(target))
        with self.assertRaisesRegex(CredentialNotFound, "credential_not_found"):
            store.get(target)

    def test_target_and_secret_validation_is_fail_closed(self) -> None:
        store = InMemoryCredentialStore()
        with self.assertRaisesRegex(CredentialInvalid, "credential_target_invalid"):
            store.put("", "secret")
        with self.assertRaisesRegex(CredentialInvalid, "credential_secret_invalid"):
            store.put("target", "")
        with self.assertRaisesRegex(CredentialInvalid, "credential_secret_too_large"):
            store.put("target", "x" * 513)
        with self.assertRaisesRegex(CredentialInvalid, "device_id_invalid"):
            device_token_target("bad\x00device")

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
    def test_windows_adapter_roundtrip_uses_machine_persistence(self) -> None:
        api = FakeCredentialApi()
        store = WindowsCredentialManager(api=SimpleNamespace(advapi32=api.advapi32, kernel32=api.kernel32))
        target = device_token_target("device-fake-001")
        store.put(target, "dvc_fake_secret")
        self.assertEqual(api.values[target][1], b"dvc_fake_secret")
        self.assertEqual(api.values[target][2], CRED_PERSIST_LOCAL_MACHINE)
        self.assertEqual(store.get(target), "dvc_fake_secret")
        self.assertTrue(store.delete(target))
        self.assertFalse(store.delete(target))

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
    def test_windows_adapter_maps_missing_credential(self) -> None:
        api = FakeCredentialApi()
        store = WindowsCredentialManager(api=SimpleNamespace(advapi32=api.advapi32, kernel32=api.kernel32))
        with self.assertRaisesRegex(CredentialNotFound, "credential_not_found"):
            store.get(device_token_target("missing-device"))

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
    def test_windows_persistence_failure_is_not_downgraded_to_session(self) -> None:
        from credential_store import ERROR_NO_SUCH_LOGON_SESSION

        class FailingApi(FakeCredentialApi):
            def write(self, _pointer, _flags):
                ctypes.set_last_error(ERROR_NO_SUCH_LOGON_SESSION)
                return False

        api = FailingApi()
        store = WindowsCredentialManager(api=SimpleNamespace(advapi32=api.advapi32, kernel32=api.kernel32))
        with self.assertRaisesRegex(CredentialBackendUnavailable, "credential_persistence_unavailable"):
            store.put(device_token_target("persistence-device"), "dvc_secret")

    def test_create_credential_store_never_defaults_to_memory(self) -> None:
        from credential_store import create_credential_store

        self.assertIsInstance(create_credential_store(backend="memory"), InMemoryCredentialStore)
        with self.assertRaisesRegex(CredentialBackendUnavailable, "credential_backend_unsupported"):
            create_credential_store(backend="unknown")

    def test_agent_token_resolution_prefers_explicit_development_token(self) -> None:
        args = SimpleNamespace(device_id="device-001", device_token="explicit-token", credential_target=None)
        with patch("agentd.WindowsCredentialManager") as manager:
            self.assertEqual(resolve_device_token(args), "explicit-token")
            manager.assert_not_called()

    def test_agent_token_resolution_uses_device_target_from_credential_store(self) -> None:
        store = InMemoryCredentialStore()
        store.put(device_token_target("device-002"), "stored-token")
        args = SimpleNamespace(device_id="device-002", device_token=None, credential_target=None)
        with patch("agentd.WindowsCredentialManager", return_value=store):
            self.assertEqual(resolve_device_token(args), "stored-token")


class FileCredentialStoreTests(unittest.TestCase):
    """POSIX 0600 文件后端：云端执行体（headless）用的那个。

    为什么这些断言重要：这台机器上没有凭据管理器，令牌就落在一个普通文件里——
    权限、目标名、原子写这三件事只要有一件松了，就等于把项目能力令牌摊在磁盘上。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name) / "credentials"
        # allow_windows=True：测试要能在 Windows 开发机上跑（部署时不允许这么开，见 test_windows_requires_explicit_opt_in）
        self.store = FileCredentialStore(self.directory, allow_windows=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_roundtrip_writes_0600_file_into_0700_directory(self) -> None:
        target = device_token_target("device-cloud-001")
        self.store.put(target, "dvc_cloud_secret")
        self.assertEqual(self.store.get(target), "dvc_cloud_secret")
        files = list(self.directory.glob("*.credential"))
        self.assertEqual(len(files), 1)
        if os.name != "nt":  # Windows 的 mode 位是模拟的（只能表达只读位），权限断言只在 POSIX 有意义
            self.assertEqual(stat.S_IMODE(os.stat(files[0]).st_mode), CREDENTIAL_FILE_MODE)
            self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), CREDENTIAL_DIRECTORY_MODE)

    def test_target_name_is_not_written_into_the_filename(self) -> None:
        target = device_token_target("device-secret-name")
        self.store.put(target, "secret")
        name = next(self.directory.glob("*.credential")).name
        self.assertNotIn("device-secret-name", name)
        self.assertNotIn("device", name)

    @unittest.skipIf(os.name == "nt", "mode bits are simulated on Windows; the guard is POSIX-only")
    def test_overly_open_permissions_are_refused_not_used(self) -> None:
        target = device_token_target("device-open-001")
        self.store.put(target, "secret")
        path = next(self.directory.glob("*.credential"))
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(CredentialPermissionDenied, "credential_file_permissions_too_open"):
            self.store.get(target)
        # 重新写入会把权限收回来
        self.store.put(target, "secret")
        self.assertEqual(self.store.get(target), "secret")

    def test_write_leaves_no_temporary_files_behind(self) -> None:
        self.store.put(device_token_target("device-tmp-001"), "secret")
        self.assertEqual(list(self.directory.glob(".tmp-*")), [])

    def test_mismatched_target_inside_the_file_is_rejected(self) -> None:
        """文件内容里的 target 必须与请求一致：防止把别的目标的值当成这个目标的。"""

        target = device_token_target("device-a")
        self.store.put(target, "secret-a")
        path = next(self.directory.glob("*.credential"))
        path.write_text(json.dumps({"target": device_token_target("device-b"), "secret": "secret-b"}), encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, CREDENTIAL_FILE_MODE)  # 覆盖写入会重置权限，POSIX 上要收回 0600 才走得到校验
        with self.assertRaisesRegex(CredentialInvalid, "credential_file_target_mismatch"):
            self.store.get(target)

    def test_missing_and_deleted_entries(self) -> None:
        target = device_token_target("device-missing")
        with self.assertRaisesRegex(CredentialNotFound, "credential_not_found"):
            self.store.get(target)
        self.store.put(target, "secret")
        self.assertTrue(self.store.delete(target))
        self.assertFalse(self.store.delete(target))

    def test_target_and_secret_validation_still_applies(self) -> None:
        with self.assertRaisesRegex(CredentialInvalid, "credential_target_invalid"):
            self.store.put("", "secret")
        with self.assertRaisesRegex(CredentialInvalid, "credential_secret_too_large"):
            self.store.put(device_token_target("device-big"), "x" * 513)

    @unittest.skipUnless(os.name == "nt", "POSIX-only guard")
    def test_windows_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(CredentialBackendUnavailable, "credential_file_backend_posix_only"):
            FileCredentialStore(self.directory)


class CredentialBackendSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name) / "credentials"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_factory_file_backend_needs_a_directory(self) -> None:
        with self.assertRaisesRegex(CredentialInvalid, "credential_file_directory_required"):
            create_credential_store(backend="file")
        store = create_credential_store(backend="file", directory=self.directory)
        self.assertIsInstance(store, FileCredentialStore)

    def test_factory_unknown_backend_is_refused(self) -> None:
        with self.assertRaisesRegex(CredentialBackendUnavailable, "credential_backend_unsupported"):
            create_credential_store(backend="keychain")

    @unittest.skipUnless(os.name == "nt", "auto picks the Credential Manager only on Windows")
    def test_auto_selects_credential_manager_on_windows(self) -> None:
        self.assertIsInstance(create_credential_store(backend="auto"), WindowsCredentialManager)

    @unittest.skipIf(os.name == "nt", "auto picks the file backend only on POSIX")
    def test_auto_selects_file_backend_on_posix(self) -> None:
        store = create_credential_store(backend="auto", directory=self.directory)
        self.assertIsInstance(store, FileCredentialStore)

    def test_agent_helper_keeps_windows_patch_point_and_uses_state_dir_for_files(self) -> None:
        """`agentd._credential_store`：Windows 名字保持可 patch；POSIX 走状态目录旁的 credentials/。"""

        from agentd import _credential_store

        windows_args = SimpleNamespace(credential_backend="windows", state_path=None)
        with patch("agentd.WindowsCredentialManager") as manager:
            _credential_store(windows_args)
            manager.assert_called_once_with()

        file_args = SimpleNamespace(credential_backend="file", state_path=str(Path(self.temp_dir.name) / "agentd.db"))
        store = _credential_store(file_args)
        self.assertIsInstance(store, FileCredentialStore)
        self.assertEqual(store.directory, Path(self.temp_dir.name) / "credentials")

    def test_agent_helper_defaults_to_the_platform_backend(self) -> None:
        from agentd import _credential_store

        args = SimpleNamespace(state_path=str(Path(self.temp_dir.name) / "agentd.db"))
        store = _credential_store(args)
        if os.name == "nt":
            self.assertIsInstance(store, WindowsCredentialManager)
        else:
            self.assertIsInstance(store, FileCredentialStore)


if __name__ == "__main__":
    unittest.main()

"""UX-4-01 `agentd keygen` 的契约测试。

关键断言不是"文件生成了"，而是：
- 私钥是**未加密 PKCS8 PEM**，能被 `device-register` 的加载方式（password=None）读出来；
- 公钥指纹与平台 `parse_public_key` 一致，且签名能被该公钥验证（密钥对真的可用于配对）；
- 已存在密钥时默认拒绝覆盖，`--force` 才替换。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from packages.device_identity import parse_public_key, sign_registration, verify_registration_signature

import agentd


def _run_keygen(directory: Path, name: str = "device", force: bool = False) -> dict:
    args = argparse.Namespace(directory=str(directory), name=name, force=force)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        agentd.keygen(args)
    return json.loads(buffer.getvalue())


class KeygenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name) / "keys"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_keygen_writes_paths_and_fingerprint(self) -> None:
        payload = _run_keygen(self.directory)

        private_path = Path(payload["private_key_path"])
        public_path = Path(payload["public_key_path"])
        fingerprint_path = Path(payload["fingerprint_path"])
        self.assertTrue(private_path.exists())
        self.assertTrue(public_path.exists())
        self.assertEqual(payload["algorithm"], "ed25519")
        self.assertEqual(private_path.parent, self.directory)

        # 指纹必须与平台解析出的指纹一致，并把同一个值落盘给接入脚本读。
        _, expected = parse_public_key(public_path.read_text(encoding="ascii"))
        self.assertEqual(payload["public_key_fingerprint"], expected)
        self.assertEqual(fingerprint_path.read_text(encoding="ascii").strip(), expected)
        self.assertEqual(len(expected), 64)

    def test_private_key_is_unencrypted_pkcs8_loadable_by_device_register(self) -> None:
        payload = _run_keygen(self.directory)
        private_path = Path(payload["private_key_path"])

        # device-register 的加载方式：PEM + password=None
        loaded = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        self.assertIsInstance(loaded, Ed25519PrivateKey)

        derived = loaded.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        _, derived_fingerprint = parse_public_key(derived)
        self.assertEqual(derived_fingerprint, payload["public_key_fingerprint"])

    def test_generated_pair_completes_registration_proof(self) -> None:
        """生成的密钥对必须真的能完成配对签名与验签（否则 keygen 只是产了两个文件）。"""

        payload = _run_keygen(self.directory)
        private_key = serialization.load_pem_private_key(
            Path(payload["private_key_path"]).read_bytes(), password=None
        )
        public_pem = Path(payload["public_key_path"]).read_text(encoding="ascii")
        fingerprint = payload["public_key_fingerprint"]

        signature = sign_registration(
            private_key,
            "11111111-2222-3333-4444-555555555555",
            "challenge-value",
            "agent-keygen",
            "device-keygen",
            fingerprint,
        )
        verified = verify_registration_signature(
            public_pem,
            signature,
            "11111111-2222-3333-4444-555555555555",
            "challenge-value",
            "agent-keygen",
            "device-keygen",
        )
        self.assertEqual(verified, fingerprint)

    def test_keygen_refuses_to_overwrite_without_force(self) -> None:
        first = _run_keygen(self.directory)
        with self.assertRaises(ValueError) as raised:
            _run_keygen(self.directory)
        self.assertIn("keygen_refused_existing_key", str(raised.exception))

        # 原密钥保持不变
        _, unchanged = parse_public_key(Path(first["public_key_path"]).read_text(encoding="ascii"))
        self.assertEqual(unchanged, first["public_key_fingerprint"])

    def test_keygen_force_replaces_pair(self) -> None:
        first = _run_keygen(self.directory)
        second = _run_keygen(self.directory, force=True)
        self.assertNotEqual(first["public_key_fingerprint"], second["public_key_fingerprint"])

    def test_custom_name_and_next_step_hint(self) -> None:
        payload = _run_keygen(self.directory, name="station-a")
        self.assertTrue(payload["private_key_path"].endswith("station-a.key"))
        self.assertTrue(payload["public_key_path"].endswith("station-a.pub"))
        # 输出里给出下一步命令，且把私钥路径填好（其余配对参数留给向导页）
        self.assertIn("device-register", payload["next_step"])
        self.assertIn("station-a.key", payload["next_step"])
        self.assertIn("--pairing-code", payload["next_step"])

    def test_keygen_is_registered_as_subcommand(self) -> None:
        """CLI 层要真的暴露 keygen（避免只加了函数没挂子命令）。"""

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        # 复用 agentd.main 的注册逻辑：直接跑一次 --help 的解析路径代价高，这里检查源码注册。
        source = Path(agentd.__file__).read_text(encoding="utf-8")
        self.assertIn('sub.add_parser("keygen"', source)
        self.assertIn("keygen_parser.set_defaults(func=keygen)", source)


if __name__ == "__main__":
    unittest.main()
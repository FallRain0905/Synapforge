"""Shared device registration proof-of-possession primitives."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


REGISTRATION_CONTEXT = b"math-agent-platform/device-pairing/v1"
ED25519_ALGORITHM = "ed25519"


class DeviceIdentityError(ValueError):
    """Stable protocol error for malformed keys, challenges, or signatures."""


def create_challenge() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_public_key(public_key: str) -> tuple[Ed25519PublicKey, str]:
    """Parse PEM/OpenSSH Ed25519 and fingerprint the canonical DER key."""

    try:
        encoded = public_key.strip().encode("utf-8")
    except UnicodeEncodeError as error:
        raise DeviceIdentityError("device_public_key_invalid") from error
    key: Any
    try:
        key = serialization.load_pem_public_key(encoded)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        try:
            key = serialization.load_ssh_public_key(encoded)
        except (ValueError, TypeError, UnsupportedAlgorithm) as error:
            raise DeviceIdentityError("device_public_key_invalid") from error
    if not isinstance(key, Ed25519PublicKey):
        raise DeviceIdentityError("device_public_key_algorithm_unsupported")
    canonical = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, hashlib.sha256(canonical).hexdigest()


def registration_message(
    pairing_id: UUID | str,
    challenge: str,
    agent_id: str,
    device_id: str,
    public_key_fingerprint: str,
) -> bytes:
    """Build a length-delimited, versioned registration message."""

    fields = (
        REGISTRATION_CONTEXT,
        str(pairing_id).encode("utf-8"),
        challenge.encode("utf-8"),
        agent_id.encode("utf-8"),
        device_id.encode("utf-8"),
        public_key_fingerprint.encode("ascii"),
    )
    return b"".join(len(field).to_bytes(4, "big") + field for field in fields)


def encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).decode("ascii")


def decode_signature(value: str) -> bytes:
    try:
        encoded = value.strip().encode("ascii")
    except UnicodeEncodeError as error:
        raise DeviceIdentityError("device_signature_invalid") from error
    if not encoded or len(encoded) > 2048:
        raise DeviceIdentityError("device_signature_invalid")
    try:
        decoded = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise DeviceIdentityError("device_signature_invalid") from error
    if len(decoded) != 64:
        raise DeviceIdentityError("device_signature_invalid")
    return decoded


def verify_registration_signature(
    public_key: str,
    signature: str,
    pairing_id: UUID | str,
    challenge: str,
    agent_id: str,
    device_id: str,
) -> str:
    """Verify private-key possession and return the canonical fingerprint."""

    key, fingerprint = parse_public_key(public_key)
    try:
        key.verify(
            decode_signature(signature),
            registration_message(pairing_id, challenge, agent_id, device_id, fingerprint),
        )
    except InvalidSignature as error:
        raise DeviceIdentityError("device_signature_invalid") from error
    return fingerprint


def sign_registration(
    private_key: Ed25519PrivateKey,
    pairing_id: UUID | str,
    challenge: str,
    agent_id: str,
    device_id: str,
    public_key_fingerprint: str,
) -> str:
    return encode_signature(
        private_key.sign(registration_message(pairing_id, challenge, agent_id, device_id, public_key_fingerprint))
    )


__all__ = [
    "DeviceIdentityError",
    "ED25519_ALGORITHM",
    "REGISTRATION_CONTEXT",
    "create_challenge",
    "decode_signature",
    "encode_signature",
    "hash_secret",
    "parse_public_key",
    "registration_message",
    "sign_registration",
    "verify_registration_signature",
]

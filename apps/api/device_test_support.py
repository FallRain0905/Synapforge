"""Test-only helpers for constructing a valid device registration proof."""

from __future__ import annotations

from typing import Any

from app.contracts import DeviceRegisterRequest
from app.device_identity import parse_public_key, sign_registration
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def registration_request(pairing: Any, private_key: Ed25519PrivateKey, agent_id: str, device_id: str, **overrides: Any) -> DeviceRegisterRequest:
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    _, fingerprint = parse_public_key(public_key)
    values = {
        "pairing_code": pairing.pairing_code,
        "pairing_id": pairing.id,
        "challenge": pairing.challenge,
        "challenge_signature": sign_registration(
            private_key,
            pairing.id,
            pairing.challenge or "",
            agent_id,
            device_id,
            fingerprint,
        ),
        "agent_id": agent_id,
        "device_id": device_id,
        "device_name": "Test workstation",
        "public_key": public_key,
        "platform": "windows",
        "agent_version": "0.1.0",
        "capabilities": [],
    }
    values.update(overrides)
    return DeviceRegisterRequest(**values)

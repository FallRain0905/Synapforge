"""API import boundary for the shared device identity contract."""

from packages.device_identity import (
    DeviceIdentityError,
    ED25519_ALGORITHM,
    REGISTRATION_CONTEXT,
    create_challenge,
    decode_signature,
    encode_signature,
    hash_secret,
    parse_public_key,
    registration_message,
    sign_registration,
    verify_registration_signature,
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

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any


DEFAULT_EMERGENCY_PASSWORD = "123456"
EMERGENCY_PASSWORD_HASH_FORMAT = "pbkdf2_sha256"
EMERGENCY_PASSWORD_HASH_ITERATIONS = 600_000
EMERGENCY_PASSWORD_SALT_BYTES = 16
EMERGENCY_PASSWORD_DIGEST_BYTES = 32


def create_emergency_password_record(password: str) -> dict[str, Any]:
    salt = os.urandom(EMERGENCY_PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        EMERGENCY_PASSWORD_HASH_ITERATIONS,
        dklen=EMERGENCY_PASSWORD_DIGEST_BYTES,
    )
    return {
        "format": EMERGENCY_PASSWORD_HASH_FORMAT,
        "iterations": EMERGENCY_PASSWORD_HASH_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "digest": base64.b64encode(digest).decode("ascii"),
    }


def is_emergency_password_record(value: Any) -> bool:
    return isinstance(value, dict) and value.get("format") == EMERGENCY_PASSWORD_HASH_FORMAT


def verify_emergency_password_record(candidate: str, value: Any) -> bool:
    if not is_emergency_password_record(value):
        return False
    try:
        iterations = int(value["iterations"])
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        encoded_salt = str(value["salt"])
        encoded_digest = str(value["digest"])
        if len(encoded_salt) > 128 or len(encoded_digest) > 128:
            return False
        salt = base64.b64decode(encoded_salt, validate=True)
        expected = base64.b64decode(encoded_digest, validate=True)
    except (KeyError, TypeError, ValueError):
        return False
    if len(salt) < 16 or len(expected) != EMERGENCY_PASSWORD_DIGEST_BYTES:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        candidate.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)

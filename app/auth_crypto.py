"""Purpose-separated credential primitives; no application authorization decisions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
import pyotp


_passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return _passwords.hash(password)


def verify_password(encoded: str, candidate: str) -> bool:
    try:
        return _passwords.verify(encoded, candidate)
    except (VerificationError, InvalidHashError):
        return False


def digest(secret_key: str, purpose: str, value: str) -> str:
    material = f"{len(purpose)}:{purpose}:{value}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _fernet(secret_key: str, encryption_key: str | None = None) -> Fernet:
    if encryption_key:
        return Fernet(encryption_key.encode("ascii"))
    key = hmac.new(secret_key.encode(), b"youth-service:totp-encryption:v1", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_totp_secret(secret: str, secret_key: str, encryption_key: str | None = None) -> str:
    # Validate the enrollment secret before storing it.
    base64.b32decode(secret.upper(), casefold=True)
    return _fernet(secret_key, encryption_key).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(ciphertext: str, secret_key: str, encryption_key: str | None = None) -> str:
    try:
        return _fernet(secret_key, encryption_key).decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise ValueError("MFA credential could not be decrypted") from exc


def totp_at(secret: str, step: int) -> str:
    """RFC 4226/6238 through PyOTP, 30-second time step and six digits."""
    return pyotp.TOTP(secret, digits=6, interval=30).at(step * 30)


def match_totp_step(secret: str, code: str, timestamp: float, last_step: int | None) -> int | None:
    if re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    current = int(timestamp // 30)
    for step in (current, current - 1, current + 1):
        if step < 0 or (last_step is not None and step <= last_step):
            continue
        if hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None

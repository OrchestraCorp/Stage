"""Established credential primitives with bounded password CPU/memory work."""

import asyncio
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from app.native_auth.errors import NativeCredentialError

# Each Argon2id operation uses 64 MiB; the process permits at most two concurrently.
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
_PASSWORD_SLOTS = asyncio.Semaphore(2)
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=1$c3RhZ2UtbmF0aXZlLWR1bW15$Wms70nWVCuzbZ2Wqp+QMDmzr/1xwweSjtLlTKXHtE+I"
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._@+-]{0,127}\Z", re.ASCII)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_secret() -> str:
    return secrets.token_urlsafe(32)


def normalize_username(value: str) -> str:
    # No silent trimming, case-folding, email linking or Unicode aliases.
    if not _USERNAME.fullmatch(value):
        raise NativeCredentialError("invalid_credentials")
    return value


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 256 or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in password):
        raise NativeCredentialError("invalid_password")


async def _password_work[T](operation: Callable[..., T], *args: Any) -> T:
    # Cancellation must not release the slot while the worker still owns memory.
    async with _PASSWORD_SLOTS:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
                # Multiple cancellation sources (request timeout, disconnect,
                # runtime drain) cannot release memory capacity prematurely.
                continue
        if cancelled:
            raise asyncio.CancelledError
        return result


async def hash_password(password: str) -> str:
    validate_password(password)
    result = await _password_work(_PASSWORD_HASHER.hash, password)
    assert isinstance(result, str)
    return result


def _verify(stored: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(stored: str | None, password: str) -> bool:
    if len(password) > 256 or any(0xD800 <= ord(char) <= 0xDFFF for char in password):
        return False
    return bool(await _password_work(_verify, stored or _DUMMY_HASH, password)) and stored is not None


class TotpCipher:
    def __init__(self, key: bytes | str) -> None:
        self._fernet = Fernet(key)

    def new_secret(self) -> str:
        return pyotp.random_base32()

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode("ascii")).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        try:
            return self._fernet.decrypt(encrypted.encode("ascii")).decode("ascii")
        except (InvalidToken, UnicodeError, ValueError):
            raise NativeCredentialError("authentication_unavailable") from None

    def uri(self, secret: str, username: str, issuer: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)

    def verified_step(self, secret: str, code: str, *, now: datetime, last_step: int) -> int | None:
        if not re.fullmatch(r"[0-9]{6}", code, flags=re.ASCII):
            return None
        current_step = int(now.timestamp()) // 30
        totp = pyotp.TOTP(secret)
        for step in (current_step, current_step - 1, current_step + 1):
            if step > last_step and hmac.compare_digest(totp.at(step * 30), code):
                return step
        return None


def recovery_codes() -> tuple[str, ...]:
    return tuple(secrets.token_hex(10) for _ in range(10))

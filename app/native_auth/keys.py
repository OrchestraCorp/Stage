"""Deployment-owned signing and encryption keys, never stored in the application database."""

import json
import os
import stat
from pathlib import Path
from typing import Any

import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class NativeKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeKeyError("native_key_duplicate_json_field")
        result[key] = value
    return result


def _private_bytes(path: str | Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        # Allow a root-owned file readable by the dedicated service group (0640).
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037 or info.st_size > 131072:
            raise NativeKeyError("native_key_file_must_be_private_regular_file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(131073)
        if len(value) > 131072:
            raise NativeKeyError("native_key_file_too_large")
        return value
    finally:
        os.close(descriptor)


class KeyRing:
    def __init__(self, active_kid: str, keys: list[dict[str, str]]) -> None:
        if (
            not isinstance(active_kid, str)
            or not 1 <= len(active_kid) <= 128
            or not isinstance(keys, list)
            or not 1 <= len(keys) <= 8
        ):
            raise NativeKeyError("native_signing_configuration_invalid")
        self.active_kid = active_kid
        self._public: dict[str, rsa.RSAPublicKey] = {}
        self._private: rsa.RSAPrivateKey | None = None
        for entry in keys:
            if not isinstance(entry, dict) or set(entry) not in ({"kid", "private_pem"}, {"kid", "public_pem"}):
                raise NativeKeyError("native_signing_key_entry_invalid")
            kid = entry.get("kid", "")
            if (
                not isinstance(kid, str)
                or not 1 <= len(kid) <= 128
                or any(ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in kid)
                or kid in self._public
                or any(not isinstance(value, str) for value in entry.values())
            ):
                raise NativeKeyError("native_signing_key_id_invalid")
            try:
                if "private_pem" in entry:
                    loaded = serialization.load_pem_private_key(entry["private_pem"].encode(), password=None)
                    if not isinstance(loaded, rsa.RSAPrivateKey) or kid != active_kid:
                        raise NativeKeyError("only_active_rsa_key_may_have_private_material")
                    self._private = loaded
                    key = loaded.public_key()
                else:
                    public = serialization.load_pem_public_key(entry["public_pem"].encode())
                    if not isinstance(public, rsa.RSAPublicKey):
                        raise NativeKeyError("rsa_public_key_required")
                    key = public
                if not 2048 <= key.key_size <= 8192:
                    raise NativeKeyError("native_rsa_key_size_invalid")
                self._public[kid] = key
            except (ValueError, TypeError, KeyError):
                raise NativeKeyError("native_signing_key_invalid") from None
        if active_kid not in self._public or self._private is None:
            raise NativeKeyError("native_active_private_key_missing")

    def sign(self, claims: dict[str, Any], typ: str = "at+jwt") -> str:
        if typ not in {"at+jwt", "JWT"} or self._private is None:
            raise NativeKeyError("unsupported_native_token_type")
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.active_kid, "typ": typ})

    def verify_access(self, token: str, issuer: str, audience: str | list[str]) -> dict[str, Any]:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise NativeKeyError("invalid_native_access_token")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or header.get("typ") != "at+jwt"
                or not isinstance(header.get("kid"), str)
                or header["kid"] not in self._public
                or any(key in header for key in ("jku", "x5u", "jwk", "crit"))
            ):
                raise NativeKeyError("invalid_native_token_header")
            return jwt.decode(
                token,
                self._public[header["kid"]],
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer,
                options={
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "exp",
                        "iat",
                        "sid",
                        "client_id",
                        "jti",
                        "auth_time",
                        "acr",
                        "amr",
                        "credential_version",
                    ]
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
            raise NativeKeyError("invalid_native_access_token") from None

    def verify_id_token_hint(self, token: str, issuer: str, client_id: str | None = None) -> dict[str, Any]:
        """Validate an RP logout hint, including an expired token, never authentication.

        A caller must bind the returned subject/session to current database facts
        and require browser logout confirmation before mutating a login session.
        """
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise NativeKeyError("invalid_native_id_token_hint")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or header.get("typ") != "JWT"
                or not isinstance(header.get("kid"), str)
                or header["kid"] not in self._public
                or any(key in header for key in ("jku", "x5u", "jwk", "crit"))
            ):
                raise NativeKeyError("invalid_native_token_header")
            claims = jwt.decode(
                token,
                self._public[header["kid"]],
                algorithms=["RS256"],
                issuer=issuer,
                audience=client_id,
                options={
                    "require": ["iss", "sub", "aud", "azp", "exp", "iat", "sid", "auth_time", "acr", "amr"],
                    "verify_exp": False,
                    "verify_aud": client_id is not None,
                },
            )
            for name in ("sub", "aud", "azp", "sid"):
                value = claims[name]
                if (
                    not isinstance(value, str)
                    or not 1 <= len(value.encode("utf-8")) <= 128
                    or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                ):
                    raise NativeKeyError("invalid_native_id_token_hint")
            if (
                claims["aud"] != claims["azp"]
                or (client_id is not None and claims["aud"] != client_id)
                or any(type(claims[name]) is not int for name in ("exp", "iat", "auth_time"))
                or not 0 <= claims["auth_time"] <= claims["iat"] < claims["exp"]
                or claims["acr"] != "urn:orchestra:acr:pwd-totp"
                or claims["amr"] != ["pwd", "otp"]
            ):
                raise NativeKeyError("invalid_native_id_token_hint")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError, UnicodeError):
            raise NativeKeyError("invalid_native_id_token_hint") from None

    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key)), "kid": kid, "use": "sig", "alg": "RS256"}
                for kid, key in self._public.items()
            ]
        }


def load_keyring(path: str | Path, environment: str = "dev") -> KeyRing:
    try:
        value = json.loads(_private_bytes(path), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"active_kid", "keys"}:
            raise NativeKeyError("native_signing_configuration_invalid")
        return KeyRing(value["active_kid"], value["keys"])
    except (OSError, ValueError, TypeError, KeyError):
        raise NativeKeyError("native_signing_configuration_unavailable") from None


def read_encryption_key(path: str | Path, environment: str = "dev") -> bytes:
    try:
        key = _private_bytes(path).strip()
        Fernet(key)
        return key
    except (OSError, ValueError, TypeError):
        raise NativeKeyError("native_encryption_key_unavailable") from None

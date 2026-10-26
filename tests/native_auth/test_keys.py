"""Real RSA rotation and deployment-file failure boundaries for the native issuer."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.native_auth.config import NativeAuthConfig
from app.native_auth.keys import KeyRing, NativeKeyError, load_keyring, read_encryption_key

ISSUER = "https://login.example.test"


@pytest.fixture(scope="module")
def signing_entries() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def private(key: rsa.RSAPrivateKey) -> str:
        return key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()

    return (
        {"kid": "old", "private_pem": private(old_key)},
        {
            "kid": "old",
            "public_pem": old_key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode(),
        },
        {"kid": "new", "private_pem": private(new_key)},
    )


def claims() -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "iss": ISSUER,
        "sub": "account:key-test",
        "aud": "stage-api",
        "iat": now,
        "exp": now + 300,
        "auth_time": now,
        "sid": "session:key-test",
        "client_id": "maestro-native",
        "jti": "jwt:key-test",
        "acr": "urn:orchestra:acr:pwd-totp",
        "amr": ["pwd", "otp"],
        "credential_version": 1,
        "scope": "openid",
    }


def test_rotation_keeps_old_public_verification_and_signs_only_with_new_private_key(
    signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]],
) -> None:
    old_private, old_public, new_private = signing_entries
    first = KeyRing("old", [old_private])
    old_token = first.sign(claims())
    rotated = KeyRing("new", [old_public, new_private])
    assert rotated.verify_access(old_token, ISSUER, "stage-api")["sub"] == "account:key-test"
    new_token = rotated.sign(claims())
    assert jwt.get_unverified_header(new_token)["kid"] == "new"
    assert rotated.verify_access(new_token, ISSUER, "stage-api")["sub"] == "account:key-test"
    assert {entry["kid"] for entry in rotated.jwks()["keys"]} == {"old", "new"}
    for entry in rotated.jwks()["keys"]:
        assert not {"d", "p", "q", "dp", "dq", "qi", "private_pem"}.intersection(entry)
    removed = KeyRing("new", [new_private])
    with pytest.raises(NativeKeyError):
        removed.verify_access(old_token, ISSUER, "stage-api")
    assert removed.verify_access(new_token, ISSUER, "stage-api")["sub"] == "account:key-test"


@pytest.mark.parametrize(
    "header",
    [
        {"typ": "JWT"},
        {"kid": "unknown"},
        {"jku": "https://attacker.test/keys"},
        {"x5u": "https://attacker.test/key"},
        {"jwk": {}},
        {"crit": ["custom"]},
    ],
)
def test_access_verification_rejects_token_types_and_remote_key_selection(
    signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]], header: dict[str, Any]
) -> None:
    private = signing_entries[0]
    keyring = KeyRing("old", [private])
    token = jwt.encode(
        claims(), private["private_pem"], algorithm="RS256", headers={"typ": "at+jwt", "kid": "old", **header}
    )
    with pytest.raises(NativeKeyError):
        keyring.verify_access(token, ISSUER, "stage-api")


@pytest.mark.parametrize("change", [{"iss": "https://other.test"}, {"aud": "another-api"}, {"exp": 1}, {"iat": 2**40}])
def test_access_verification_enforces_issuer_audience_and_lifetime(
    signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]], change: dict[str, Any]
) -> None:
    keyring = KeyRing("old", [signing_entries[0]])
    with pytest.raises(NativeKeyError):
        keyring.verify_access(keyring.sign({**claims(), **change}), ISSUER, "stage-api")


def test_duplicate_keys_inactive_private_material_and_missing_active_key_fail_closed(
    signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]],
) -> None:
    old_private, old_public, new_private = signing_entries
    for active, entries in (
        ("old", [old_private, old_private]),
        ("new", [old_private, new_private]),
        ("old", [old_public]),
        ("missing", [old_private]),
    ):
        with pytest.raises(NativeKeyError):
            KeyRing(active, entries)


def test_weak_rsa_and_non_rsa_signing_keys_are_rejected() -> None:
    for key in (
        rsa.generate_private_key(public_exponent=65537, key_size=1024),
        ec.generate_private_key(ec.SECP256R1()),
    ):
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        with pytest.raises(NativeKeyError):
            KeyRing("bad", [{"kid": "bad", "private_pem": pem}])


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o400])
def test_private_signing_and_encryption_files_can_be_read(
    tmp_path: Path, signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]], mode: int
) -> None:
    signing = tmp_path / "signing.json"
    signing.write_text(json.dumps({"active_kid": "old", "keys": [signing_entries[0]]}))
    signing.chmod(mode)
    assert load_keyring(signing, "prod").active_kid == "old"
    encrypted = tmp_path / "totp.key"
    key = Fernet.generate_key()
    encrypted.write_bytes(key)
    encrypted.chmod(mode)
    assert read_encryption_key(encrypted, "prod") == key


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666, 0o750])
def test_public_or_writable_key_files_are_rejected(tmp_path: Path, mode: int) -> None:
    key = tmp_path / "private-secret.key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(mode)
    with pytest.raises(NativeKeyError, match="native_encryption_key_unavailable"):
        read_encryption_key(key)
    with pytest.raises(NativeKeyError, match="native_signing_configuration_unavailable"):
        load_keyring(key)


@pytest.mark.parametrize(
    "contents", ["[]", "{}", '{"active_kid":"x","keys":[]}', '{"active_kid":"x","keys":["bad"]}', "not-json"]
)
def test_malformed_key_files_have_stable_errors_without_contents(tmp_path: Path, contents: str) -> None:
    key = tmp_path / "synthetic-private-path"
    key.write_text(contents)
    key.chmod(0o600)
    with pytest.raises(NativeKeyError) as error:
        load_keyring(key)
    assert "synthetic-private-path" not in str(error.value) and contents not in str(error.value)


def test_missing_directory_oversized_and_invalid_encryption_keys_fail_closed(tmp_path: Path) -> None:
    huge = tmp_path / "huge.key"
    huge.write_bytes(b"x" * 131073)
    huge.chmod(0o600)
    invalid = tmp_path / "invalid.key"
    invalid.write_text("synthetic-not-fernet")
    invalid.chmod(0o600)
    for path in (tmp_path / "missing", tmp_path, huge, invalid):
        with pytest.raises(NativeKeyError):
            read_encryption_key(path)
        with pytest.raises(NativeKeyError):
            load_keyring(path)


def test_symlinks_and_named_pipes_cannot_supply_private_material(tmp_path: Path) -> None:
    actual = tmp_path / "actual.key"
    actual.write_bytes(Fernet.generate_key())
    actual.chmod(0o600)
    linked = tmp_path / "linked.key"
    linked.symlink_to(actual)
    pipe = tmp_path / "pipe.key"
    os.mkfifo(pipe, mode=0o600)
    for path in (linked, pipe):
        with pytest.raises(NativeKeyError):
            read_encryption_key(path)


def test_duplicate_json_fields_cannot_select_a_different_active_key(
    tmp_path: Path, signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]]
) -> None:
    key = tmp_path / "duplicate.json"
    key.write_text('{"active_kid":"unexpected","active_kid":"old","keys":' + json.dumps([signing_entries[0]]) + "}")
    key.chmod(0o600)
    with pytest.raises(NativeKeyError):
        load_keyring(key)


@pytest.mark.parametrize(
    "change", [{"kid": "invalid\x00kid"}, {"kid": "kid with space"}, {"public_pem": "ambiguous"}, {"unused": "ignored"}]
)
def test_signing_entries_reject_ambiguous_metadata(
    signing_entries: tuple[dict[str, str], dict[str, str], dict[str, str]], change: dict[str, str]
) -> None:
    with pytest.raises(NativeKeyError):
        KeyRing("old", [{**signing_entries[0], **change}])


@pytest.mark.parametrize(
    "issuer",
    [
        "http://login.example.test",
        "https://user:password@login.example.test",
        "https://login.example.test/path",
        "https://login.example.test?issuer=other",
        "https://login.example.test#fragment",
        "https://login.example.test:99999",
        "https://login.example.test:bad",
        "https://login.example.test\\evil",
        "https://login.example.test\n",
    ],
)
def test_issuer_rejects_unsafe_or_ambiguous_origins(issuer: str) -> None:
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer=issuer, organization_ref="org:test", environment="prod")


def test_http_issuer_is_only_allowed_for_explicit_local_development() -> None:
    for environment in ("dev", "test"):
        for issuer in ("http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080"):
            assert (
                NativeAuthConfig(issuer=issuer, organization_ref="org:test", environment=environment).issuer == issuer
            )
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer="http://127.0.0.1:8080", organization_ref="org:test", environment="prod")


@pytest.mark.parametrize("reference", ["", "org:with space", "org:bad\x00", "x" * 129])
def test_native_authority_references_are_bounded_and_control_free(reference: str) -> None:
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer=ISSUER, organization_ref=reference)
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer=ISSUER, organization_ref="org:valid", trust_ref=reference)

"""Explicit local development/test native-auth installation and credential recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from pydantic import Field, SecretStr  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.control.contracts import AdministrationError, OperationUnknown, Reference, StrictPlan  # noqa: E402
from app.control.operations import check_database_environment, operation_result  # noqa: E402
from app.models.native_auth import NativeAccountTable, NativeAuthEventTable  # noqa: E402
from app.native_auth.administration import INSTALL_OPERATION, TRUST, InstallPlan, install  # noqa: E402
from app.native_auth.credentials import NativeCredentials  # noqa: E402
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError  # noqa: E402
from app.native_auth.keys import KeyRing, read_encryption_key  # noqa: E402
from scripts.identity_admin import local_operator_ref  # noqa: E402


class RecoveryPlan(StrictPlan):
    account_ref: Reference
    expected_version: int = Field(ge=1)
    new_password: SecretStr | None = Field(default=None, repr=False)
    reset_totp: bool = False


def read_private_plan(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_size > 65536
            or info.st_uid != os.geteuid()
        ):
            raise AdministrationError("plan_requires_private_owned_regular_file")
        return os.read(descriptor, 65537)
    finally:
        os.close(descriptor)


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _new_signing_entry() -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return {
        "kid": "native-" + uuid4().hex,
        "private_pem": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
    }


def generate_keys(key_file: Path, encryption_key_file: Path) -> dict[str, Any]:
    if key_file.resolve() == encryption_key_file.resolve() or key_file.exists() or encryption_key_file.exists():
        raise AdministrationError("key_output_must_be_new_distinct_files")
    entry = _new_signing_entry()
    _write_private(key_file, json.dumps({"active_kid": entry["kid"], "keys": [entry]}, indent=2).encode() + b"\n")
    try:
        _write_private(encryption_key_file, Fernet.generate_key() + b"\n")
    except BaseException:
        key_file.unlink(missing_ok=True)
        raise
    return {"key_file": str(key_file), "encryption_key_file": str(encryption_key_file), "active_kid": entry["kid"]}


def prepare_key_rotation(key_file: Path, output: Path) -> dict[str, Any]:
    """Write a new candidate ring; activating it is an explicit deployment change."""
    raw = json.loads(read_private_plan(key_file))
    previous = KeyRing(raw["active_kid"], raw["keys"])
    if len(raw["keys"]) >= 8:
        raise AdministrationError("explicit_key_retirement_required")
    entry = _new_signing_entry()
    public_entries = []
    for key in raw["keys"]:
        if "private_pem" in key:
            private = serialization.load_pem_private_key(key["private_pem"].encode(), password=None)
            public = private.public_key()
            public_entries.append(
                {
                    "kid": key["kid"],
                    "public_pem": public.public_bytes(
                        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                    ).decode(),
                }
            )
        else:
            public_entries.append({"kid": key["kid"], "public_pem": key["public_pem"]})
    candidate = {"active_kid": entry["kid"], "keys": [entry, *public_entries]}
    KeyRing(candidate["active_kid"], candidate["keys"])  # type: ignore[arg-type]
    _write_private(output, json.dumps(candidate, indent=2).encode() + b"\n")
    return {
        "candidate_key_file": str(output),
        "active_kid": entry["kid"],
        "previous_active_kid": previous.active_kid,
        "activation_pending": True,
        "retained_public_keys": len(public_entries),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "generate-keys":
        return generate_keys(args.key_file, args.encryption_key_file)
    if args.command == "prepare-key-rotation":
        return prepare_key_rotation(args.key_file, args.output)
    from app.core.config import Settings
    from app.core.db import create_engine, create_session_factory

    settings = Settings()
    if settings.env not in {"dev", "test"} or settings.env != args.environment:
        raise AdministrationError("development_environment_required")
    if not os.environ.get("STAGE_DATABASE_URL"):
        raise AdministrationError("explicit_database_required")
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    actor = local_operator_ref()
    try:
        if args.command == "result":
            return (
                await operation_result(
                    sessions,
                    environment=args.environment,
                    actor_ref=actor,
                    operation_type=INSTALL_OPERATION,
                    idempotency_key=args.idempotency_key,
                )
            ).model_dump(mode="json")
        async with sessions() as session:
            await check_database_environment(session, args.environment)
        if args.command == "inspect":
            async with sessions() as session:
                account = await session.get(NativeAccountTable, args.account_ref)
                if account is None:
                    raise AdministrationError("account_not_found")
                events = (
                    await session.scalars(
                        select(NativeAuthEventTable)
                        .where(NativeAuthEventTable.account_ref == args.account_ref)
                        .order_by(NativeAuthEventTable.id.desc())
                        .limit(10)
                    )
                ).all()
                return {
                    "account_ref": account.account_ref,
                    "principal_ref": account.principal_ref,
                    "credential_version": account.credential_version,
                    "status": account.status,
                    "totp_enrolled": account.totp_secret is not None,
                    "totp_bootstrap_allowed": account.totp_bootstrap_allowed,
                    "recent_events": [{"event": event.event, "at": event.created_at.isoformat()} for event in events],
                }
        key_path = args.encryption_key_file or settings.native_auth_encryption_key_file
        if not key_path:
            raise AdministrationError("explicit_encryption_key_required")
        encryption_key = read_encryption_key(key_path, settings.env)
        if args.command == "install":
            plan = InstallPlan.model_validate_json(read_private_plan(args.plan))
            if plan.environment != settings.env:
                raise AdministrationError("environment_mismatch")
            if (settings.native_auth_issuer is not None and settings.native_auth_issuer != plan.issuer) or (
                settings.native_auth_organization_ref is not None
                and settings.native_auth_organization_ref != plan.organization_ref
            ):
                raise AdministrationError("native_deployment_configuration_mismatch")
            return (
                await install(
                    sessions,
                    plan,
                    actor_ref=actor,
                    idempotency_key=args.idempotency_key,
                    credential_digest_key=encryption_key,
                )
            ).model_dump(mode="json")
        organization_ref = args.organization_ref or settings.native_auth_organization_ref
        if not organization_ref:
            raise AdministrationError("explicit_organization_required")
        credentials = NativeCredentials(
            sessions, fernet_key=encryption_key, organization_ref=organization_ref, trust_ref=TRUST
        )
        if args.command == "reset":
            recovery = RecoveryPlan.model_validate_json(read_private_plan(args.plan))
            output_path = getattr(args, "enrollment_output", None)
            if recovery.reset_totp != (output_path is not None):
                raise AdministrationError("factor_reset_requires_private_enrollment_output")
            descriptor = None
            if output_path is not None:
                # Reserve the private destination before changing credentials.
                descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                result = await credentials.admin_reset(
                    recovery.account_ref,
                    recovery.expected_version,
                    new_password=recovery.new_password.get_secret_value()
                    if recovery.new_password is not None
                    else None,
                    reset_totp=recovery.reset_totp,
                    actor_ref=actor,
                )
                if descriptor is not None:
                    material = json.dumps(
                        {"account_ref": recovery.account_ref, "enrollment_code": result.enrollment_code}
                    ).encode()
                    try:
                        with os.fdopen(descriptor, "wb", closefd=False) as output:
                            output.write(material)
                            output.flush()
                            os.fsync(descriptor)
                    except OSError:
                        # The DB change is committed but its single-use proof
                        # may be lost. Inspect the version, then explicitly reset.
                        raise NativeCommitUnknown() from None
                response = {"account_ref": recovery.account_ref, "credential_version": result.credential_version}
                if output_path is not None:
                    response["enrollment_output"] = str(output_path)
                return response
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if args.command == "disable":
            version = await credentials.disable_account(args.account_ref, args.expected_version, actor_ref=actor)
            return {"account_ref": args.account_ref, "credential_version": version, "status": "disabled"}
        raise AdministrationError("unsupported_operation")
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test"), required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    keys = subparsers.add_parser(
        "generate-keys", help="create new private RSA and TOTP encryption files; never overwrite"
    )
    keys.add_argument("--key-file", type=Path, required=True)
    keys.add_argument("--encryption-key-file", type=Path, required=True)
    rotation = subparsers.add_parser(
        "prepare-key-rotation", help="prepare a new signing ring retaining old public keys"
    )
    rotation.add_argument("--key-file", type=Path, required=True)
    rotation.add_argument("--output", type=Path, required=True)
    installation = subparsers.add_parser(
        "install", help="explicitly bind a native account to an existing human membership"
    )
    installation.add_argument("--plan", type=Path, required=True)
    installation.add_argument("--idempotency-key", required=True)
    installation.add_argument("--encryption-key-file", type=Path)
    recovery = subparsers.add_parser("reset", help="operator recovery; inspect state after an unknown outcome")
    recovery.add_argument("--plan", type=Path, required=True)
    recovery.add_argument("--organization-ref")
    recovery.add_argument("--encryption-key-file", type=Path)
    recovery.add_argument("--enrollment-output", type=Path, help="new private output file required for factor reset")
    disable = subparsers.add_parser("disable", help="disable the explicit account and revoke its sessions")
    disable.add_argument("--account-ref", required=True)
    disable.add_argument("--expected-version", type=int, required=True)
    disable.add_argument("--organization-ref")
    disable.add_argument("--encryption-key-file", type=Path)
    inspect = subparsers.add_parser("inspect", help="read safe account status and recent authentication events")
    inspect.add_argument("--account-ref", required=True)
    result = subparsers.add_parser("result", help="recover a prior install by original operator and idempotency key")
    result.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        result_value = asyncio.run(run(args))
    except OperationUnknown as error:
        print(
            json.dumps(
                {
                    "error": "result_unknown",
                    "operation_type": error.operation_type,
                    "idempotency_key": error.idempotency_key,
                    "recovery": "query result using the original operator and key",
                }
            ),
            file=sys.stderr,
        )
        return 2
    except NativeCommitUnknown:
        print(
            json.dumps({"error": "result_unknown", "recovery": "inspect account version and events before retry"}),
            file=sys.stderr,
        )
        return 2
    except (AdministrationError, NativeCredentialError) as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception as error:
        # Validation errors may embed the plan. Never print their repr or input.
        print(json.dumps({"error": "native_administration_failed", "type": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result_value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

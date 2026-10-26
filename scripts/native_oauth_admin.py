"""Controlled OAuth client administration; secrets are written only to private files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.control.contracts import AdministrationError, OperationUnknown  # noqa: E402
from app.control.operations import check_database_environment, operation_result  # noqa: E402
from app.identity.service import content_digest  # noqa: E402
from app.models.native_auth import NativeClientTable  # noqa: E402
from app.native_auth.client_administration import (  # noqa: E402
    CONFIGURE_OPERATION,
    OPERATIONS,
    ROTATE_OPERATION,
    AdmitClientPlan,
    ClientPlan,
    ConfigureClientPlan,
    RotateClientSecretPlan,
    admit_client,
    client_snapshot,
    configure_client,
    rotate_client_secret,
)
from app.native_auth.crypto import digest_secret, random_secret  # noqa: E402
from scripts.identity_admin import local_operator_ref  # noqa: E402
from scripts.native_auth_admin import _write_private, read_private_plan  # noqa: E402


def _client_secret(args: argparse.Namespace, plan: ClientPlan, operation_type: str, *, required: bool) -> str | None:
    """Persist fresh entropy before DB writes; retry explicitly with the same private file.

    A DB commit can succeed after the network response is lost. Preparing the
    private delivery first leaves the secret available in that case and keeps
    operation_result completely free of reusable credentials.
    """
    output = getattr(args, "secret_output", None)
    input_path = getattr(args, "secret_input", None)
    if not required:
        if output is not None or input_path is not None:
            raise AdministrationError("client_secret_not_expected")
        return None
    if (output is None) == (input_path is None):
        raise AdministrationError("one_private_secret_destination_or_retry_input_required")
    binding = {
        "client_id": plan.client_id,
        "operation_type": operation_type,
        "idempotency_key": args.idempotency_key,
        "configuration_digest": content_digest(plan.model_dump(mode="json")),
    }
    if input_path is not None:
        raw = json.loads(read_private_plan(input_path))
        if (
            not isinstance(raw, dict)
            or set(raw) != {*binding, "client_secret"}
            or any(raw[key] != value for key, value in binding.items())
            or not isinstance(raw["client_secret"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", raw["client_secret"], flags=re.ASCII) is None
        ):
            raise AdministrationError("client_secret_retry_identity_mismatch")
        return raw["client_secret"]
    assert output is not None
    secret = random_secret()
    _write_private(output, json.dumps({**binding, "client_secret": secret}, indent=2).encode() + b"\n")
    # A durable file also needs its newly created directory entry persisted.
    parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return secret


async def run(args: argparse.Namespace) -> dict[str, Any]:
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
                    operation_type=args.operation_type,
                    idempotency_key=args.idempotency_key,
                )
            ).model_dump(mode="json")
        async with sessions() as session:
            await check_database_environment(session, args.environment)
        if args.command == "inspect":
            async with sessions() as session:
                client = await session.get(NativeClientTable, args.client_id)
                if client is None:
                    raise AdministrationError("client_not_found")
                return client_snapshot(client)
        raw_plan = read_private_plan(args.plan)
        plan: ConfigureClientPlan | RotateClientSecretPlan | AdmitClientPlan
        if args.command == "configure":
            plan = ConfigureClientPlan.model_validate_json(raw_plan)
        elif args.command == "rotate-secret":
            plan = RotateClientSecretPlan.model_validate_json(raw_plan)
        elif args.command == "admit":
            plan = AdmitClientPlan.model_validate_json(raw_plan)
        else:
            raise AdministrationError("unsupported_operation")
        if plan.environment != settings.env:
            raise AdministrationError("environment_mismatch")
        if (
            settings.native_auth_organization_ref is not None
            and settings.native_auth_organization_ref != plan.organization_ref
        ):
            raise AdministrationError("native_deployment_configuration_mismatch")
        if isinstance(plan, ConfigureClientPlan):
            secret = _client_secret(
                args,
                plan,
                CONFIGURE_OPERATION,
                required=plan.expected_version == 0 and plan.token_endpoint_auth_method == "client_secret_basic",
            )
            result = await configure_client(
                sessions,
                plan,
                actor_ref=actor,
                idempotency_key=args.idempotency_key,
                client_secret_hash=digest_secret(secret) if secret is not None else None,
            )
        elif isinstance(plan, RotateClientSecretPlan):
            secret = _client_secret(args, plan, ROTATE_OPERATION, required=True)
            assert secret is not None
            result = await rotate_client_secret(
                sessions,
                plan,
                actor_ref=actor,
                idempotency_key=args.idempotency_key,
                client_secret_hash=digest_secret(secret),
            )
        else:
            result = await admit_client(sessions, plan, actor_ref=actor, idempotency_key=args.idempotency_key)
        safe_result = result.model_dump(mode="json")
        if getattr(args, "secret_output", None) is not None:
            safe_result["secret_output"] = str(args.secret_output)
        return safe_result
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("configure", "create (version 0), update, disable or enable registered client metadata"),
        ("rotate-secret", "replace a confidential client secret and revoke its existing sessions"),
        ("admit", "explicitly create a separate Stage profile and preserve current MFA policy"),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("--plan", type=Path, required=True)
        subparser.add_argument("--idempotency-key", required=True)
        if command != "admit":
            delivery = subparser.add_mutually_exclusive_group()
            delivery.add_argument("--secret-output", type=Path, help="new private destination for a generated secret")
            delivery.add_argument(
                "--secret-input", type=Path, help="reuse prior private material when retrying the same operation"
            )
    inspect = commands.add_parser("inspect", help="read safe registered client metadata")
    inspect.add_argument("--client-id", required=True)
    result = commands.add_parser("result", help="recover an operation with its original operator and key")
    result.add_argument("--operation-type", choices=OPERATIONS, required=True)
    result.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        value = asyncio.run(run(args))
    except OperationUnknown as error:
        print(
            json.dumps(
                {
                    "error": "result_unknown",
                    "operation_type": error.operation_type,
                    "idempotency_key": error.idempotency_key,
                    "recovery": (
                        "keep private secret file; query result using original operator and key; "
                        "retry using --secret-input"
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 2
    except AdministrationError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception as error:
        # Pydantic/SQL errors can contain input. Do not print exception text.
        print(json.dumps({"error": "oauth_administration_failed", "type": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

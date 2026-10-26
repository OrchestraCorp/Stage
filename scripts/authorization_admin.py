"""Offline development/test human authorization installation and revocation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.authz.administration import (  # noqa: E402
    AuthorizationRevocationRequest,
    InstallAuthorizationPlan,
    install_authorization,
    revoke_authorization,
)
from app.control.contracts import AdministrationError, AdministrationResult, OperationUnknown  # noqa: E402
from app.control.operations import operation_result  # noqa: E402
from scripts.identity_admin import local_operator_ref  # noqa: E402


async def run(args: argparse.Namespace) -> AdministrationResult:
    from app.core.config import Settings
    from app.core.db import create_engine, create_session_factory

    config = Settings()
    if config.env not in {"dev", "test"} or config.env != args.environment:
        raise AdministrationError("development_environment_required")
    if not os.environ.get("STAGE_DATABASE_URL"):
        raise AdministrationError("explicit_database_required")
    engine = create_engine(config.database_url)
    sessions = create_session_factory(engine)
    try:
        actor = local_operator_ref()
        if args.command == "install":
            raw = args.plan.read_bytes()
            if len(raw) > 262144:
                raise AdministrationError("authorization_plan_too_large")
            plan = InstallAuthorizationPlan.model_validate_json(raw)
            if plan.environment != config.env:
                raise AdministrationError("environment_mismatch")
            return await install_authorization(sessions, plan, actor_ref=actor, idempotency_key=args.idempotency_key)
        if args.command == "revoke":
            request = AuthorizationRevocationRequest(
                environment=args.environment,
                organization_ref=args.organization_ref,
                kind=args.kind,
                target_ref=args.target_ref,
                expected_version=args.expected_version,
                role_revision=args.role_revision,
            )
            return await revoke_authorization(sessions, request, actor_ref=actor, idempotency_key=args.idempotency_key)
        return await operation_result(
            sessions,
            environment=args.environment,
            actor_ref=actor,
            operation_type=args.operation_type,
            idempotency_key=args.idempotency_key,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="append a bounded role version, human assignments and direct ACLs")
    install.add_argument("--plan", type=Path, required=True)
    install.add_argument("--idempotency-key", required=True)
    revoke = commands.add_parser("revoke", help="terminally revoke an existing authorization fact")
    revoke.add_argument("--organization-ref", required=True)
    revoke.add_argument(
        "--kind", choices=("assignment", "relation", "role_version", "scope", "resource"), required=True
    )
    revoke.add_argument("--target-ref", required=True)
    revoke.add_argument("--expected-version", type=int, required=True)
    revoke.add_argument("--role-revision", type=int)
    revoke.add_argument("--idempotency-key", required=True)
    result = commands.add_parser("result", help="recover an operation using the same local operator and key")
    result.add_argument(
        "--operation-type",
        choices=(
            "install.authorization",
            "revoke.authorization.assignment",
            "revoke.authorization.relation",
            "revoke.authorization.role_version",
            "revoke.authorization.scope",
            "revoke.authorization.resource",
        ),
        required=True,
    )
    result.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        response = asyncio.run(run(args))
    except OperationUnknown as error:
        print(
            json.dumps(
                {
                    "error": "result_unknown",
                    "operation_type": error.operation_type,
                    "idempotency_key": error.idempotency_key,
                    "recovery": "query the original operation; retry only with the same key and input",
                }
            ),
            file=sys.stderr,
        )
        return 2
    except AdministrationError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception as error:
        print(
            json.dumps({"error": "authorization_administration_failed", "type": type(error).__name__}),
            file=sys.stderr,
        )
        return 1
    print(response.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

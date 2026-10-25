"""Offline development/test identity bootstrap and revocation; no HTTP admin endpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.identity.administration import (  # noqa: E402
    AdministrationResult,
    BootstrapPlan,
    IdentityAdministrationError,
    IdentityOperationUnknown,
    RevocationRequest,
    bootstrap,
    operation_result,
    revoke,
)


def local_operator_ref() -> str:
    host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]
    return f"local-os:{host}:uid:{os.geteuid()}"


async def run(args: argparse.Namespace) -> AdministrationResult:
    from app.core.config import Settings
    from app.core.db import create_engine, create_session_factory

    config = Settings()
    if config.env not in {"dev", "test"} or config.env != args.environment:
        raise IdentityAdministrationError("development_environment_required")
    if not os.environ.get("STAGE_DATABASE_URL"):
        raise IdentityAdministrationError("explicit_database_required")
    engine = create_engine(config.database_url)
    sessions = create_session_factory(engine)
    actor = local_operator_ref()
    try:
        if args.command == "bootstrap":
            raw = args.plan.read_bytes()
            if len(raw) > 262144:
                raise IdentityAdministrationError("bootstrap_plan_too_large")
            plan = BootstrapPlan.model_validate_json(raw)
            if plan.environment != config.env:
                raise IdentityAdministrationError("environment_mismatch")
            return await bootstrap(sessions, plan, actor_ref=actor, idempotency_key=args.idempotency_key)
        if args.command == "revoke":
            request = RevocationRequest.model_validate(
                {
                    "environment": args.environment,
                    "kind": args.kind,
                    "target_ref": args.target_ref,
                    "expected_version": args.expected_version,
                    "profile_version": args.profile_version,
                    **({"organization_ref": args.organization_ref} if args.organization_ref is not None else {}),
                    **({"tenant_ref": args.tenant_ref} if args.tenant_ref is not None else {}),
                }
            )
            return await revoke(sessions, request, actor_ref=actor, idempotency_key=args.idempotency_key)
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
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser("bootstrap", help="install explicitly mapped development identities once")
    bootstrap_parser.add_argument("--plan", type=Path, required=True)
    bootstrap_parser.add_argument("--idempotency-key", required=True)
    revoke_parser = subparsers.add_parser("revoke", help="terminally revoke an identity fact")
    revoke_parser.add_argument(
        "--kind", choices=("principal", "organization", "tenant", "membership", "profile"), required=True
    )
    revoke_parser.add_argument("--target-ref", required=True)
    revoke_parser.add_argument("--expected-version", type=int, required=True)
    revoke_parser.add_argument("--organization-ref")
    # Historical spelling; conflicting aliases are rejected by RevocationRequest.
    revoke_parser.add_argument("--tenant-ref")
    revoke_parser.add_argument("--profile-version", type=int)
    revoke_parser.add_argument("--idempotency-key", required=True)
    result_parser = subparsers.add_parser("result", help="recover a prior operation using its original identity")
    result_parser.add_argument(
        "--operation-type",
        choices=(
            "bootstrap.identity-foundation",
            "revoke.principal",
            "revoke.organization",
            "revoke.tenant",
            "revoke.membership",
            "revoke.profile",
        ),
        required=True,
    )
    result_parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except IdentityOperationUnknown as error:
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
    except IdentityAdministrationError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception as error:
        # Validation/driver exceptions may contain SQL parameters or connection URLs.
        print(json.dumps({"error": "identity_administration_failed", "type": type(error).__name__}), file=sys.stderr)
        return 1
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

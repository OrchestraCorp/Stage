"""Nominate an existing first administrator; never create a login or accept on their behalf."""

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

from app.administration.bootstrap import NOMINATE_OPERATION, NominationPlan, nominate  # noqa: E402
from app.control.contracts import AdministrationError, AdministrationResult, OperationUnknown  # noqa: E402
from app.control.operations import operation_result  # noqa: E402
from scripts.identity_admin import local_operator_ref  # noqa: E402
from scripts.native_auth_admin import read_private_plan  # noqa: E402


async def run(args: argparse.Namespace) -> AdministrationResult:
    from app.core.config import Settings
    from app.core.db import create_engine, create_session_factory

    settings = Settings()
    if settings.env != args.environment:
        raise AdministrationError("environment_mismatch")
    if not os.environ.get("STAGE_DATABASE_URL"):
        raise AdministrationError("explicit_database_required")
    engine = create_engine(settings.database_url)
    try:
        sessions = create_session_factory(engine)
        actor = local_operator_ref()
        if args.command == "result":
            return await operation_result(
                sessions,
                environment=args.environment,
                actor_ref=actor,
                operation_type=NOMINATE_OPERATION,
                idempotency_key=args.idempotency_key,
            )
        plan = NominationPlan.model_validate_json(read_private_plan(args.plan))
        if plan.environment != settings.env:
            raise AdministrationError("environment_mismatch")
        return await nominate(sessions, plan, actor_ref=actor, idempotency_key=args.idempotency_key)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    nomination = commands.add_parser("nominate", help="nominate an already provisioned human under an explicit plan")
    nomination.add_argument("--plan", type=Path, required=True)
    nomination.add_argument("--idempotency-key", required=True)
    recovery = commands.add_parser("result", help="recover a nomination with the original OS operator and key")
    recovery.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except OperationUnknown as error:
        print(
            json.dumps(
                {
                    "error": "result_unknown",
                    "operation_type": error.operation_type,
                    "idempotency_key": error.idempotency_key,
                    "recovery": "query the original operator and key; do not silently replace the nomination",
                }
            ),
            file=sys.stderr,
        )
        return 2
    except AdministrationError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except Exception as error:
        print(json.dumps({"error": "administration_bootstrap_failed", "type": type(error).__name__}), file=sys.stderr)
        return 1
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

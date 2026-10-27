"""Explicit dev/test opt-in to employee onboarding capabilities for an accepted administrator."""

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

from app.administration.capabilities import (  # noqa: E402
    UPGRADE_OPERATION,
    UpgradeManagementCapabilitiesPlan,
    upgrade_management_capabilities,
)
from app.control.contracts import AdministrationError, AdministrationResult, OperationUnknown  # noqa: E402
from app.control.operations import operation_result  # noqa: E402
from scripts.identity_admin import local_operator_ref  # noqa: E402
from scripts.native_auth_admin import read_private_plan  # noqa: E402


async def run(args: argparse.Namespace) -> AdministrationResult:
    from app.core.config import Settings
    from app.core.db import create_engine, create_session_factory

    if args.environment not in {"dev", "test"}:
        raise AdministrationError("development_environment_required")
    settings = Settings()
    if settings.env not in {"dev", "test"} or settings.env != args.environment:
        raise AdministrationError("development_environment_required")
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
                operation_type=UPGRADE_OPERATION,
                idempotency_key=args.idempotency_key,
            )
        plan = UpgradeManagementCapabilitiesPlan.model_validate_json(read_private_plan(args.plan))
        if plan.environment != settings.env:
            raise AdministrationError("environment_mismatch")
        return await upgrade_management_capabilities(
            sessions, plan, actor_ref=actor, idempotency_key=args.idempotency_key
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    upgrade = commands.add_parser("upgrade", help="explicitly bind one accepted administrator to fixed role revision 2")
    upgrade.add_argument("--plan", type=Path, required=True)
    upgrade.add_argument("--idempotency-key", required=True)
    result = commands.add_parser("result", help="recover the original OS operator's upgrade using the original key")
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
                    "recovery": "query the original OS operator and key; retry only with the original plan and key",
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
            json.dumps({"error": "administration_capabilities_failed", "type": type(error).__name__}), file=sys.stderr
        )
        return 1
    print(response.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

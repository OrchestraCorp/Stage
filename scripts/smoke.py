"""Run the opt-in, read-only Stage live-service smoke suite.

Examples::

    uv run python scripts/smoke.py --base-url http://127.0.0.1:8080
    uv run python scripts/smoke.py --base-url http://127.0.0.1:8080 \
        --database-url postgresql+asyncpg://... \
        --redis-url redis://...

No database or Redis state is reset.  The suite is safe to run against a
shared development service because direct probes only execute ``SELECT 1``
and ``PING`` and HTTP probes only read system endpoints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Stage HTTP base URL")
    parser.add_argument("--database-url", help="read-only database URL")
    parser.add_argument("--redis-url", help="read-only Redis URL")
    parser.add_argument("--expect-ready", choices=("0", "1"), help="expected /ready result")
    parser.add_argument("--expect-redis", choices=("ready", "unavailable", "disabled"))
    parser.add_argument("--expect-openapi", choices=("auto", "0", "1"))
    parser.add_argument("--concurrency", type=int, help="concurrent requests per probe endpoint")
    parser.add_argument("--rounds", type=int, help="number of concurrent probe rounds")
    parser.add_argument("--timeout", type=float, help="request/probe timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    values = {
        "STAGE_SMOKE_BASE_URL": args.base_url,
        "STAGE_SMOKE_DATABASE_URL": args.database_url,
        "STAGE_SMOKE_REDIS_URL": args.redis_url,
        "STAGE_SMOKE_EXPECT_READY": args.expect_ready,
        "STAGE_SMOKE_EXPECT_REDIS": args.expect_redis,
        "STAGE_SMOKE_EXPECT_OPENAPI": args.expect_openapi,
        "STAGE_SMOKE_CONCURRENCY": str(args.concurrency) if args.concurrency is not None else None,
        "STAGE_SMOKE_ROUNDS": str(args.rounds) if args.rounds is not None else None,
        "STAGE_SMOKE_TIMEOUT_SECONDS": str(args.timeout) if args.timeout is not None else None,
    }
    for name, value in values.items():
        if value is not None:
            os.environ[name] = value

    import pytest

    os.chdir(ROOT)
    return int(pytest.main([str(ROOT / "tests/smoke"), "-q"]))


if __name__ == "__main__":
    sys.exit(main())

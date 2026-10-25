"""Check Redis connectivity and ephemeral keys without flushing shared data."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

from redis.exceptions import RedisError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.core.redis import create_redis_client  # noqa: E402


async def check_redis(url: str) -> None:
    if not url:
        raise ValueError("TEST_REDIS_URL must identify the Redis instance to check")
    # BaseSettings supports this runtime override; Pydantic's generated typing
    # omits underscored constructor options.
    config = Settings(_env_file=None, env="test", redis_url=url)  # type: ignore[call-arg]
    client = create_redis_client(config)
    key = "stage:probe:" + uuid4().hex
    created = False
    try:
        for attempt in range(30):
            try:
                if await client.ping():
                    break
            except (RedisError, OSError):
                if attempt == 29:
                    raise RuntimeError("Redis did not become available") from None
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Redis did not become available")
        print("PASS: Redis PING")

        created = bool(await client.set(key, "first", nx=True, ex=30))
        if not created or await client.set(key, "second", nx=True, ex=30):
            raise RuntimeError("Redis SET NX did not preserve key ownership")
        if await client.get(key) not in (b"first", "first") or not 0 < await client.ttl(key) <= 30:
            raise RuntimeError("Redis did not preserve the value and TTL")
        print("PASS: isolated SET NX, value and TTL")

        await client.pexpire(key, 100)
        for _ in range(30):
            if await client.get(key) is None:
                print("PASS: ephemeral key expired")
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Redis key did not expire")
    finally:
        try:
            if created:
                await client.delete(key)
        finally:
            await client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(check_redis(os.environ.get("TEST_REDIS_URL", "")))
    except Exception as error:
        # Connection/validation exceptions can contain credentials or URL inputs.
        print(f"Redis check failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None

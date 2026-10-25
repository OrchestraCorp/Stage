"""Optional Redis factory for a future Core provider; no startup dependency.

The provider owns explicit PING readiness checks and awaits client.aclose() on
shutdown. Redis is an auxiliary store; PostgreSQL owns durable business facts.
"""

import math
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config import Settings, validate_redis_url


def create_redis_client(config: Settings) -> Redis:
    """Build an unconnected async client with bounded connection and read waits."""
    if config.redis_url is None:
        raise ValueError("Redis use requires an explicit STAGE_REDIS_URL")
    validate_redis_url(config.redis_url)
    for timeout in (config.redis_connect_timeout_seconds, config.redis_socket_timeout_seconds):
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Redis timeouts must be finite and between 0 and 30 seconds")
    tls_options: dict[str, Any] = {}
    if urlsplit(config.redis_url).scheme == "rediss":
        tls_options = {"ssl_cert_reqs": "required", "ssl_check_hostname": True}
    return Redis.from_url(
        config.redis_url,
        socket_connect_timeout=config.redis_connect_timeout_seconds,
        socket_timeout=config.redis_socket_timeout_seconds,
        decode_responses=True,
        max_connections=config.redis_max_connections,
        # Commands with ambiguous outcomes must not be automatically replayed.
        retry=Retry(NoBackoff(), 0),
        **tls_options,
    )

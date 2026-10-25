import asyncio
from typing import Any

import pytest
from pydantic import ValidationError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import Settings, validate_redis_url
from app.core.redis import create_redis_client


@pytest.fixture(autouse=True)
def isolated_redis_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in ("STAGE_REDIS_URL", "STAGE_REDIS_CONNECT_TIMEOUT_SECONDS", "STAGE_REDIS_SOCKET_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("url", ("redis://localhost", "redis://user:pass@localhost:6379/2", "rediss://[::1]:6380/0"))
def test_supported_redis_urls(url: str) -> None:
    assert validate_redis_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/0",
        "unix:///tmp/redis.sock",
        "redis:///0",
        "redis://localhost:0/0",
        "redis://localhost:65536/0",
        "redis://localhost:invalid/0",
        "redis://localhost/not-a-db",
        "redis://localhost/-1",
        "redis://localhost/0?socket_timeout=0",
        "rediss://localhost/0?ssl_cert_reqs=none",
        "redis://localhost/0#fragment",
        " redis://localhost/0",
        "redis://local\nhost/0",
    ),
)
def test_invalid_redis_urls_fail_closed(url: str) -> None:
    with pytest.raises(ValueError, match="STAGE_REDIS_URL"):
        validate_redis_url(url)


def test_redis_is_optional_and_never_inferred_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE_REDIS_URL", raising=False)
    config = Settings(env="prod", database_url="postgresql+asyncpg://localhost/stage")
    assert config.redis_url is None
    with pytest.raises(ValueError, match="explicit STAGE_REDIS_URL"):
        create_redis_client(config)


def test_redis_credentials_are_redacted_from_config_and_validation_errors() -> None:
    secret = "synthetic-redis-password"
    config = Settings(redis_url=f"redis://user:{secret}@localhost:6379/0")
    assert secret not in repr(config)
    assert secret not in config.model_dump_json()
    assert "redis_url" not in config.model_dump()
    for url in (f"unsupported://user:{secret}@localhost/0", f"redis://user:password@localhost:{secret}/0"):
        with pytest.raises(ValidationError) as error:
            Settings(redis_url=url)
        assert secret not in str(error.value)
        assert secret not in repr(error.value)
        assert secret not in error.value.json()
        assert secret not in repr(error.value.errors())


@pytest.mark.parametrize("field", ("redis_connect_timeout_seconds", "redis_socket_timeout_seconds"))
@pytest.mark.parametrize("value", (0, -1, 31, float("nan"), float("inf")))
def test_redis_timeouts_are_finite_and_bounded(field: str, value: float) -> None:
    values: dict[str, Any] = {field: value}
    with pytest.raises(ValidationError):
        Settings(**values)


@pytest.mark.parametrize("scheme", ("redis", "rediss"))
async def test_redis_factory_creates_unconnected_client_with_controlled_timeouts(scheme: str) -> None:
    config = Settings(
        redis_url=f"{scheme}://user:synthetic-redis-password@localhost:6379/2",
        redis_connect_timeout_seconds=1.5,
        redis_socket_timeout_seconds=3,
    )
    client = create_redis_client(config)
    try:
        options = client.connection_pool.connection_kwargs
        assert options["host"] == "localhost" and options["db"] == 2
        assert options["socket_connect_timeout"] == 1.5
        assert options["socket_timeout"] == 3
        assert options["decode_responses"] is True
        assert options["retry"].get_retries() == 0
        assert "synthetic-redis-password" not in repr(client)
        if scheme == "rediss":
            assert options["ssl_cert_reqs"] == "required"
            assert options["ssl_check_hostname"] is True
    finally:
        await client.aclose()


@pytest.mark.parametrize("respond", (True, False))
async def test_redis_client_supports_native_ping_timeout_and_async_close(respond: bool) -> None:
    """Exercise the async RESP connection against a disposable local protocol sink."""
    finished = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                assert line.startswith(b"*")
                parts = []
                for _ in range(int(line[1:])):
                    size = int((await reader.readline())[1:])
                    parts.append(await reader.readexactly(size))
                    assert await reader.readexactly(2) == b"\r\n"
                if parts[0] == b"PING" and not respond:
                    continue
                writer.write(b"+PONG\r\n" if parts[0] == b"PING" else b"+OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_redis_client(Settings(redis_url=f"redis://127.0.0.1:{port}/0", redis_socket_timeout_seconds=0.1))
    try:
        async with asyncio.timeout(5):
            if respond:
                assert await client.ping() is True
            else:
                with pytest.raises(RedisTimeoutError):
                    await client.ping()
            await client.aclose()
            await finished.wait()
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()

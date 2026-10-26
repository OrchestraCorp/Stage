"""Shared synthetic builders for human concurrency."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.support.authenticated_commands import (
    seed_authenticated_command,
)


async def prepare(sessions: async_sessionmaker[AsyncSession]) -> None:
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("verified command races require PostgreSQL row locks")
    await seed_authenticated_command(sessions)

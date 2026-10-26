"""Observed PostgreSQL blocking edges and cancellation-safe test cleanup."""

import asyncio
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def wait_until_blocked_by(sessions: async_sessionmaker[AsyncSession], blocker_pid: int) -> None:
    """Require a real blocking edge; timing alone cannot establish lock ordering."""
    async with asyncio.timeout(10):
        async with sessions() as observer:
            while not await observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                ),
                {"blocker": blocker_pid},
            ):
                await asyncio.sleep(0.01)


async def finish_tasks(release: asyncio.Event, tasks: list[asyncio.Task[Any]]) -> None:
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

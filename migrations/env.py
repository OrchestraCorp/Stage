"""Alembic environment for the Stage async SQLAlchemy models."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import settings
from app.core.db import create_engine
from app.models import identity_foundation, native_auth  # noqa: F401
from app.models.authorization import RelationTupleTable, RoleAssignmentTable  # noqa: F401
from app.models.base import Base
from app.models.execution import (  # noqa: F401
    AuditRecordTable,
    CommandExecutionTable,
    DeliveryAttemptTable,
    OutboxRecordTable,
    TenantGuardTable,
)
from app.models.identity import PrincipalTable  # noqa: F401
from app.models.work import WorkItemTable  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_engine(settings.database_url)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

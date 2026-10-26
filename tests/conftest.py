import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import create_engine, create_session_factory  # noqa: E402
from app.models import administration, identity_foundation, native_auth  # noqa: E402, F401
from app.models.authorization import RelationTupleTable, RoleAssignmentTable  # noqa: E402, F401
from app.models.base import Base  # noqa: E402
from app.models.execution import (  # noqa: E402, F401
    AuditRecordTable,
    CommandExecutionTable,
    DeliveryAttemptTable,
    OutboxRecordTable,
    TenantGuardTable,
)
from app.models.identity import PrincipalTable  # noqa: E402, F401
from app.models.work import WorkItemTable  # noqa: E402, F401
from scripts.check_database import validate_test_database  # noqa: E402
from tests.support.database_safety import require_serial_database_reset  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Reject unsafe parallel reset before collecting or opening any database."""
    try:
        require_serial_database_reset(
            os.environ.get("TEST_DATABASE_URL"),
            workers=config.getoption("numprocesses", default=None),
            worker_id=os.environ.get("PYTEST_XDIST_WORKER"),
        )
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None


@pytest.fixture
async def persistence_sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Only disposable explicitly authorized databases may be reset by these tests."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'stage_test_execution.db'}"
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()

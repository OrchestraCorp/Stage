"""Portable storage semantics; domain code always receives UTC aware times."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator, TypeEngine


class UTCDateTime(TypeDecorator[datetime]):
    """Use timestamptz on PostgreSQL and explicit UTC wall time elsewhere.

    Naive values are rejected on write. Naive reads are only interpreted as UTC
    for MySQL/SQLite, where this adapter deliberately stores UTC without a zone.
    The migration requires an explicit acknowledgement before adopting old data.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=dialect.name == "postgresql"))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Stage timestamps must include a timezone")
        utc_value = value.astimezone(UTC)
        return utc_value if dialect.name == "postgresql" else utc_value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            if dialect.name == "postgresql":
                raise ValueError("PostgreSQL returned a naive Stage timestamp")
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ExactString(TypeDecorator[str]):
    """Identifiers and machine values use case/accent/space sensitive comparison.

    Display names should remain ordinary String/Text columns. PostgreSQL uses
    C collation and SQLite uses BINARY. The historical MySQL mapping is retained
    for migration/DDL checks; MySQL is no longer an application database target.
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int) -> None:
        super().__init__(length=length)
        self.length = length

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[str]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.VARCHAR(self.length, charset="utf8mb4", collation="utf8mb4_0900_bin"))
        collation = "C" if dialect.name == "postgresql" else "BINARY" if dialect.name == "sqlite" else None
        return dialect.type_descriptor(String(self.length, collation=collation))


class utc_now(FunctionElement[datetime]):
    """Database generated timestamps independent of the session timezone."""

    type = UTCDateTime()
    inherit_cache = True


@compiles(utc_now)
def _utc_now_default(element: utc_now, compiler: SQLCompiler, **kw: object) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(utc_now, "mysql")
def _utc_now_mysql(element: utc_now, compiler: SQLCompiler, **kw: object) -> str:
    return "(UTC_TIMESTAMP(6))"


@compiles(utc_now, "sqlite")
def _utc_now_sqlite(element: utc_now, compiler: SQLCompiler, **kw: object) -> str:
    return "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))"

"""Refuse parallel destructive fixtures against one configured database."""


def require_serial_database_reset(
    database_url: str | None, *, workers: int | str | None, worker_id: str | None
) -> None:
    # tmp_path SQLite databases are independent. TEST_DATABASE_URL selects one
    # shared schema whose fixtures drop all tables; one worker can erase another.
    if database_url and (worker_id is not None or workers not in (None, 0, "0")):
        raise ValueError(
            "TEST_DATABASE_URL uses one disposable database and cannot run with pytest-xdist; "
            "use -n 0, or omit TEST_DATABASE_URL for isolated SQLite fixtures"
        )

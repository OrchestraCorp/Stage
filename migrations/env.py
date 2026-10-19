"""Alembic environment placeholder for Stage."""

from alembic import context


def run_migrations_offline() -> None:
    context.configure(url=context.config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    raise RuntimeError("Stage migrations are not configured yet; define the async engine before generating revisions")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

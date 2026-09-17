"""Alembic environment: online migrations only (spec 012, Design 7.4).

The connection comes from ``config.attributes["connection"]``, which is how the application
shares one transaction with the migration. Without one, which is the developer CLI path, an
engine is built from the settings. Offline mode (``--sql``) is refused: it would need a URL in
the configuration, which this feature deliberately does not have.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from trading_bot.config import Settings
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.models import Base


def run_migrations_on(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,  # SQLite emulates ALTER TABLE by copying the table
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def main() -> None:
    if context.is_offline_mode():
        raise RuntimeError("offline migrations are not supported: run them against a connection")
    shared = context.config.attributes.get("connection")
    if isinstance(shared, Connection):
        run_migrations_on(shared)
        return
    engine = create_database_engine(database_path(Settings().data_dir))
    try:
        with engine.begin() as connection:
            run_migrations_on(connection)
    finally:
        engine.dispose()


main()

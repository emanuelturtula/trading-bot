"""The database handle the rest of the application is injected with (spec 012, Design 4.3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading_bot.persistence.engine import (
    BUSY_TIMEOUT_MS,
    create_database_engine,
    create_session_factory,
    database_path,
)
from trading_bot.persistence.migrator import run_migrations


@dataclass(frozen=True, slots=True)
class Database:
    """An open SQLite database: one engine per process and its session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session for one unit of work, committing it when the block succeeds.

        Every ``BaseException`` rolls the session back and propagates, which also covers the
        ``asyncio.CancelledError`` of a shutdown, and the session is always closed.
        """
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close the connection pool; SQLite then checkpoints and removes the ``-wal``."""
        self.engine.dispose()


def open_database(data_dir: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Database:
    """Open the database in ``data_dir``, applying the migrations before returning it.

    Migrating here means no caller can forget it. If the migration fails the engine is
    disposed and the error propagates, so a failed startup leaks no connection pool.
    """
    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=busy_timeout_ms)
    try:
        run_migrations(engine)
    except BaseException:
        engine.dispose()
        raise
    return Database(engine=engine, session_factory=create_session_factory(engine))

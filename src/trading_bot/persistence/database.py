"""The database handle the rest of the application is injected with (spec 012, Design 4.3).

``open_database`` migrates and is the application's entry point; ``connect_database`` opens an
already-migrated database and refuses anything else (spec 013, D95), which is what the CLI uses.
Alembic is imported inside those two functions on purpose: importing this module then drags
neither the migration machinery nor its dependencies into the Telegram and API layers, which
only need the handle and its sessions.
"""

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
from trading_bot.persistence.errors import SchemaMismatchError


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
    from trading_bot.persistence.migrator import run_migrations

    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=busy_timeout_ms)
    try:
        run_migrations(engine)
    except BaseException:
        engine.dispose()
        raise
    return Database(engine=engine, session_factory=create_session_factory(engine))


def connect_database(data_dir: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Database:
    """Open an already-migrated database, applying no migration (decision D95).

    Migrations belong to the one process that owns the database at startup (CLAUDE.md rule 7):
    a command-line tool that migrated could upgrade production from a stray container. A
    missing file is reported **before** the engine is built, so no empty database is created on
    a machine where the application has never run, and a revision that is not the head raises
    ``SchemaMismatchError`` naming the two revisions, never a path.
    """
    from trading_bot.persistence.migrator import current_revision, head_revision

    expected = head_revision()
    path = database_path(data_dir)
    if not path.is_file():
        raise SchemaMismatchError(None, expected)
    engine = create_database_engine(path, busy_timeout_ms=busy_timeout_ms)
    try:
        current = current_revision(engine)
    except BaseException:
        engine.dispose()
        raise
    if current != expected:
        engine.dispose()
        raise SchemaMismatchError(current, expected)
    return Database(engine=engine, session_factory=create_session_factory(engine))

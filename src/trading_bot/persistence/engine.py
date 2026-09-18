"""The SQLite engine, its pragmas and the session factory (spec 012, Design 4.1, 4.2).

``create_database_engine`` is the only supported way to build an engine: it registers the
``connect`` listener that applies the pragmas and hands transaction control to SQLAlchemy
(spec 013, D88), so no caller can end up with a connection without them. Foreign keys and the
busy timeout are per connection and reset on every new one, and the pool opens new connections
whenever it needs them.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import URL, Connection, Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

DATABASE_FILENAME = "trading_bot.db"
BUSY_TIMEOUT_MS = 5000


def database_path(data_dir: Path) -> Path:
    """Return the database file inside ``data_dir``.

    The file name is fixed: ``deploy/deploy.py`` backs up exactly this path, so an override
    would let the backup silently copy nothing.
    """
    return data_dir / DATABASE_FILENAME


def create_database_engine(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Engine:
    """Return an engine for the SQLite file ``path``, with the pragmas of Design 4.2.

    The parent directory is created private (``0o700``) if it is missing; an existing one
    keeps its mode. No file is created until the first connection, and statements are never
    echoed, so no path or statement reaches the logs through SQLAlchemy.
    """
    timeout_ms = int(busy_timeout_ms)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # URL.create escapes the path: a hand-built "sqlite:///{path}" loses everything after a
    # "#" and mangles spaces, which a development directory can easily contain.
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)))

    @event.listens_for(engine, "connect")
    def _set_pragmas(connection: DBAPIConnection, _entry: ConnectionPoolEntry) -> None:
        # pysqlite opens a transaction only before the first DML statement, so DDL would run
        # in autocommit and a migration that fails halfway would leave part of its schema
        # behind. Handing transaction control to SQLAlchemy also makes SAVEPOINT work (#13).
        # The pragmas run here, before any BEGIN: PRAGMA journal_mode=WAL is illegal inside a
        # transaction, so this order is load-bearing (spec 013, D88).
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
            cursor.execute("PRAGMA foreign_keys=ON")  # off by default, reset per connection
            cursor.execute("PRAGMA busy_timeout=" + str(timeout_ms))  # wait, do not fail
            cursor.execute("PRAGMA synchronous=FULL")  # survive a power cut on the SD card
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return the session factory of the application.

    ``expire_on_commit=False`` keeps loaded objects readable after the commit, so a notifier
    can format a stored signal without a surprise ``SELECT`` or a ``DetachedInstanceError``.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)

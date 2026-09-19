"""How transactions begin: sessions take the write lock first (spec 014, T3, AC7, AC9, D110).

Statements are recorded with a ``before_cursor_execute`` listener on ``Database.engine``, which
also sees what the sessions' bound engine executes. The lock is probed with a plain ``sqlite3``
connection opened with ``timeout=0``, so a refusal is immediate and no elapsed time is ever
measured. A second ``Database.session()`` is never opened while one is live in the same thread:
visibility and locks are checked through ``engine.connect()`` or ``sqlite3`` instead, which use
a deferred ``BEGIN`` and are never blocked by an open session.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.fixtures.database import temporary_database
from trading_bot.persistence.database import Database
from trading_bot.persistence.engine import (
    BEGIN_IMMEDIATE_OPTION,
    create_database_engine,
    create_session_factory,
    database_path,
)

TIMESTAMP = "2026-01-02 03:04:05.000000"


@contextmanager
def recorded_statements(engine: Engine) -> Iterator[list[str]]:
    """Every statement the engine (and any engine sharing its events) sends to SQLite."""
    statements: list[str] = []

    def spy(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", spy)


@contextmanager
def lock_probe(data_dir: Path) -> Iterator[sqlite3.Connection]:
    """A raw connection that never waits: ``BEGIN IMMEDIATE`` fails at once if it must wait."""
    with closing(
        sqlite3.connect(database_path(data_dir), timeout=0, isolation_level=None)
    ) as probe:
        yield probe


def write_lock_is_free(data_dir: Path) -> bool:
    """Whether another connection could take the write lock right now.

    Any refusal other than ``database is locked`` propagates: a probe that failed for another
    reason would silently make every caller read "the lock is held".
    """
    with lock_probe(data_dir) as probe:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "database is locked" not in str(error):
                raise
            return False
        probe.execute("ROLLBACK")
        return True


def insert_ticker(connection: Connection, symbol: str) -> None:
    connection.exec_driver_sql(
        "INSERT INTO tickers (symbol, timeframe, enabled, created_at) VALUES (?, '1d', 1, ?)",
        (symbol, TIMESTAMP),
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "volume"


@pytest.fixture
def handle(data_dir: Path) -> Iterator[Database]:
    with temporary_database(data_dir) as database:
        yield database


# --- AC7: what each kind of transaction emits ----------------------------------------------


def test_a_session_begins_its_transaction_immediate(handle: Database) -> None:
    with recorded_statements(handle.engine) as statements, handle.session() as session:
        session.execute(text("SELECT COUNT(*) FROM tickers"))

    assert statements == ["BEGIN IMMEDIATE", "SELECT COUNT(*) FROM tickers"]


def test_every_transaction_of_one_session_begins_immediate(handle: Database) -> None:
    with recorded_statements(handle.engine) as statements, handle.session() as session:
        session.execute(text("SELECT 1"))
        session.commit()
        session.execute(text("SELECT 2"))

    assert statements == ["BEGIN IMMEDIATE", "SELECT 1", "BEGIN IMMEDIATE", "SELECT 2"]


def test_an_idle_session_emits_nothing_not_even_on_commit(handle: Database) -> None:
    with recorded_statements(handle.engine) as statements:
        with handle.session():
            pass
        with handle.session() as session:
            session.commit()

    assert statements == []


def test_a_raw_connection_keeps_a_deferred_begin(handle: Database) -> None:
    with recorded_statements(handle.engine) as statements, handle.engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")

    assert statements == ["BEGIN", "SELECT 1"]


def test_engine_begin_keeps_a_deferred_begin(handle: Database) -> None:
    with recorded_statements(handle.engine) as statements, handle.engine.begin() as connection:
        connection.exec_driver_sql("SELECT 1")

    assert statements == ["BEGIN", "SELECT 1"]


def test_a_savepoint_inside_a_session_is_not_a_second_begin(handle: Database) -> None:
    with (
        recorded_statements(handle.engine) as statements,
        handle.session() as session,
        session.begin_nested(),
    ):
        session.execute(text("SELECT 1"))

    assert statements == [
        "BEGIN IMMEDIATE",
        "SAVEPOINT sa_savepoint_1",
        "SELECT 1",
        "RELEASE SAVEPOINT sa_savepoint_1",
    ]


# --- AC7: the write lock, probed from another connection -----------------------------------


def test_after_a_session_reads_no_other_connection_can_take_the_write_lock(
    handle: Database, data_dir: Path
) -> None:
    with handle.session() as session:
        session.execute(text("SELECT COUNT(*) FROM tickers"))  # a read, and nothing else

        assert write_lock_is_free(data_dir) is False

    assert write_lock_is_free(data_dir) is True


def test_after_a_raw_connection_reads_another_connection_can_take_the_write_lock(
    handle: Database, data_dir: Path
) -> None:
    with handle.engine.connect() as connection:
        connection.exec_driver_sql("SELECT COUNT(*) FROM tickers")

        assert connection.in_transaction() is True
        assert write_lock_is_free(data_dir) is True


def test_an_idle_session_holds_no_lock(handle: Database, data_dir: Path) -> None:
    with handle.session():
        assert write_lock_is_free(data_dir) is True


def test_a_writer_on_another_engine_is_refused_while_a_session_that_only_read_is_open(
    handle: Database, data_dir: Path
) -> None:
    """The lock is SQLite's, not the pool's: a second engine on the file cannot write either."""
    other = create_database_engine(database_path(data_dir), busy_timeout_ms=0)
    try:
        with handle.session() as session:
            session.execute(text("SELECT 1"))

            with (
                pytest.raises(OperationalError, match="database is locked"),
                other.begin() as connection,
            ):
                insert_ticker(connection, "MSFT")
    finally:
        other.dispose()


# --- AC7: the factory and the handle ------------------------------------------------------


def test_the_session_factory_binds_an_engine_that_shares_the_pool(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        factory = create_session_factory(engine)
        bind = factory.kw["bind"]

        assert factory.kw["expire_on_commit"] is False
        assert isinstance(bind, Engine)
        assert bind is not engine
        assert bind.pool is engine.pool
        assert bind.get_execution_options()[BEGIN_IMMEDIATE_OPTION] is True
        assert BEGIN_IMMEDIATE_OPTION not in engine.get_execution_options()
        with factory() as session:
            assert isinstance(session, Session)
    finally:
        engine.dispose()


def test_the_database_handle_keeps_the_plain_engine(handle: Database) -> None:
    assert BEGIN_IMMEDIATE_OPTION not in handle.engine.get_execution_options()
    assert handle.session_factory.kw["expire_on_commit"] is False


def test_the_option_name_is_the_documented_one() -> None:
    assert BEGIN_IMMEDIATE_OPTION == "trading_bot_begin_immediate"


def test_a_false_option_keeps_a_deferred_begin(handle: Database) -> None:
    """Only ``True`` turns it on: the listener compares with ``is True``."""
    deferred = handle.engine.execution_options(**{BEGIN_IMMEDIATE_OPTION: False})
    with recorded_statements(handle.engine) as statements, deferred.connect() as connection:
        connection.exec_driver_sql("SELECT 1")

    assert statements == ["BEGIN", "SELECT 1"]


# --- AC9: the hazard D110 removes ----------------------------------------------------------


def test_a_deferred_reader_that_writes_after_a_commit_fails_with_busy_snapshot(
    handle: Database,
) -> None:
    """Characterization: the busy timeout never retries a stale snapshot (D110).

    The reader's ``SELECT`` fixes its snapshot; a session then commits a row; the reader's write
    must upgrade a snapshot that is no longer the latest, which SQLite refuses at once with
    ``SQLITE_BUSY_SNAPSHOT``, even though the write lock is free. If SQLite ever changes this,
    this test fails and D110 must be revisited.
    """
    reader = handle.engine.connect()
    try:
        reader.exec_driver_sql("SELECT COUNT(*) FROM tickers").scalar()

        with handle.session() as session:
            session.execute(
                text(
                    "INSERT INTO tickers (symbol, timeframe, enabled, created_at)"
                    " VALUES ('AAPL', '1d', 1, :created_at)"
                ),
                {"created_at": TIMESTAMP},
            )

        with pytest.raises(OperationalError) as caught:
            insert_ticker(reader, "MSFT")
        reader.rollback()
    finally:
        reader.close()

    driver_error = caught.value.orig
    assert isinstance(driver_error, sqlite3.OperationalError)
    assert driver_error.sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"
    with handle.engine.connect() as connection:
        symbols = connection.exec_driver_sql("SELECT symbol FROM tickers").scalars().all()
    assert list(symbols) == ["AAPL"]


def test_a_session_that_reads_then_writes_cannot_be_overtaken(
    handle: Database, data_dir: Path
) -> None:
    """With D110 the same shape inside a session is safe: nobody can commit in between."""
    with handle.session() as session:
        session.execute(text("SELECT COUNT(*) FROM tickers")).scalar()

        assert write_lock_is_free(data_dir) is False

        session.execute(
            text(
                "INSERT INTO tickers (symbol, timeframe, enabled, created_at)"
                " VALUES ('MSFT', '1d', 1, :created_at)"
            ),
            {"created_at": TIMESTAMP},
        )

    with handle.engine.connect() as connection:
        symbols = connection.exec_driver_sql("SELECT symbol FROM tickers").scalars().all()
    assert list(symbols) == ["MSFT"]

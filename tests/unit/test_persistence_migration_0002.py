"""Revision ``0002`` and the transactional-DDL recipe (spec 013, T2, T3, AC7, AC8, AC29).

Every database lives under ``tmp_path`` and is opened through the factory engine, so the
pragmas and the transaction control of ``create_database_engine`` are the ones under test.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from trading_bot.persistence.engine import BUSY_TIMEOUT_MS, create_database_engine, database_path
from trading_bot.persistence.migrator import (
    alembic_config,
    current_revision,
    head_revision,
    run_migrations,
)
from trading_bot.persistence.models import Base

BASELINE = "0001"
HEAD = "0002"
TABLES = ("rules", "ticker_rules", "tickers")

# The pragmas every connection must still report once SQLAlchemy owns the transaction (D88).
EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": BUSY_TIMEOUT_MS,
    "synchronous": 2,  # FULL
}

# A throwaway revision that creates one table and then fails, so the recipe is what decides
# whether the table survives. It never touches the packaged migrations.
FAILING_REVISION = '''"""a revision that fails after its first statement"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE half_applied (id INTEGER PRIMARY KEY)")
    raise RuntimeError("the migration failed halfway")


def downgrade() -> None:
    op.execute("DROP TABLE half_applied")
'''

THROWAWAY_ENV = '''"""Online migrations on the connection the caller shares."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

connection = context.config.attributes["connection"]
assert isinstance(connection, Connection)
context.configure(connection=connection)
with context.begin_transaction():
    context.run_migrations()
'''


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path))


def read_pragmas(connection: Connection) -> dict[str, object]:
    names = ("journal_mode", "foreign_keys", "busy_timeout", "synchronous")
    return {name: connection.exec_driver_sql(f"PRAGMA {name}").scalar() for name in names}


def driver_in_transaction(connection: Connection) -> bool:
    """Ask pysqlite itself whether a transaction is open on this connection."""
    driver = connection.connection.driver_connection
    assert isinstance(driver, sqlite3.Connection)
    return driver.in_transaction


def table_names(engine: Engine) -> list[str]:
    """The tables SQLite holds, without the internal ones (``sqlite_sequence``)."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name"
        )
        return [str(name) for name in rows.scalars().all()]


def has_sequence_table(engine: Engine) -> bool:
    """``sqlite_sequence`` exists only once a table was created with ``AUTOINCREMENT``."""
    with engine.connect() as connection:
        found = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_sequence'"
        ).scalar()
    return found == 1


def stored_revisions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        return [str(value) for value in rows.scalars().all()]


def throwaway_config(tmp_path: Path) -> Config:
    """An Alembic script directory of its own, holding only the failing revision."""
    root = tmp_path / "throwaway"
    versions = root / "versions"
    versions.mkdir(parents=True)
    (root / "env.py").write_text(THROWAWAY_ENV, encoding="utf-8", newline="\n")
    (versions / "0001_failing.py").write_text(FAILING_REVISION, encoding="utf-8", newline="\n")
    config = Config()
    config.set_main_option("script_location", str(root))
    return config


# --- T3: transactional DDL (AC8) -----------------------------------------------------------


def test_a_transaction_is_open_after_a_create_table_inside_engine_begin(tmp_path: Path) -> None:
    """Without the recipe pysqlite runs DDL in autocommit and reports no transaction."""
    engine = fresh_engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

            assert driver_in_transaction(connection) is True
    finally:
        engine.dispose()


def test_a_transaction_is_open_before_any_statement(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with engine.begin() as connection:
            assert driver_in_transaction(connection) is True
    finally:
        engine.dispose()


def test_a_create_table_rolled_back_by_its_transaction_leaves_nothing(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            connection.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            transaction.rollback()

        assert table_names(engine) == []
    finally:
        engine.dispose()


def test_a_migration_that_fails_halfway_leaves_no_table_and_no_stamp(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    config = throwaway_config(tmp_path)

    def failing_upgrade() -> None:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    try:
        with pytest.raises(RuntimeError, match="halfway"):
            failing_upgrade()

        assert table_names(engine) == []
    finally:
        engine.dispose()


def test_a_successful_unit_of_work_still_commits(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO probe (id) VALUES (7)")

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM probe").scalar() == 7
    finally:
        engine.dispose()


def test_the_pragmas_still_hold_on_the_first_connection(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with engine.connect() as connection:
            assert read_pragmas(connection) == EXPECTED_PRAGMAS
    finally:
        engine.dispose()


def test_the_pragmas_still_hold_on_a_connection_from_another_thread(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    found: list[dict[str, object]] = []

    def read_from_thread() -> None:
        with engine.connect() as connection:
            found.append(read_pragmas(connection))

    try:
        with engine.connect() as connection:
            read_pragmas(connection)
        thread = threading.Thread(target=read_from_thread)
        thread.start()
        thread.join(timeout=30)
    finally:
        engine.dispose()

    assert found == [EXPECTED_PRAGMAS]


def test_the_pragmas_still_hold_after_dispose(tmp_path: Path) -> None:
    """``PRAGMA journal_mode=WAL`` is illegal inside a transaction: the listener order matters."""
    engine = fresh_engine(tmp_path)
    try:
        with engine.connect() as connection:
            read_pragmas(connection)

        engine.dispose()

        with engine.connect() as connection:
            assert read_pragmas(connection) == EXPECTED_PRAGMAS
    finally:
        engine.dispose()


def test_a_savepoint_rolls_back_independently_of_its_outer_transaction(tmp_path: Path) -> None:
    """#13 catches the signal unique constraint in a savepoint without losing its unit of work."""
    engine = fresh_engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

        with Session(engine) as session:
            session.execute(text("INSERT INTO probe (id) VALUES (1)"))
            savepoint = session.begin_nested()
            session.execute(text("INSERT INTO probe (id) VALUES (2)"))
            savepoint.rollback()
            session.execute(text("INSERT INTO probe (id) VALUES (3)"))
            session.commit()

        with engine.connect() as connection:
            rows = connection.exec_driver_sql("SELECT id FROM probe ORDER BY id").scalars().all()

        assert list(rows) == [1, 3]
    finally:
        engine.dispose()


# --- T2: revision 0002 (AC7, AC29) ---------------------------------------------------------


def test_upgrade_reaches_0002_and_creates_the_three_tables(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        assert run_migrations(engine) == HEAD

        assert current_revision(engine) == HEAD
        assert stored_revisions(engine) == [HEAD]
        assert table_names(engine) == ["alembic_version", *TABLES]
        assert has_sequence_table(engine) is True
    finally:
        engine.dispose()


def test_downgrade_to_base_drops_the_three_tables_and_empties_the_version_table(
    tmp_path: Path,
) -> None:
    engine = fresh_engine(tmp_path)
    config = alembic_config()
    try:
        run_migrations(engine)

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")

        assert table_names(engine) == ["alembic_version"]
        assert stored_revisions(engine) == []
        assert current_revision(engine) is None

        assert run_migrations(engine) == HEAD
        assert table_names(engine) == ["alembic_version", *TABLES]
    finally:
        engine.dispose()


def test_a_downgrade_of_one_step_returns_to_the_baseline(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    config = alembic_config()
    try:
        run_migrations(engine)

        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, BASELINE)

        assert current_revision(engine) == BASELINE
        assert table_names(engine) == ["alembic_version"]
    finally:
        engine.dispose()


def test_the_head_is_0002_and_it_follows_the_baseline() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD]
    assert head_revision() == HEAD
    assert script.get_revision(HEAD).down_revision == BASELINE


def test_the_migrated_schema_holds_exactly_the_declared_tables(tmp_path: Path) -> None:
    """The revision and the models cannot drift apart without one of them failing."""
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)

        assert sorted(Base.metadata.tables) == sorted(TABLES)
        assert table_names(engine) == ["alembic_version", *TABLES]
    finally:
        engine.dispose()

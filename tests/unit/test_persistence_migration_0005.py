"""Revision ``0005``: tickers and rules get an owner (spec 024, AC1).

The upgrade runs on a ``0004`` database that already holds configuration, assignments,
signals and deleted ids, as production does. Every row must survive with its id, the
``sqlite_sequence`` counters must not go back, and the stored DDL must match the models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.schema import CreateTable

from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import (
    alembic_config,
    current_revision,
    head_revision,
    run_migrations,
)
from trading_bot.persistence.models import Base

PREVIOUS = "0004"
THIS = "0005"
TIMESTAMP = "2026-01-02 03:04:05.000000"
CHILD_QUERIES = (
    "SELECT ticker_id, rule_id, timeframe, created_at FROM ticker_rules ORDER BY ticker_id",
    "SELECT id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price"
    " FROM signals ORDER BY id",
    'SELECT "key", value FROM bot_state ORDER BY "key"',
)
PARENT_QUERIES = (
    "SELECT id, symbol, timeframe, enabled, created_at FROM tickers ORDER BY id",
    "SELECT id, name, signal, timeframe, definition_json, enabled, created_at, updated_at"
    " FROM rules ORDER BY id",
)


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


def move_to(engine: Engine, revision: str, *, down: bool = False) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def rows(engine: Engine, queries: tuple[str, ...]) -> list[str]:
    with engine.connect() as connection:
        return [repr(tuple(row)) for query in queries for row in connection.exec_driver_sql(query)]


def scalar(engine: Engine, query: str) -> object:
    with engine.connect() as connection:
        return connection.exec_driver_sql(query).scalar()


def add_user(engine: Engine, username: str, *, admin: bool) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (username, password_hash, is_admin, active, created_at,"
            " updated_at) VALUES (?, NULL, ?, 1, ?, ?)",
            (username, int(admin), TIMESTAMP, TIMESTAMP),
        )


def seed(engine: Engine) -> None:
    """Configuration with a gap in every id sequence, an assignment, a signal and a pause."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO tickers (symbol, timeframe, enabled, created_at)"
            " VALUES ('GONE', '1d', 1, ?), ('AAPL', '1d', 1, ?), ('MSFT', '1h', 0, ?),"
            " ('LAST', '1d', 1, ?)",
            (TIMESTAMP,) * 4,
        )
        connection.exec_driver_sql(
            "INSERT INTO rules"
            " (name, signal, timeframe, definition_json, enabled, created_at, updated_at)"
            " VALUES ('daily', 'BUY', '1d', '{}', 1, ?, ?), ('last', 'SELL', '1d', '{}', 0, ?, ?)",
            (TIMESTAMP,) * 4,
        )
        connection.exec_driver_sql(
            "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
            " VALUES (2, 1, '1d', ?)",
            (TIMESTAMP,),
        )
        connection.exec_driver_sql(
            "INSERT INTO signals (ticker_id, rule_id, timeframe, candle_close_ts, side,"
            " close_price, indicator_values_json, created_at, notified_at) VALUES"
            " (2, 1, '1d', '2024-01-02 05:00:00.000000', 'BUY', 180.0, '{}', ?, NULL),"
            " (2, 1, '1d', '2024-01-03 05:00:00.000000', 'BUY', 187.5, '{}', ?, NULL)",
            (TIMESTAMP, TIMESTAMP),
        )
        connection.exec_driver_sql(
            "INSERT INTO bot_state (\"key\", value, updated_at) VALUES ('paused_since', ?, ?)",
            (TIMESTAMP, TIMESTAMP),
        )
        # The last id of each table is deleted, so only sqlite_sequence remembers it.
        connection.exec_driver_sql("DELETE FROM signals WHERE id = 2")
        connection.exec_driver_sql("DELETE FROM tickers WHERE symbol IN ('GONE', 'LAST')")
        connection.exec_driver_sql("DELETE FROM rules WHERE name = 'last'")


def sequences(engine: Engine) -> list[str]:
    return rows(engine, ("SELECT name, seq FROM sqlite_sequence ORDER BY name",))


def owners(engine: Engine, table: str) -> list[int]:
    with engine.connect() as connection:
        found = connection.exec_driver_sql(
            f"SELECT DISTINCT user_id FROM {table} ORDER BY user_id"  # noqa: S608
        )
        return [int(value) for value in found.scalars().all()]


def test_0005_follows_0004() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_revision(THIS).down_revision == PREVIOUS
    assert head_revision() >= THIS


def test_the_existing_rows_go_to_the_first_administrator(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        add_user(engine, "ana", admin=False)
        add_user(engine, "emi", admin=True)
        add_user(engine, "zoe", admin=True)
        seed(engine)
        parents, children, counters = (
            rows(engine, PARENT_QUERIES),
            rows(engine, CHILD_QUERIES),
            sequences(engine),
        )

        assert run_migrations(engine) == head_revision()

        assert rows(engine, PARENT_QUERIES) == parents
        assert rows(engine, CHILD_QUERIES) == children
        assert sequences(engine) == counters
        assert owners(engine, "tickers") == [2]
        assert owners(engine, "rules") == [2]
        assert scalar(engine, "SELECT COUNT(*) FROM users") == 3
        assert rows(engine, ("PRAGMA foreign_key_check",)) == []
    finally:
        engine.dispose()


def test_a_database_without_users_gets_an_admin_without_a_password(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)

        move_to(engine, THIS)

        assert rows(
            engine, ("SELECT id, username, password_hash, is_admin, active FROM users",)
        ) == [repr((1, "admin", None, 1, 1))]
        assert owners(engine, "tickers") == [1]
        assert owners(engine, "rules") == [1]
    finally:
        engine.dispose()


def test_without_an_administrator_the_first_user_becomes_one(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        add_user(engine, "zoe", admin=False)
        add_user(engine, "ana", admin=False)
        seed(engine)

        move_to(engine, THIS)

        assert rows(engine, ("SELECT username, is_admin FROM users ORDER BY id",)) == [
            repr(("zoe", 1)),
            repr(("ana", 0)),
        ]
        assert owners(engine, "tickers") == [1]
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["tickers", "rules", "ticker_rules", "signals"])
def test_the_stored_ddl_matches_the_models(tmp_path: Path, table: str) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        move_to(engine, THIS)

        stored = scalar(engine, f"SELECT sql FROM sqlite_master WHERE name = '{table}'")  # noqa: S608
        rendered = str(CreateTable(Base.metadata.tables[table]).compile(dialect=engine.dialect))
        # Revision 0006 (spec 027) widened the timeframe check; 0005 stored the narrower one.
        rendered = rendered.replace("'1d', '1w'", "'1d'")
        assert " ".join(str(stored).split()) == " ".join(rendered.split())
    finally:
        engine.dispose()


def test_the_indexes_and_no_temporary_table_survive(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        query = (
            "SELECT type, name FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY type, name",
        )
        before = rows(engine, query)

        move_to(engine, THIS)

        assert rows(engine, query) == before
    finally:
        engine.dispose()


def test_new_ids_continue_after_the_deleted_ones(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        move_to(engine, THIS)

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO tickers (symbol, timeframe, enabled, created_at, user_id)"
                " VALUES ('NEW', '1d', 1, ?, 1)",
                (TIMESTAMP,),
            )
        assert scalar(engine, "SELECT id FROM tickers WHERE symbol = 'NEW'") == 5
    finally:
        engine.dispose()


def test_downgrade_to_0004_drops_the_owner_and_keeps_every_row(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        move_to(engine, THIS)
        parents, children, counters = (
            rows(engine, PARENT_QUERIES),
            rows(engine, CHILD_QUERIES),
            sequences(engine),
        )

        move_to(engine, PREVIOUS, down=True)

        assert current_revision(engine) == PREVIOUS
        assert rows(engine, PARENT_QUERIES) == parents
        assert rows(engine, CHILD_QUERIES) == children
        assert sequences(engine) == counters
        assert "user_id" not in str(
            scalar(engine, "SELECT sql FROM sqlite_master WHERE name = 'tickers'")
        )
        assert rows(engine, ("PRAGMA foreign_key_check",)) == []
        assert run_migrations(engine) == head_revision()
    finally:
        engine.dispose()

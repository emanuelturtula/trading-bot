"""Revision ``0006``: the weekly timeframe (spec 027, AC5).

The upgrade runs on a ``0005`` database that already holds configuration, assignments, signals,
state and deleted ids, as production does. Every row must survive with its id, the
``sqlite_sequence`` counters must not go back, ``1w`` rows must be accepted afterwards and the
stored DDL must match the models. The downgrade deletes the ``1w`` rows and restores the old
constraints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import alembic_config, current_revision, head_revision
from trading_bot.persistence.models import Base

PREVIOUS = "0005"
THIS = "0006"
TIMESTAMP = "2026-01-02 03:04:05.000000"
QUERIES = (
    "SELECT id, symbol, timeframe, enabled, created_at, user_id FROM tickers ORDER BY id",
    "SELECT id, name, signal, timeframe, definition_json, enabled, created_at, updated_at,"
    " user_id FROM rules ORDER BY id",
    "SELECT ticker_id, rule_id, timeframe, created_at FROM ticker_rules ORDER BY ticker_id",
    "SELECT id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price, notified_at"
    " FROM signals ORDER BY id",
    'SELECT "key", value, updated_at FROM bot_state ORDER BY "key"',
    "SELECT name, seq FROM sqlite_sequence ORDER BY name",
)
TABLES = ("tickers", "rules", "ticker_rules", "signals", "bot_state")


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


def move_to(engine: Engine, revision: str, *, down: bool = False) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def rows(engine: Engine, queries: tuple[str, ...] = QUERIES) -> list[str]:
    with engine.connect() as connection:
        return [repr(tuple(row)) for query in queries for row in connection.exec_driver_sql(query)]


def execute(engine: Engine, statement: str, parameters: tuple[object, ...] = ()) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(statement, parameters)


def seed(engine: Engine) -> None:
    """A user, configuration with deleted ids, an assignment, a signal and two state rows."""
    execute(
        engine,
        "INSERT INTO users (username, password_hash, is_admin, active, created_at, updated_at)"
        " VALUES ('ana', NULL, 1, 1, ?, ?)",
        (TIMESTAMP, TIMESTAMP),
    )
    execute(
        engine,
        "INSERT INTO tickers (symbol, timeframe, enabled, created_at, user_id)"
        " VALUES ('AAPL', '1d', 1, ?, 1), ('MSFT', '4h', 0, ?, 1), ('GONE', '1h', 1, ?, 1)",
        (TIMESTAMP,) * 3,
    )
    execute(
        engine,
        "INSERT INTO rules (name, signal, timeframe, definition_json, enabled, created_at,"
        " updated_at, user_id) VALUES ('daily', 'BUY', '1d', '{}', 1, ?, ?, 1),"
        " ('gone', 'SELL', '1h', '{}', 1, ?, ?, 1)",
        (TIMESTAMP,) * 4,
    )
    execute(
        engine,
        "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
        " VALUES (1, 1, '1d', ?)",
        (TIMESTAMP,),
    )
    execute(
        engine,
        "INSERT INTO signals (ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at, notified_at) VALUES"
        " (1, 1, '1d', '2024-01-02 05:00:00.000000', 'BUY', 180.0, '{}', ?, ?),"
        " (1, 1, '1d', '2024-01-03 05:00:00.000000', 'BUY', 181.0, '{}', ?, NULL)",
        (TIMESTAMP,) * 3,
    )
    execute(
        engine,
        "INSERT INTO bot_state (\"key\", value, updated_at) VALUES ('paused_since', ?, ?),"
        " ('last_run.1d', ?, ?)",
        (TIMESTAMP,) * 4,
    )
    execute(engine, "DELETE FROM signals WHERE id = 2")
    execute(engine, "DELETE FROM tickers WHERE symbol = 'GONE'")
    execute(engine, "DELETE FROM rules WHERE name = 'gone'")


def add_weekly(engine: Engine) -> None:
    execute(
        engine,
        "INSERT INTO tickers (symbol, timeframe, enabled, created_at, user_id)"
        " VALUES ('SPY', '1w', 1, ?, 1)",
        (TIMESTAMP,),
    )
    execute(
        engine,
        "INSERT INTO rules (name, signal, timeframe, definition_json, enabled, created_at,"
        " updated_at, user_id) VALUES ('weekly', 'BUY', '1w', '{}', 1, ?, ?, 1)",
        (TIMESTAMP, TIMESTAMP),
    )
    execute(
        engine,
        "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
        " VALUES (4, 3, '1w', ?)",
        (TIMESTAMP,),
    )
    execute(
        engine,
        "INSERT INTO signals (ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at) VALUES"
        " (4, 3, '1w', '2024-06-17 04:00:00.000000', 'BUY', 500.0, '{}', ?)",
        (TIMESTAMP,),
    )
    execute(
        engine,
        "INSERT INTO bot_state (\"key\", value, updated_at) VALUES ('last_run.1w', ?, ?)",
        (TIMESTAMP, TIMESTAMP),
    )


def test_0006_is_the_head_and_follows_0005() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [THIS]
    assert head_revision() == THIS
    assert script.get_revision(THIS).down_revision == PREVIOUS


def test_every_row_and_counter_survives_the_upgrade(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        before = rows(engine)

        move_to(engine, THIS)

        assert current_revision(engine) == THIS
        assert rows(engine) == before
        assert rows(engine, ("PRAGMA foreign_key_check",)) == []
    finally:
        engine.dispose()


def test_weekly_rows_are_refused_before_and_accepted_after(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        with pytest.raises(IntegrityError, match="CHECK constraint"):
            add_weekly(engine)

        move_to(engine, THIS)
        add_weekly(engine)

        assert rows(engine, ("SELECT COUNT(*) FROM signals WHERE timeframe = '1w'",)) == ["(1,)"]
        assert rows(engine, ("SELECT id FROM tickers WHERE symbol = 'SPY'",)) == ["(4,)"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", TABLES)
def test_the_stored_ddl_matches_the_models(tmp_path: Path, table: str) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        move_to(engine, THIS)

        with engine.connect() as connection:
            stored = connection.exec_driver_sql(
                f"SELECT sql FROM sqlite_master WHERE name = '{table}'"  # noqa: S608
            ).scalar()
        rendered = str(CreateTable(Base.metadata.tables[table]).compile(dialect=engine.dialect))
        assert " ".join(str(stored).split()) == " ".join(rendered.split())
    finally:
        engine.dispose()


def test_the_indexes_and_no_temporary_table_survive(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    query = (
        "SELECT type, name FROM sqlite_master WHERE sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY type, name",
    )
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        before = rows(engine, query)

        move_to(engine, THIS)

        assert rows(engine, query) == before
    finally:
        engine.dispose()


def test_downgrade_deletes_the_weekly_rows_and_keeps_the_rest(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        move_to(engine, THIS)
        before = rows(engine, QUERIES[:-1])
        add_weekly(engine)

        move_to(engine, PREVIOUS, down=True)

        assert current_revision(engine) == PREVIOUS
        assert rows(engine, QUERIES[:-1]) == before
        assert rows(engine, ("PRAGMA foreign_key_check",)) == []
        with pytest.raises(IntegrityError, match="CHECK constraint"):
            add_weekly(engine)
    finally:
        engine.dispose()

"""Revision ``0004``: the dashboard users (spec 023, AC1).

The upgrade runs on a ``0003`` database that already holds rows, as production does; the
downgrade drops ``users`` and leaves every other row intact.
"""

from __future__ import annotations

from pathlib import Path

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

PREVIOUS = "0003"
HEAD = "0005"
THIS = "0004"
TIMESTAMP = "2026-01-02 03:04:05.000000"
EXISTING_QUERIES = (
    "SELECT id, symbol, timeframe, enabled, created_at FROM tickers ORDER BY id",
    "SELECT id, name, definition_json FROM rules ORDER BY id",
    'SELECT "key", value FROM bot_state ORDER BY "key"',
)


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


def move_to(engine: Engine, revision: str, *, down: bool = False) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def table_names(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name"
        )
        return [str(name) for name in rows.scalars().all()]


def existing_rows(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            repr(tuple(row))
            for query in EXISTING_QUERIES
            for row in connection.exec_driver_sql(query)
        ]


def seed(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO tickers (symbol, timeframe, enabled, created_at)"
            " VALUES ('AAPL', '1d', 1, ?)",
            (TIMESTAMP,),
        )
        connection.exec_driver_sql(
            "INSERT INTO rules"
            " (name, signal, timeframe, definition_json, enabled, created_at, updated_at)"
            " VALUES ('daily', 'BUY', '1d', '{}', 1, ?, ?)",
            (TIMESTAMP, TIMESTAMP),
        )
        connection.exec_driver_sql(
            "INSERT INTO bot_state (\"key\", value, updated_at) VALUES ('paused_since', ?, ?)",
            (TIMESTAMP, TIMESTAMP),
        )


def test_0004_follows_0003_and_0005_follows_it() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD]
    assert head_revision() == HEAD
    assert script.get_revision(THIS).down_revision == PREVIOUS
    assert script.get_revision(HEAD).down_revision == THIS


def test_upgrading_a_0003_database_adds_an_empty_users_table(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        seed(engine)
        before = existing_rows(engine)

        move_to(engine, THIS)

        assert existing_rows(engine) == before
        assert "users" in table_names(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM users").scalar() == 0
            stored = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = 'users'"
            ).scalar()
        rendered = str(CreateTable(Base.metadata.tables["users"]).compile(dialect=engine.dialect))
        # 0005 changes tickers and rules, never the users table
        assert " ".join(str(stored).split()) == " ".join(rendered.split())
    finally:
        engine.dispose()


def test_downgrade_to_0003_drops_only_the_users(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, THIS)
        seed(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, is_admin, active, created_at,"
                " updated_at) VALUES ('admin', NULL, 1, 1, ?, ?)",
                (TIMESTAMP, TIMESTAMP),
            )
        before = existing_rows(engine)

        move_to(engine, PREVIOUS, down=True)

        assert current_revision(engine) == PREVIOUS
        assert "users" not in table_names(engine)
        assert existing_rows(engine) == before
        assert run_migrations(engine) == HEAD
    finally:
        engine.dispose()

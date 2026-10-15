"""Revision ``0007``: each user's bot, linked chats and pause (spec 025, D1).

The revision only adds tables, so a ``0006`` database with configuration keeps every row, and
the stored DDL must match the models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.schema import CreateTable

from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import alembic_config, current_revision, run_migrations
from trading_bot.persistence.models import Base

PREVIOUS = "0006"
THIS = "0007"
TABLES = ("telegram_bots", "telegram_chats", "user_pauses")
TIMESTAMP = "2026-01-02 03:04:05.000000"


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


def move_to(engine: Engine, revision: str, *, down: bool = False) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def table_names(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        found = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
            " ORDER BY name"
        )
        return [str(name) for name in found.scalars()]


def test_0007_is_the_head_and_follows_0006() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [THIS]
    assert script.get_revision(THIS).down_revision == PREVIOUS


@pytest.mark.parametrize("table", TABLES)
def test_the_upgrade_creates_the_tables_the_models_declare(tmp_path: Path, table: str) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, THIS)
        with engine.connect() as connection:
            stored = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).scalar()
        rendered = str(CreateTable(Base.metadata.tables[table]).compile(dialect=engine.dialect))
        assert " ".join(str(stored).split()) == " ".join(rendered.split())
    finally:
        engine.dispose()


def test_upgrading_keeps_the_configuration_and_downgrading_drops_only_the_new_tables(
    tmp_path: Path,
) -> None:
    engine = fresh_engine(tmp_path)
    try:
        move_to(engine, PREVIOUS)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO tickers (user_id, symbol, timeframe, enabled, created_at)"
                " VALUES (1, 'AAPL', '1d', 1, ?)",
                (TIMESTAMP,),
            )
        before = table_names(engine)

        assert run_migrations(engine) == THIS
        assert table_names(engine) == sorted([*before, *TABLES])
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO telegram_bots (user_id, token_ciphertext, token_fingerprint,"
                " bot_username, link_digest, link_expires_at, created_at, updated_at)"
                " VALUES (1, 'c', 'f', NULL, NULL, NULL, ?, ?)",
                (TIMESTAMP, TIMESTAMP),
            )
            connection.exec_driver_sql(
                "INSERT INTO telegram_chats (user_id, chat_id, title, created_at)"
                " VALUES (1, ?, NULL, ?)",
                (-(10**12), TIMESTAMP),
            )

        move_to(engine, PREVIOUS, down=True)

        assert current_revision(engine) == PREVIOUS
        assert table_names(engine) == before
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT symbol FROM tickers").scalars().all() == [
                "AAPL"
            ]
    finally:
        engine.dispose()

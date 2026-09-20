"""Revision ``0003``: the signal history and the bot state (spec 014, T2, AC6).

Every database lives under ``tmp_path`` and is opened through the factory engine. The upgrade
from ``0002`` runs on a database that already holds configuration rows, as production does, and
the downgrade must drop exactly the two new tables and leave every configuration row intact.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import (
    alembic_config,
    current_revision,
    head_revision,
    run_migrations,
)
from trading_bot.persistence.models import Base

PREVIOUS = "0002"
HEAD = "0003"
REVISION_ID = re.compile(r"^[0-9]{4}$")
MIGRATOR_LOGGER = "trading_bot.persistence.migrator"
CONFIGURATION_TABLES = ["rules", "ticker_rules", "tickers"]
ALL_TABLES = ["alembic_version", "bot_state", "rules", "signals", "ticker_rules", "tickers"]
SIGNAL_INDEXES = ["ix_signals_candle_close_ts", "ix_signals_rule_id_candle_close_ts"]
TIMESTAMP = "2026-01-02 03:04:05.000000"

# Every configuration row, in a stable text form, so "intact" is one comparison.
CONFIGURATION_QUERIES = (
    "SELECT id, symbol, timeframe, enabled, created_at FROM tickers ORDER BY id",
    "SELECT id, name, signal, timeframe, definition_json, enabled, created_at, updated_at"
    " FROM rules ORDER BY id",
    "SELECT ticker_id, rule_id, timeframe, created_at FROM ticker_rules"
    " ORDER BY ticker_id, rule_id",
)


def fresh_engine(tmp_path: Path) -> Engine:
    return create_database_engine(database_path(tmp_path), busy_timeout_ms=200)


def table_names(engine: Engine) -> list[str]:
    """The tables SQLite holds, without the internal ones (``sqlite_sequence``)."""
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite~_%' ESCAPE '~' ORDER BY name"
        )
        return [str(name) for name in rows.scalars().all()]


def index_names(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name"
        )
        return [str(name) for name in rows.scalars().all()]


def stored_sql(engine: Engine, name: str) -> str:
    with engine.connect() as connection:
        found = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).scalar()
    assert isinstance(found, str), f"{name} is missing from sqlite_master"
    return " ".join(found.split())


def stored_revisions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        return [str(value) for value in rows.scalars().all()]


def migrate_to(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade_to(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def seed_configuration(connection: Connection) -> None:
    connection.exec_driver_sql(
        "INSERT INTO tickers (symbol, timeframe, enabled, created_at)"
        " VALUES ('AAPL', '1d', 1, ?), ('MSFT', '1h', 0, ?)",
        (TIMESTAMP, TIMESTAMP),
    )
    connection.exec_driver_sql(
        "INSERT INTO rules"
        " (name, signal, timeframe, definition_json, enabled, created_at, updated_at)"
        " VALUES ('daily', 'BUY', '1d', '{}', 1, ?, ?)",
        (TIMESTAMP, TIMESTAMP),
    )
    connection.exec_driver_sql(
        "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
        " VALUES (1, 1, '1d', ?)",
        (TIMESTAMP,),
    )


def seed_history(connection: Connection) -> None:
    connection.exec_driver_sql(
        "INSERT INTO signals (ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at, notified_at)"
        " VALUES (1, 1, '1d', '2024-01-03 05:00:00.000000', 'BUY', 187.5, '{}', ?, NULL)",
        (TIMESTAMP,),
    )
    connection.exec_driver_sql(
        "INSERT INTO bot_state (\"key\", value, updated_at) VALUES ('paused_since', ?, ?)",
        (TIMESTAMP, TIMESTAMP),
    )


def configuration(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            repr(tuple(row))
            for query in CONFIGURATION_QUERIES
            for row in connection.exec_driver_sql(query)
        ]


def migrator_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == MIGRATOR_LOGGER]


# --- the revision chain --------------------------------------------------------------------


def test_the_head_is_0003_and_it_follows_0002() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD]
    assert head_revision() == HEAD
    assert script.get_revision(HEAD).down_revision == PREVIOUS
    assert REVISION_ID.match(HEAD)


# --- upgrade -------------------------------------------------------------------------------


def test_an_empty_database_is_upgraded_to_0003(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = fresh_engine(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger=MIGRATOR_LOGGER):
            assert run_migrations(engine) == HEAD

        assert current_revision(engine) == HEAD
        assert stored_revisions(engine) == [HEAD]
        assert table_names(engine) == ALL_TABLES
        assert migrator_messages(caplog) == ["database schema upgraded from empty to 0003"]
    finally:
        engine.dispose()


def test_a_second_run_is_a_no_op(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger=MIGRATOR_LOGGER):
            assert run_migrations(engine) == HEAD

        assert stored_revisions(engine) == [HEAD]
        assert table_names(engine) == ALL_TABLES
        assert migrator_messages(caplog) == ["database schema already at revision 0003"]
    finally:
        engine.dispose()


def test_upgrading_a_0002_database_keeps_its_configuration(tmp_path: Path) -> None:
    """The production path: a database with rows at ``0002`` gains the two empty tables."""
    engine = fresh_engine(tmp_path)
    try:
        migrate_to(engine, PREVIOUS)
        with engine.begin() as connection:
            seed_configuration(connection)
        before = configuration(engine)

        assert run_migrations(engine) == HEAD

        assert configuration(engine) == before
        assert table_names(engine) == ALL_TABLES
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM signals").scalar() == 0
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM bot_state").scalar() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["signals", "bot_state"])
def test_the_upgrade_creates_the_tables_the_models_declare(tmp_path: Path, table: str) -> None:
    """The revision is self-contained, so its DDL is compared with the models' own."""
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)

        rendered = str(CreateTable(Base.metadata.tables[table]).compile(dialect=engine.dialect))
        assert stored_sql(engine, table) == " ".join(rendered.split())
    finally:
        engine.dispose()


def test_the_upgrade_creates_the_indexes_the_models_declare(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)

        for index in Base.metadata.tables["signals"].indexes:
            rendered = str(CreateIndex(index).compile(dialect=engine.dialect))
            assert stored_sql(engine, str(index.name)) == " ".join(rendered.split())
        assert index_names(engine) == [*SIGNAL_INDEXES, "ix_ticker_rules_rule_id"]
    finally:
        engine.dispose()


# --- downgrade -----------------------------------------------------------------------------


def test_downgrade_to_0002_drops_exactly_the_two_tables(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        with engine.begin() as connection:
            seed_configuration(connection)
            seed_history(connection)
        before = configuration(engine)

        downgrade_to(engine, PREVIOUS)

        assert current_revision(engine) == PREVIOUS
        assert table_names(engine) == ["alembic_version", *CONFIGURATION_TABLES]
        assert index_names(engine) == ["ix_ticker_rules_rule_id"]
        assert configuration(engine) == before
    finally:
        engine.dispose()


def test_downgrade_to_base_then_upgrade_head_returns_to_0003(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        with engine.begin() as connection:
            seed_configuration(connection)
            seed_history(connection)

        downgrade_to(engine, "base")

        assert table_names(engine) == ["alembic_version"]
        assert stored_revisions(engine) == []
        assert current_revision(engine) is None

        assert run_migrations(engine) == HEAD
        assert table_names(engine) == ALL_TABLES
        assert index_names(engine) == [*SIGNAL_INDEXES, "ix_ticker_rules_rule_id"]
    finally:
        engine.dispose()


def test_upgrade_head_after_a_downgrade_to_0002_restores_empty_tables(tmp_path: Path) -> None:
    engine = fresh_engine(tmp_path)
    try:
        run_migrations(engine)
        with engine.begin() as connection:
            seed_configuration(connection)
            seed_history(connection)
        downgrade_to(engine, PREVIOUS)

        assert run_migrations(engine) == HEAD

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM signals").scalar() == 0
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM bot_state").scalar() == 0
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM tickers").scalar() == 2
    finally:
        engine.dispose()

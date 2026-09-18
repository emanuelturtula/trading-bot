"""The three configuration tables and their DDL (spec 013, T1, AC1-AC4).

The expected DDL is written out literally, as spec 013 Design 3 renders it, so a change to a
model, to the naming convention or to a ``Timeframe``/``Side`` member fails here instead of
silently diverging from the database. Nothing is recomputed with the code under test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, inspect
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError

from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe, UnknownTimeframeError
from trading_bot.persistence.base import Base
from trading_bot.persistence.database import Database
from trading_bot.persistence.models import RuleRow, TickerRow, TickerRuleRow
from trading_bot.persistence.types import SideType, TimeframeType

TIMESTAMP = "2026-01-02 03:04:05.000000"

EXPECTED_DDL = {
    "tickers": """
CREATE TABLE tickers (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_tickers_symbol_timeframe UNIQUE (symbol, timeframe),
    CONSTRAINT uq_tickers_id_timeframe UNIQUE (id, timeframe),
    CONSTRAINT ck_tickers_timeframe CHECK (timeframe IN ('1h', '4h', '1d'))
)
""",
    "rules": """
CREATE TABLE rules (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(80) NOT NULL,
    signal VARCHAR(4) NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    definition_json TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_rules_name UNIQUE (name),
    CONSTRAINT uq_rules_id_timeframe UNIQUE (id, timeframe),
    CONSTRAINT ck_rules_timeframe CHECK (timeframe IN ('1h', '4h', '1d')),
    CONSTRAINT ck_rules_signal CHECK (signal IN ('BUY', 'SELL'))
)
""",
    "ticker_rules": """
CREATE TABLE ticker_rules (
    ticker_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT pk_ticker_rules PRIMARY KEY (ticker_id, rule_id),
    CONSTRAINT fk_ticker_rules_ticker_id_timeframe_tickers
        FOREIGN KEY(ticker_id, timeframe) REFERENCES tickers (id, timeframe) ON DELETE CASCADE,
    CONSTRAINT fk_ticker_rules_rule_id_timeframe_rules
        FOREIGN KEY(rule_id, timeframe) REFERENCES rules (id, timeframe) ON DELETE CASCADE
)
""",
}

EXPECTED_INDEX_DDL = "CREATE INDEX ix_ticker_rules_rule_id ON ticker_rules (rule_id)"


def collapsed(text: str) -> str:
    return " ".join(text.split())


def stored_ddl(connection: Connection, name: str) -> str:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).scalar()
    assert isinstance(row, str), f"{name} is missing from sqlite_master"
    return collapsed(row)


def insert_ticker(connection: Connection, symbol: str, timeframe: str = "1d") -> int:
    connection.exec_driver_sql(
        "INSERT INTO tickers (symbol, timeframe, enabled, created_at) VALUES (?, ?, 1, ?)",
        (symbol, timeframe, TIMESTAMP),
    )
    identifier = connection.exec_driver_sql("SELECT last_insert_rowid()").scalar()
    assert isinstance(identifier, int)
    return identifier


def insert_rule(
    connection: Connection, name: str, timeframe: str = "1d", signal: str = "BUY"
) -> int:
    connection.exec_driver_sql(
        "INSERT INTO rules"
        " (name, signal, timeframe, definition_json, enabled, created_at, updated_at)"
        " VALUES (?, ?, ?, '{}', 0, ?, ?)",
        (name, signal, timeframe, TIMESTAMP, TIMESTAMP),
    )
    identifier = connection.exec_driver_sql("SELECT last_insert_rowid()").scalar()
    assert isinstance(identifier, int)
    return identifier


# --- AC3: the column types at the boundary (Design 4) --------------------------------------

DIALECT = sqlite.dialect()


def test_a_timeframe_binds_as_its_code_and_comes_back_as_a_member() -> None:
    column = TimeframeType()

    assert column.process_bind_param(Timeframe.H4, DIALECT) == "4h"
    assert column.process_result_value("4h", DIALECT) is Timeframe.H4


def test_a_side_binds_as_its_code_and_comes_back_as_a_member() -> None:
    column = SideType()

    assert column.process_bind_param(Side.SELL, DIALECT) == "SELL"
    assert column.process_result_value("SELL", DIALECT) is Side.SELL


def test_none_round_trips_through_both_column_types() -> None:
    for column in (TimeframeType(), SideType()):
        assert column.process_bind_param(None, DIALECT) is None
        assert column.process_result_value(None, DIALECT) is None


def test_a_raw_string_cannot_reach_either_column_through_the_orm() -> None:
    with pytest.raises(TypeError, match="Timeframe"):
        TimeframeType().process_bind_param("1d", DIALECT)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Side"):
        SideType().process_bind_param("BUY", DIALECT)  # type: ignore[arg-type]


def test_text_the_database_should_never_hold_raises_at_the_boundary() -> None:
    with pytest.raises(UnknownTimeframeError):
        TimeframeType().process_result_value("60m", DIALECT)
    with pytest.raises(ValueError, match="HOLD"):
        SideType().process_result_value("HOLD", DIALECT)


# --- AC1: the tables, their columns and their constraints ----------------------------------


def test_the_metadata_holds_exactly_the_three_configuration_tables() -> None:
    assert sorted(Base.metadata.tables) == ["rules", "ticker_rules", "tickers"]


def test_the_model_classes_map_to_the_three_tables() -> None:
    names = (TickerRow.__tablename__, RuleRow.__tablename__, TickerRuleRow.__tablename__)

    assert names == ("tickers", "rules", "ticker_rules")


def test_the_migrated_database_holds_exactly_the_three_tables(database: Database) -> None:
    names = inspect(database.engine).get_table_names()

    assert sorted(names) == ["alembic_version", "rules", "ticker_rules", "tickers"]


@pytest.mark.parametrize("table", ["tickers", "rules", "ticker_rules"])
def test_the_stored_ddl_matches_the_specification(database: Database, table: str) -> None:
    with database.engine.connect() as connection:
        assert stored_ddl(connection, table) == collapsed(EXPECTED_DDL[table])


def test_the_only_index_is_the_reverse_lookup_of_the_join_table(database: Database) -> None:
    with database.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            " ORDER BY name"
        ).all()

    assert [str(row[0]) for row in rows] == ["ix_ticker_rules_rule_id"]
    assert collapsed(str(rows[0][1])) == EXPECTED_INDEX_DDL


def test_the_reflected_columns_carry_the_declared_types_and_nullability(
    database: Database,
) -> None:
    inspector = inspect(database.engine)
    reflected = {
        table: [
            (column["name"], str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        ]
        for table in ("tickers", "rules", "ticker_rules")
    }

    assert reflected == {
        "tickers": [
            ("id", "INTEGER", False),
            ("symbol", "VARCHAR(32)", False),
            ("timeframe", "VARCHAR(2)", False),
            ("enabled", "BOOLEAN", False),
            ("created_at", "DATETIME", False),
        ],
        "rules": [
            ("id", "INTEGER", False),
            ("name", "VARCHAR(80)", False),
            ("signal", "VARCHAR(4)", False),
            ("timeframe", "VARCHAR(2)", False),
            ("definition_json", "TEXT", False),
            ("enabled", "BOOLEAN", False),
            ("created_at", "DATETIME", False),
            ("updated_at", "DATETIME", False),
        ],
        "ticker_rules": [
            ("ticker_id", "INTEGER", False),
            ("rule_id", "INTEGER", False),
            ("timeframe", "VARCHAR(2)", False),
            ("created_at", "DATETIME", False),
        ],
    }


def test_the_reflected_unique_constraints_have_the_expected_names(database: Database) -> None:
    inspector = inspect(database.engine)
    found = {
        table: sorted(
            (str(constraint["name"]), tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table)
        )
        for table in ("tickers", "rules", "ticker_rules")
    }

    assert found == {
        "tickers": [
            ("uq_tickers_id_timeframe", ("id", "timeframe")),
            ("uq_tickers_symbol_timeframe", ("symbol", "timeframe")),
        ],
        "rules": [
            ("uq_rules_id_timeframe", ("id", "timeframe")),
            ("uq_rules_name", ("name",)),
        ],
        "ticker_rules": [],
    }


def test_the_reflected_primary_keys_have_the_expected_columns(database: Database) -> None:
    inspector = inspect(database.engine)
    found = {
        table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        for table in ("tickers", "rules", "ticker_rules")
    }

    assert found == {
        "tickers": ("id",),
        "rules": ("id",),
        "ticker_rules": ("ticker_id", "rule_id"),
    }


def test_the_composite_foreign_keys_cascade_on_delete(database: Database) -> None:
    found = sorted(
        (
            str(key["name"]),
            tuple(key["constrained_columns"]),
            str(key["referred_table"]),
            tuple(key["referred_columns"]),
            str(key["options"]["ondelete"]),
        )
        for key in inspect(database.engine).get_foreign_keys("ticker_rules")
    )

    assert found == [
        (
            "fk_ticker_rules_rule_id_timeframe_rules",
            ("rule_id", "timeframe"),
            "rules",
            ("id", "timeframe"),
            "CASCADE",
        ),
        (
            "fk_ticker_rules_ticker_id_timeframe_tickers",
            ("ticker_id", "timeframe"),
            "tickers",
            ("id", "timeframe"),
            "CASCADE",
        ),
    ]


# --- AC2: identity is never reused ---------------------------------------------------------


@pytest.mark.parametrize("table", ["tickers", "rules"])
def test_the_ddl_of_both_identity_tables_carries_autoincrement(
    database: Database, table: str
) -> None:
    with database.engine.connect() as connection:
        assert "AUTOINCREMENT" in stored_ddl(connection, table)


def test_a_ticker_identifier_is_not_reused_after_a_delete(database: Database) -> None:
    with database.engine.begin() as connection:
        first = insert_ticker(connection, "AAPL")
        second = insert_ticker(connection, "MSFT")
        connection.exec_driver_sql("DELETE FROM tickers WHERE id = ?", (second,))
        third = insert_ticker(connection, "NVDA")

    assert (first, second) == (1, 2)
    assert third == 3


def test_a_rule_identifier_is_not_reused_after_a_delete(database: Database) -> None:
    with database.engine.begin() as connection:
        first = insert_rule(connection, "first")
        second = insert_rule(connection, "second")
        connection.exec_driver_sql("DELETE FROM rules WHERE id = ?", (second,))
        third = insert_rule(connection, "third")

    assert (first, second) == (1, 2)
    assert third == 3


# --- AC3: the codes are checked by the database --------------------------------------------


def test_the_check_expressions_are_built_from_the_enumerations(database: Database) -> None:
    timeframes = ", ".join(f"'{member.value}'" for member in Timeframe)
    sides = ", ".join(f"'{member.value}'" for member in Side)

    with database.engine.connect() as connection:
        tickers = stored_ddl(connection, "tickers")
        rules = stored_ddl(connection, "rules")

    assert f"CHECK (timeframe IN ({timeframes}))" in tickers
    assert f"CHECK (timeframe IN ({timeframes}))" in rules
    assert f"CHECK (signal IN ({sides}))" in rules


def test_a_ticker_with_an_unknown_timeframe_is_refused_by_the_database(
    database: Database,
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_tickers_timeframe"),
        database.engine.begin() as connection,
    ):
        insert_ticker(connection, "AAPL", timeframe="1D")


def test_a_rule_with_an_unknown_timeframe_is_refused_by_the_database(database: Database) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_rules_timeframe"),
        database.engine.begin() as connection,
    ):
        insert_rule(connection, "hourly", timeframe="60m")


def test_a_rule_with_an_unknown_signal_is_refused_by_the_database(database: Database) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_rules_signal"),
        database.engine.begin() as connection,
    ):
        insert_rule(connection, "hold", signal="HOLD")


# --- AC4: uniqueness -----------------------------------------------------------------------


def test_one_symbol_may_be_tracked_on_two_timeframes(database: Database) -> None:
    with database.engine.begin() as connection:
        insert_ticker(connection, "AAPL", timeframe="1d")
        insert_ticker(connection, "AAPL", timeframe="1h")

        count = connection.exec_driver_sql("SELECT COUNT(*) FROM tickers").scalar()

    assert count == 2


def test_the_same_symbol_and_timeframe_twice_is_refused(database: Database) -> None:
    with database.engine.begin() as connection:
        insert_ticker(connection, "AAPL")

    with (
        pytest.raises(IntegrityError, match=r"tickers\.symbol"),
        database.engine.begin() as connection,
    ):
        insert_ticker(connection, "AAPL")


def test_rule_names_are_compared_with_the_default_binary_collation(database: Database) -> None:
    """``'daily'`` and ``'DAILY'`` are two different rules (decision D85)."""
    with database.engine.begin() as connection:
        insert_rule(connection, "daily")
        insert_rule(connection, "DAILY")

        count = connection.exec_driver_sql("SELECT COUNT(*) FROM rules").scalar()

    assert count == 2


def test_the_same_rule_name_twice_is_refused(database: Database) -> None:
    with database.engine.begin() as connection:
        insert_rule(connection, "daily")

    with (
        pytest.raises(IntegrityError, match=r"rules\.name"),
        database.engine.begin() as connection,
    ):
        insert_rule(connection, "daily")

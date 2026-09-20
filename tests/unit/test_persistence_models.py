"""The five tables and their DDL (spec 013, T1, AC1-AC4; spec 014, T1, AC1-AC5).

The expected DDL is written out literally, as spec 013 Design 3 and spec 014 Design 3 render
it, so a change to a model, to the naming convention or to a ``Timeframe``, ``Side`` or
``StateKey`` member fails here instead of silently diverging from the database. Nothing is
recomputed with the code under test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, inspect
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from tests.fixtures.repositories import repositories, sample_rule
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe, UnknownTimeframeError
from trading_bot.persistence.base import Base
from trading_bot.persistence.database import Database
from trading_bot.persistence.models import (
    BotStateRow,
    RuleRow,
    SignalRow,
    TickerRow,
    TickerRuleRow,
)
from trading_bot.persistence.state import StateKey
from trading_bot.persistence.types import SideType, StateKeyType, TimeframeType

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

EXPECTED_DDL["signals"] = """
CREATE TABLE signals (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ticker_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    timeframe VARCHAR(2) NOT NULL,
    candle_close_ts DATETIME NOT NULL,
    side VARCHAR(4) NOT NULL,
    close_price FLOAT NOT NULL,
    indicator_values_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    notified_at DATETIME,
    CONSTRAINT uq_signals_ticker_id_rule_id_timeframe_candle_close_ts
        UNIQUE (ticker_id, rule_id, timeframe, candle_close_ts),
    CONSTRAINT fk_signals_ticker_id_timeframe_tickers
        FOREIGN KEY(ticker_id, timeframe) REFERENCES tickers (id, timeframe) ON DELETE CASCADE,
    CONSTRAINT fk_signals_rule_id_rules
        FOREIGN KEY(rule_id) REFERENCES rules (id) ON DELETE CASCADE,
    CONSTRAINT ck_signals_timeframe CHECK (timeframe IN ('1h', '4h', '1d')),
    CONSTRAINT ck_signals_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT ck_signals_close_price CHECK (close_price > 0)
)
"""

EXPECTED_DDL["bot_state"] = """
CREATE TABLE bot_state (
    "key" VARCHAR(32) NOT NULL,
    value DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT pk_bot_state PRIMARY KEY ("key"),
    CONSTRAINT ck_bot_state_key CHECK (key IN ('paused_since', 'last_heartbeat',
        'last_run.1h', 'last_run.4h', 'last_run.1d'))
)
"""

EXPECTED_INDEX_DDL = {
    "ix_signals_candle_close_ts": (
        "CREATE INDEX ix_signals_candle_close_ts ON signals (candle_close_ts)"
    ),
    "ix_signals_rule_id_candle_close_ts": (
        "CREATE INDEX ix_signals_rule_id_candle_close_ts ON signals (rule_id, candle_close_ts)"
    ),
    "ix_ticker_rules_rule_id": "CREATE INDEX ix_ticker_rules_rule_id ON ticker_rules (rule_id)",
}


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


CLOSE = "2024-01-03 05:00:00.000000"
VALUES_JSON = '{"rsi(length=14).value":28.4}'


def insert_signal(
    connection: Connection,
    ticker_id: int,
    rule_id: int,
    *,
    timeframe: str = "1d",
    candle_close_ts: str = CLOSE,
    side: str = "BUY",
    close_price: float = 187.5,
) -> int:
    connection.exec_driver_sql(
        "INSERT INTO signals (ticker_id, rule_id, timeframe, candle_close_ts, side,"
        " close_price, indicator_values_json, created_at, notified_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (ticker_id, rule_id, timeframe, candle_close_ts, side, close_price, VALUES_JSON, TIMESTAMP),
    )
    identifier = connection.exec_driver_sql("SELECT last_insert_rowid()").scalar()
    assert isinstance(identifier, int)
    return identifier


def insert_state(connection: Connection, key: str) -> None:
    connection.exec_driver_sql(
        'INSERT INTO bot_state ("key", value, updated_at) VALUES (?, ?, ?)',
        (key, TIMESTAMP, TIMESTAMP),
    )


def count_rows(connection: Connection, table: str) -> int:
    found = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()  # noqa: S608
    assert isinstance(found, int)
    return found


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


def test_a_state_key_binds_as_its_text_and_comes_back_as_a_member() -> None:
    """Spec 014 Design 4: the same boundary rules as ``TimeframeType``."""
    column = StateKeyType()

    assert column.process_bind_param(StateKey.LAST_RUN_1D, DIALECT) == "last_run.1d"
    assert column.process_result_value("paused_since", DIALECT) is StateKey.PAUSED_SINCE
    assert column.process_bind_param(None, DIALECT) is None
    assert column.process_result_value(None, DIALECT) is None


def test_a_raw_string_cannot_reach_the_state_key_column_through_the_orm() -> None:
    with pytest.raises(TypeError, match="StateKey"):
        StateKeyType().process_bind_param("paused_since", DIALECT)  # type: ignore[arg-type]


def test_a_state_key_the_database_should_never_hold_raises_at_the_boundary() -> None:
    with pytest.raises(ValueError, match=r"last_run\.1w"):
        StateKeyType().process_result_value("last_run.1w", DIALECT)


# --- AC1: the tables, their columns and their constraints ----------------------------------


def test_the_metadata_holds_exactly_the_five_tables() -> None:
    assert sorted(Base.metadata.tables) == [
        "bot_state",
        "rules",
        "signals",
        "ticker_rules",
        "tickers",
    ]


def test_the_model_classes_map_to_the_five_tables() -> None:
    names = (
        TickerRow.__tablename__,
        RuleRow.__tablename__,
        TickerRuleRow.__tablename__,
        SignalRow.__tablename__,
        BotStateRow.__tablename__,
    )

    assert names == ("tickers", "rules", "ticker_rules", "signals", "bot_state")


def test_the_migrated_database_holds_exactly_the_five_tables(database: Database) -> None:
    names = inspect(database.engine).get_table_names()

    assert sorted(names) == [
        "alembic_version",
        "bot_state",
        "rules",
        "signals",
        "ticker_rules",
        "tickers",
    ]


@pytest.mark.parametrize("table", ["tickers", "rules", "ticker_rules", "signals", "bot_state"])
def test_the_stored_ddl_matches_the_specification(database: Database, table: str) -> None:
    with database.engine.connect() as connection:
        assert stored_ddl(connection, table) == collapsed(EXPECTED_DDL[table])


@pytest.mark.parametrize("table", ["tickers", "rules", "ticker_rules", "signals", "bot_state"])
def test_the_models_render_the_same_ddl_as_the_migration(table: str) -> None:
    """The revision is self-contained, so this is what keeps it and the models in step."""
    rendered = str(CreateTable(Base.metadata.tables[table]).compile(dialect=DIALECT))

    assert collapsed(rendered) == collapsed(EXPECTED_DDL[table])


def test_the_indexes_are_the_join_lookup_and_the_two_signal_indexes(database: Database) -> None:
    with database.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            " ORDER BY name"
        ).all()

    assert {str(row[0]): collapsed(str(row[1])) for row in rows} == EXPECTED_INDEX_DDL


def test_the_models_declare_the_same_indexes() -> None:
    rendered = {
        str(index.name): collapsed(str(CreateIndex(index).compile(dialect=DIALECT)))
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }

    assert rendered == EXPECTED_INDEX_DDL


def test_the_reflected_columns_carry_the_declared_types_and_nullability(
    database: Database,
) -> None:
    inspector = inspect(database.engine)
    reflected = {
        table: [
            (column["name"], str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        ]
        for table in ("tickers", "rules", "ticker_rules", "signals", "bot_state")
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
        "signals": [
            ("id", "INTEGER", False),
            ("ticker_id", "INTEGER", False),
            ("rule_id", "INTEGER", False),
            ("timeframe", "VARCHAR(2)", False),
            ("candle_close_ts", "DATETIME", False),
            ("side", "VARCHAR(4)", False),
            ("close_price", "FLOAT", False),
            ("indicator_values_json", "TEXT", False),
            ("created_at", "DATETIME", False),
            ("notified_at", "DATETIME", True),
        ],
        "bot_state": [
            ("key", "VARCHAR(32)", False),
            ("value", "DATETIME", False),
            ("updated_at", "DATETIME", False),
        ],
    }


def test_the_reflected_unique_constraints_have_the_expected_names(database: Database) -> None:
    inspector = inspect(database.engine)
    found = {
        table: sorted(
            (str(constraint["name"]), tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table)
        )
        for table in ("tickers", "rules", "ticker_rules", "signals", "bot_state")
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
        "signals": [
            (
                "uq_signals_ticker_id_rule_id_timeframe_candle_close_ts",
                ("ticker_id", "rule_id", "timeframe", "candle_close_ts"),
            ),
        ],
        "bot_state": [],
    }


def test_the_reflected_primary_keys_have_the_expected_columns(database: Database) -> None:
    inspector = inspect(database.engine)
    found = {
        table: tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        for table in ("tickers", "rules", "ticker_rules", "signals", "bot_state")
    }

    assert found == {
        "tickers": ("id",),
        "rules": ("id",),
        "ticker_rules": ("ticker_id", "rule_id"),
        "signals": ("id",),
        "bot_state": ("key",),
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


def test_the_signal_foreign_keys_cascade_on_delete(database: Database) -> None:
    """Spec 014 D113: composite to the ticker, plain to the rule, both ``CASCADE``."""
    found = sorted(
        (
            str(key["name"]),
            tuple(key["constrained_columns"]),
            str(key["referred_table"]),
            tuple(key["referred_columns"]),
            str(key["options"]["ondelete"]),
        )
        for key in inspect(database.engine).get_foreign_keys("signals")
    )

    assert found == [
        ("fk_signals_rule_id_rules", ("rule_id",), "rules", ("id",), "CASCADE"),
        (
            "fk_signals_ticker_id_timeframe_tickers",
            ("ticker_id", "timeframe"),
            "tickers",
            ("id", "timeframe"),
            "CASCADE",
        ),
    ]


def test_the_bot_state_table_has_no_foreign_key(database: Database) -> None:
    assert inspect(database.engine).get_foreign_keys("bot_state") == []


# --- AC2: identity is never reused ---------------------------------------------------------


@pytest.mark.parametrize("table", ["tickers", "rules", "signals"])
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


def test_a_signal_identifier_is_not_reused_after_the_highest_is_deleted(
    database: Database,
) -> None:
    """Spec 014 AC3: a signal id in a cursor or a Telegram reference never changes meaning."""
    with database.engine.begin() as connection:
        ticker = insert_ticker(connection, "AAPL")
        rule = insert_rule(connection, "daily")
        first = insert_signal(connection, ticker, rule, candle_close_ts="2024-01-02 05:00:00")
        second = insert_signal(connection, ticker, rule, candle_close_ts="2024-01-03 05:00:00")
        connection.exec_driver_sql("DELETE FROM signals WHERE id = ?", (second,))
        third = insert_signal(connection, ticker, rule, candle_close_ts="2024-01-03 05:00:00")

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


def test_the_signal_and_state_checks_are_built_from_the_enumerations(database: Database) -> None:
    """Spec 014 AC4: a new member changes the DDL, which fails the golden assertion."""
    timeframes = ", ".join(f"'{member.value}'" for member in Timeframe)
    sides = ", ".join(f"'{member.value}'" for member in Side)
    keys = ", ".join(f"'{member.value}'" for member in StateKey)

    with database.engine.connect() as connection:
        signals = stored_ddl(connection, "signals")
        state = stored_ddl(connection, "bot_state")

    assert f"CHECK (timeframe IN ({timeframes}))" in signals
    assert f"CHECK (side IN ({sides}))" in signals
    assert "CHECK (close_price > 0)" in signals
    assert f"CHECK (key IN ({keys}))" in state


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


# --- Spec 014 AC4: the signal and bot-state checks refuse raw rows -------------------------


@pytest.mark.parametrize(
    ("timeframe", "side", "close_price", "constraint"),
    [
        ("1D", "BUY", 187.5, "ck_signals_timeframe"),
        ("1d", "HOLD", 187.5, "ck_signals_side"),
        ("1d", "BUY", 0.0, "ck_signals_close_price"),
        ("1d", "BUY", -1.5, "ck_signals_close_price"),
    ],
)
def test_a_signal_that_breaks_a_check_is_refused_by_the_database(
    database: Database, timeframe: str, side: str, close_price: float, constraint: str
) -> None:
    with database.engine.begin() as connection:
        ticker = insert_ticker(connection, "AAPL")
        rule = insert_rule(connection, "daily")

    with pytest.raises(IntegrityError, match=constraint), database.engine.begin() as connection:
        insert_signal(
            connection, ticker, rule, timeframe=timeframe, side=side, close_price=close_price
        )


@pytest.mark.parametrize("key", ["paused", "last_run.1w", "PAUSED_SINCE", ""])
def test_a_state_key_outside_the_vocabulary_is_refused_by_the_database(
    database: Database, key: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_bot_state_key"), database.engine.begin() as c:
        insert_state(c, key)


def test_every_state_key_of_the_vocabulary_is_accepted_once(database: Database) -> None:
    with database.engine.begin() as connection:
        for key in ("paused_since", "last_heartbeat", "last_run.1h", "last_run.4h", "last_run.1d"):
            insert_state(connection, key)

    with (
        pytest.raises(IntegrityError, match=r"bot_state\.key"),
        database.engine.begin() as connection,
    ):
        insert_state(connection, "paused_since")


# --- Spec 014 AC2: the signal identity is unique (rule 5) ----------------------------------


def test_the_same_signal_key_twice_is_refused(database: Database) -> None:
    with database.engine.begin() as connection:
        ticker = insert_ticker(connection, "AAPL")
        rule = insert_rule(connection, "daily")
        insert_signal(connection, ticker, rule)

    with (
        pytest.raises(IntegrityError, match=r"signals\.ticker_id, signals\.rule_id"),
        database.engine.begin() as connection,
    ):
        # A different payload does not make it a different signal.
        insert_signal(connection, ticker, rule, side="SELL", close_price=1.0)


def test_a_key_that_differs_in_any_one_column_is_a_different_signal(database: Database) -> None:
    with database.engine.begin() as connection:
        ticker = insert_ticker(connection, "AAPL")
        other_ticker = insert_ticker(connection, "MSFT")
        hourly_ticker = insert_ticker(connection, "AAPL", timeframe="1h")
        rule = insert_rule(connection, "daily")
        other_rule = insert_rule(connection, "other")
        insert_signal(connection, ticker, rule)
        insert_signal(connection, other_ticker, rule)
        insert_signal(connection, ticker, other_rule)
        insert_signal(connection, hourly_ticker, rule, timeframe="1h")
        insert_signal(connection, ticker, rule, candle_close_ts="2024-01-03 05:00:00.000001")

        assert count_rows(connection, "signals") == 5


# --- Spec 014 AC5: foreign keys and the delete cascade (D113) ------------------------------


def seed_two_pairs(connection: Connection) -> tuple[int, int, int, int]:
    """Two tickers and two rules, all assigned, with one signal per pair and a pause."""
    aapl = insert_ticker(connection, "AAPL")
    msft = insert_ticker(connection, "MSFT")
    first = insert_rule(connection, "first")
    second = insert_rule(connection, "second")
    for ticker in (aapl, msft):
        for rule in (first, second):
            connection.exec_driver_sql(
                "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
                " VALUES (?, ?, '1d', ?)",
                (ticker, rule, TIMESTAMP),
            )
            insert_signal(connection, ticker, rule)
    insert_state(connection, "paused_since")
    return aapl, msft, first, second


def signal_pairs(connection: Connection) -> list[tuple[int, int]]:
    rows = connection.exec_driver_sql(
        "SELECT ticker_id, rule_id FROM signals ORDER BY ticker_id, rule_id"
    ).all()
    return [(int(row[0]), int(row[1])) for row in rows]


def test_deleting_a_ticker_deletes_its_signals_and_nothing_else(database: Database) -> None:
    with database.engine.begin() as connection:
        aapl, msft, first, second = seed_two_pairs(connection)

        connection.exec_driver_sql("DELETE FROM tickers WHERE id = ?", (aapl,))

        assert signal_pairs(connection) == [(msft, first), (msft, second)]
        assert count_rows(connection, "ticker_rules") == 2
        assert count_rows(connection, "rules") == 2
        assert count_rows(connection, "bot_state") == 1


def test_deleting_a_rule_deletes_its_signals_and_nothing_else(database: Database) -> None:
    with database.engine.begin() as connection:
        aapl, msft, first, second = seed_two_pairs(connection)

        connection.exec_driver_sql("DELETE FROM rules WHERE id = ?", (first,))

        assert signal_pairs(connection) == [(aapl, second), (msft, second)]
        assert count_rows(connection, "ticker_rules") == 2
        assert count_rows(connection, "tickers") == 2
        assert count_rows(connection, "bot_state") == 1


@pytest.mark.parametrize(
    ("ticker_offset", "rule_offset", "timeframe"),
    [
        pytest.param(404, 0, "1d", id="missing-ticker"),
        pytest.param(0, 404, "1d", id="missing-rule"),
        pytest.param(0, 0, "1h", id="timeframe-differs-from-the-ticker"),
    ],
)
def test_a_signal_without_a_matching_parent_is_refused(
    database: Database, ticker_offset: int, rule_offset: int, timeframe: str
) -> None:
    with database.engine.begin() as connection:
        ticker = insert_ticker(connection, "AAPL")
        rule = insert_rule(connection, "daily")

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY"),
        database.engine.begin() as connection,
    ):
        insert_signal(connection, ticker + ticker_offset, rule + rule_offset, timeframe=timeframe)


def test_a_rule_with_signals_but_no_assignment_may_change_timeframe(database: Database) -> None:
    """D113: the rule key is not composite, so the history keeps its original timeframe."""
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule("Daily breakout"))
    with database.engine.begin() as connection:
        insert_signal(connection, ticker.id, rule.id)

    with database.session() as session:
        replaced = repositories(session).rules.replace(
            rule.id, sample_rule("Daily breakout", timeframe="1h")
        )

    assert replaced.rule.timeframe is Timeframe.H1
    with database.engine.connect() as connection:
        stored = connection.exec_driver_sql("SELECT timeframe FROM signals").scalars().all()
    assert list(stored) == ["1d"]

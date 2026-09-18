"""The UTC column type and the declarative metadata (spec 012, T4, T5, AC8, AC9)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import StatementError
from sqlalchemy.schema import CreateIndex, CreateTable

from trading_bot.persistence import base, models
from trading_bot.persistence.base import NAMING_CONVENTION, Base
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.types import UtcDateTime

NEW_YORK = ZoneInfo("America/New_York")
STORED_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$")


def sample_table() -> tuple[MetaData, Table]:
    metadata = MetaData()  # never Base.metadata: this feature ships no table
    table = Table(
        "instant",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("moment", UtcDateTime, nullable=True),
    )
    return metadata, table


def instants_engine(tmp_path: Path) -> tuple[Engine, Table]:
    engine = create_database_engine(database_path(tmp_path))
    metadata, table = sample_table()
    metadata.create_all(engine)
    return engine, table


def round_trip(engine: Engine, table: Table, value: object) -> datetime | None:
    with engine.begin() as connection:
        connection.execute(table.insert().values(moment=value))
        stored = connection.execute(select(table.c.moment)).scalars().all()
    assert len(stored) == 1
    result = stored[0]
    assert result is None or isinstance(result, datetime)
    return result


# --- T4: UtcDateTime (AC8) -----------------------------------------------------------------


def test_the_type_is_cacheable() -> None:
    assert UtcDateTime.cache_ok is True


@pytest.mark.parametrize(
    "value",
    [
        datetime(2024, 1, 2, 5, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 0, 0, tzinfo=NEW_YORK),
        datetime(2024, 1, 2, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        pd.Timestamp("2024-01-02T05:00:00Z"),
        datetime(2024, 1, 2, 5, 0, 0, 123456, tzinfo=UTC),
    ],
    ids=["utc", "new_york", "fixed_offset", "pandas", "microseconds"],
)
def test_an_aware_value_round_trips_to_the_same_instant(tmp_path: Path, value: datetime) -> None:
    engine, table = instants_engine(tmp_path)
    try:
        result = round_trip(engine, table, value)
    finally:
        engine.dispose()

    assert result is not None
    assert result.tzinfo is UTC
    assert result == value


def test_none_round_trips_to_none(tmp_path: Path) -> None:
    engine, table = instants_engine(tmp_path)
    try:
        assert round_trip(engine, table, None) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "value",
    [
        datetime(2024, 1, 2, 5, 0),  # a naive value names no instant
        pd.NaT,
        pd.Timestamp("2024-01-02T05:00:00.123456789Z"),
        "2024-01-02T05:00:00+00:00",
        1704171600,
    ],
    ids=["naive", "nat", "nanoseconds", "text", "epoch"],
)
def test_a_value_that_is_not_an_instant_is_rejected_and_nothing_is_written(
    tmp_path: Path, value: object
) -> None:
    engine, table = instants_engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(table.insert().values(moment=datetime(2024, 1, 2, 5, 0, tzinfo=UTC)))

        with pytest.raises(StatementError), engine.begin() as connection:
            connection.execute(table.insert().values(moment=value))

        with engine.connect() as connection:
            assert connection.execute(select(table.c.moment)).scalars().all() == [
                datetime(2024, 1, 2, 5, 0, tzinfo=UTC)
            ]
    finally:
        engine.dispose()


def test_the_stored_text_always_carries_six_fractional_digits(tmp_path: Path) -> None:
    engine, table = instants_engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                table.insert(),
                [
                    {"moment": datetime(2024, 1, 2, 5, 0, tzinfo=UTC)},
                    {"moment": datetime(2024, 1, 2, 5, 0, 0, 1, tzinfo=UTC)},
                ],
            )
        with engine.connect() as connection:
            stored = connection.exec_driver_sql("SELECT moment FROM instant").scalars().all()
    finally:
        engine.dispose()

    assert [str(value) for value in stored] == [
        "2024-01-02 05:00:00.000000",
        "2024-01-02 05:00:00.000001",
    ]
    assert all(STORED_TEXT.match(str(value)) for value in stored)


def test_sql_ordering_matches_chronological_order(tmp_path: Path) -> None:
    engine, table = instants_engine(tmp_path)
    instants = [
        datetime(2024, 3, 10, 7, 0, tzinfo=UTC),
        datetime(1999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2024, 1, 2, 0, 0, tzinfo=NEW_YORK),
        datetime(2100, 1, 1, 0, 0, tzinfo=UTC),
        datetime(1970, 1, 1, 0, 0, tzinfo=UTC),
    ]
    try:
        with engine.begin() as connection:
            connection.execute(table.insert(), [{"moment": value} for value in instants])
        with engine.connect() as connection:
            ordered = (
                connection.execute(select(table.c.moment).order_by(table.c.moment)).scalars().all()
            )
    finally:
        engine.dispose()

    assert list(ordered) == sorted(instants)


# --- T5: metadata (AC9) --------------------------------------------------------------------


def test_the_naming_convention_is_the_expected_one() -> None:
    assert NAMING_CONVENTION == {
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
    assert dict(Base.metadata.naming_convention) == NAMING_CONVENTION


def test_the_convention_names_every_constraint_of_a_sample_table(tmp_path: Path) -> None:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    child = Table(
        "child",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("parent.id"), nullable=False),
        Column("code", String(8), nullable=False),
        Column("name", String(8), nullable=False),
        Column("size", Integer, nullable=False),
        UniqueConstraint("code"),
        CheckConstraint("size > 0", name="positive_size"),
    )
    index = Index(None, child.c.name, child.c.code)
    engine = create_database_engine(database_path(tmp_path))
    try:
        table_ddl = str(CreateTable(child).compile(engine))
        index_ddl = str(CreateIndex(index).compile(engine))
    finally:
        engine.dispose()

    assert "CONSTRAINT pk_child PRIMARY KEY" in table_ddl
    assert "CONSTRAINT uq_child_code UNIQUE" in table_ddl
    assert "CONSTRAINT ck_child_positive_size CHECK" in table_ddl
    assert "CONSTRAINT fk_child_parent_id_parent FOREIGN KEY" in table_ddl
    assert "CREATE INDEX ix_child_name_code" in index_ddl


def test_models_re_exports_the_single_declarative_base() -> None:
    assert models.Base is base.Base
    assert models.__all__ == ["Base", "RuleRow", "TickerRow", "TickerRuleRow"]


def test_the_metadata_holds_only_the_configuration_tables_of_the_next_feature() -> None:
    """Signals and ``bot_state`` arrive with #13 (spec 013, Design 3)."""
    assert sorted(Base.metadata.tables) == ["rules", "ticker_rules", "tickers"]

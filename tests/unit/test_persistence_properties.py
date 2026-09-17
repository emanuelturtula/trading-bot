"""Property tests for ``UtcDateTime`` over drawn instants (spec 012, T11, AC8).

Hypothesis draws aware datetimes across 1970-2100 in a mix of named zones and fixed offsets
and checks invariants that must hold for *every* draw, not only the literal cases already
pinned by ``test_persistence_types.py``:

- the round trip through a real SQLite column preserves the exact instant, whatever zone the
  value was expressed in (Design 6, AC8);
- two representations of the same instant, drawn in different zones, are stored as the
  identical UTC text, so storage never depends on the caller's zone;
- ``ORDER BY`` on the column matches sorting the same instants in Python, for any drawn set of
  two to eight instants, generalizing the fixed list of
  ``test_sql_ordering_matches_chronological_order``.

No network, no wall clock: every instant is drawn or derived from a draw. Each example builds
its own throwaway database under a fresh temporary directory instead of a pytest fixture:
Hypothesis reuses one function-scoped fixture instance across every example of a test node (it
even fails its own health check over it), so an engine or a ``tmp_path`` shared across draws
would leak rows from one example into the next and silently break the ordering property.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Column, Engine, Integer, MetaData, Table, select

from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.types import UtcDateTime

# Short: every example opens and tears down its own database (spec 012, testing rules).
_SETTINGS = {"max_examples": 25, "deadline": None}

# 1970-01-01 to 2100-01-01, in whatever zone Hypothesis' tzdata-backed catalogue draws: named
# zones with DST, fixed offsets and UTC itself. Microsecond resolution matches what a stdlib
# ``datetime`` and ``UtcDateTime`` can hold, so no draw is rejected for sub-microsecond precision.
INSTANTS = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.timezones(),
)


def _instant_table() -> tuple[MetaData, Table]:
    metadata = MetaData()  # never Base.metadata: this feature ships no table (spec 012 T5)
    table = Table(
        "instant",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("moment", UtcDateTime, nullable=False),
    )
    return metadata, table


@contextmanager
def _throwaway_instants_engine() -> Iterator[tuple[Engine, Table]]:
    """A migrated-free engine with one ``instant`` table, disposed and removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix="tb-persistence-properties-"))
    engine = create_database_engine(database_path(directory), busy_timeout_ms=200)
    metadata, table = _instant_table()
    metadata.create_all(engine)
    try:
        yield engine, table
    finally:
        engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)


@given(value=INSTANTS)
@settings(**_SETTINGS)
def test_the_round_trip_preserves_the_instant_for_any_drawn_zone(value: datetime) -> None:
    with _throwaway_instants_engine() as (engine, table), engine.begin() as connection:
        connection.execute(table.insert().values(moment=value))
        stored = connection.execute(select(table.c.moment)).scalar_one()

    assert stored.tzinfo is UTC
    # Compared against ``value.astimezone(UTC)``, never ``value`` directly: PEP 495 makes a
    # plain ``==`` between two *different* tzinfo objects conservatively return ``False`` when
    # one side falls in a DST fold-ambiguous wall time, even though the resolved instant (what
    # ``astimezone`` and ``to_utc`` both compute) is identical. That is a comparison-operator
    # quirk of the drawn *input*, not a defect in ``UtcDateTime``: both sides must be
    # normalized to the same tzinfo before ``==`` is a meaningful check of "same instant".
    assert stored == value.astimezone(UTC)


@given(value=INSTANTS, other_zone=st.timezones())
@settings(**_SETTINGS)
def test_storage_does_not_depend_on_the_input_zone(value: datetime, other_zone: object) -> None:
    """The same instant, expressed in two zones, is stored as the identical UTC text."""
    same_instant_elsewhere = value.astimezone(other_zone)  # type: ignore[arg-type]

    with _throwaway_instants_engine() as (engine, table), engine.begin() as connection:
        connection.execute(
            table.insert(),
            [{"moment": value}, {"moment": same_instant_elsewhere}],
        )
        raw = connection.exec_driver_sql("SELECT moment FROM instant ORDER BY id").scalars().all()

    assert str(raw[0]) == str(raw[1])


@given(values=st.lists(INSTANTS, min_size=2, max_size=8))
@settings(**_SETTINGS)
def test_sql_ordering_matches_python_ordering_for_any_drawn_set(values: list[datetime]) -> None:
    with _throwaway_instants_engine() as (engine, table):
        with engine.begin() as connection:
            connection.execute(table.insert(), [{"moment": value} for value in values])
        with engine.connect() as connection:
            ordered = (
                connection.execute(select(table.c.moment).order_by(table.c.moment)).scalars().all()
            )

    # ``sorted(values)`` orders correctly regardless of each value's zone (Python's ``<``
    # always resolves the absolute instant), but comparing its elements to ``ordered`` with a
    # plain ``==`` hits the same PEP 495 cross-tzinfo fold quirk as the round-trip test above.
    # Normalizing the Python side to UTC first keeps the comparison meaningful.
    assert list(ordered) == [value.astimezone(UTC) for value in sorted(values)]

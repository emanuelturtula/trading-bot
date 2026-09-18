"""Property tests for the configuration repositories (spec 013, T13, AC13, AC17, AC19).

Complements ``test_persistence_rules.py``'s fixed ``valid_payloads()`` list with rules drawn
from ``tests.fixtures.rule_strategies`` (a much larger space than the curated fixtures can
enumerate) and complements ``test_persistence_properties.py``'s raw ``UtcDateTime`` round trip
with the same property exercised through the repositories that actually write timestamps.

Every example opens its own throwaway, migrated database: Hypothesis reuses one
function-scoped fixture instance across every example of a test node, so a database or a
``tmp_path`` shared across draws would leak rows from one example into the next and silently
break the ordering property (mirrors ``test_persistence_properties.py``).
"""

from __future__ import annotations

import shutil
import string
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.repositories import sample_rule
from tests.fixtures.rule_strategies import cheap_rules, rule_names
from trading_bot.domain.rules.schema import Rule, dump_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database, open_database
from trading_bot.persistence.repositories.rules import SqlRuleRepository, dump_rule_document
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

# Short: every example opens and migrates its own database (mirrors the persistence properties
# of spec 012, T11).
_SETTINGS = {"max_examples": 20, "deadline": None}

# 1970-01-01 to 2100-01-01, in whatever zone Hypothesis' tzdata-backed catalogue draws: named
# zones with DST, fixed offsets and UTC itself (mirrors test_persistence_properties.py).
INSTANTS = st.datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.timezones(),
)

# Plain upper-case letters: always a valid, already-normalized symbol (spec 004), so a
# collision-free ``unique=True`` list needs no extra normalization step to stay collision-free.
SYMBOLS = st.text(alphabet=string.ascii_uppercase, min_size=1, max_size=8)


@contextmanager
def _throwaway_database() -> Iterator[Database]:
    """A freshly migrated database under its own directory, disposed and removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix="tb-repository-properties-"))
    database = open_database(directory, busy_timeout_ms=200)
    try:
        yield database
    finally:
        database.dispose()
        shutil.rmtree(directory, ignore_errors=True)


# --- AC13: store -> load -> dump_rule is a fixed point, over the whole strategy space --------


@given(rule=cheap_rules())
@settings(**_SETTINGS)
def test_store_load_dump_rule_is_a_fixed_point_for_any_drawn_rule(rule: Rule) -> None:
    """The round trip through storage never moves a rule's meaning or its canonical text.

    This is the same guarantee ``test_persistence_rules.py`` pins for the curated
    ``valid_payloads()`` list, exercised instead over the much larger space
    ``tests.fixtures.rule_strategies`` draws from (every registry indicator, every operator,
    nested groups, nested cooldowns), so a corner the curated list happens not to cover still
    gets checked.
    """
    expected_document = dump_rule_document(rule)
    with _throwaway_database() as database:
        with database.session() as session:
            stored = SqlRuleRepository(session).add(rule)
        with database.engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT definition_json FROM rules WHERE id = ?", (stored.id,)
            ).scalar_one()
        with database.session() as session:
            reloaded = SqlRuleRepository(session).get(stored.id)

    assert row == expected_document
    assert reloaded is not None
    assert reloaded.rule == rule
    assert dump_rule(reloaded.rule) == dump_rule(rule)


# --- AC17: timestamps round-trip exactly, whatever zone they were expressed in ---------------


@given(instant=INSTANTS)
@settings(**_SETTINGS)
def test_a_tickers_created_at_round_trips_exactly_for_any_drawn_zone(instant: datetime) -> None:
    with _throwaway_database() as database:
        with database.session() as session:
            stored = SqlTickerRepository(session, clock=lambda: instant).add("AAPL", Timeframe.D1)
        with database.session() as session:
            reloaded = SqlTickerRepository(session).get(stored.id)

    assert reloaded is not None
    assert reloaded.created_at.tzinfo is UTC
    # Compared after normalizing to UTC, never against ``instant`` directly: PEP 495 makes a
    # plain ``==`` between two different tzinfo objects conservatively return ``False`` for a
    # DST fold-ambiguous wall time even when the resolved instant is identical (spec 012,
    # hand-off 14; mirrors test_persistence_properties.py).
    assert reloaded.created_at == instant.astimezone(UTC)


@given(instant=INSTANTS)
@settings(**_SETTINGS)
def test_a_rules_timestamps_round_trip_exactly_for_any_drawn_zone(instant: datetime) -> None:
    with _throwaway_database() as database:
        with database.session() as session:
            stored = SqlRuleRepository(session, clock=lambda: instant).add(sample_rule())
        with database.session() as session:
            reloaded = SqlRuleRepository(session).get(stored.id)

    assert reloaded is not None
    assert reloaded.created_at == instant.astimezone(UTC)
    assert reloaded.updated_at == instant.astimezone(UTC)


# --- AC19: list_all's order matches Python's own sort, for any drawn set ---------------------


@given(symbols=st.lists(SYMBOLS, min_size=2, max_size=8, unique=True))
@settings(**_SETTINGS)
def test_tickers_list_all_matches_python_sort_for_any_drawn_set_of_symbols(
    symbols: list[str],
) -> None:
    with _throwaway_database() as database:
        with database.session() as session:
            handles = SqlTickerRepository(session)
            for symbol in symbols:
                handles.add(symbol, Timeframe.D1)
        with database.session() as session:
            listed = SqlTickerRepository(session).list_all()

    assert [row.symbol for row in listed] == sorted(symbols)
    assert list(listed) == sorted(listed, key=lambda row: (row.symbol, row.timeframe.value))


@given(names=st.lists(rule_names(), min_size=2, max_size=8, unique=True))
@settings(**_SETTINGS)
def test_rules_list_all_matches_python_sort_for_any_drawn_set_of_names(names: list[str]) -> None:
    with _throwaway_database() as database:
        with database.session() as session:
            handles = SqlRuleRepository(session)
            for name in names:
                handles.add(sample_rule(name))
        with database.session() as session:
            listed = SqlRuleRepository(session).list_all()

    assert [row.name for row in listed] == sorted(names)
    assert list(listed) == sorted(listed, key=lambda row: row.name)

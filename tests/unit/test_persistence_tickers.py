"""The ticker repository (spec 013, T4, AC6, AC11, AC15, AC18, AC19).

Every instant is a literal and every clock is injected, so nothing here reads the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.repositories import fixed_clock, repositories, sample_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import DuplicateTickerError, UnknownTickerError
from trading_bot.persistence.records import StoredTicker

START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
NEW_YORK = ZoneInfo("America/New_York")


# --- add -----------------------------------------------------------------------------------


def test_add_stores_an_enabled_ticker_with_the_injected_instant(database: Database) -> None:
    with database.session() as session:
        stored = repositories(session, clock=fixed_clock(START)).tickers.add("AAPL", Timeframe.D1)

    assert stored == StoredTicker(
        id=1, symbol="AAPL", timeframe=Timeframe.D1, enabled=True, created_at=START
    )


def test_add_normalizes_the_symbol(database: Database) -> None:
    with database.session() as session:
        stored = repositories(session).tickers.add("  aapl  ", Timeframe.D1)

    assert stored.symbol == "AAPL"


def test_a_disabled_ticker_can_be_added(database: Database) -> None:
    with database.session() as session:
        stored = repositories(session).tickers.add("AAPL", Timeframe.H1, enabled=False)

    assert stored.enabled is False


def test_one_symbol_can_be_tracked_on_two_timeframes(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        daily = tickers.add("AAPL", Timeframe.D1)
        hourly = tickers.add("AAPL", Timeframe.H1)

    assert (daily.id, hourly.id) == (1, 2)
    assert (daily.timeframe, hourly.timeframe) == (Timeframe.D1, Timeframe.H1)


def test_adding_the_same_symbol_and_timeframe_twice_is_refused(database: Database) -> None:
    with database.session() as session:
        repositories(session).tickers.add("AAPL", Timeframe.D1)

    with database.session() as session, pytest.raises(DuplicateTickerError) as error:
        repositories(session).tickers.add(" aapl ", Timeframe.D1)

    assert "AAPL" in str(error.value)
    assert "1d" in str(error.value)


def test_a_refused_duplicate_leaves_the_session_usable(database: Database) -> None:
    """The typed error is raised before the flush, so the unit of work can continue (AC15)."""
    with database.session() as session:
        tickers = repositories(session).tickers
        tickers.add("AAPL", Timeframe.D1)

        with pytest.raises(DuplicateTickerError):
            tickers.add("AAPL", Timeframe.D1)

        tickers.add("MSFT", Timeframe.D1)

    with database.session() as session:
        assert [ticker.symbol for ticker in repositories(session).tickers.list_all()] == [
            "AAPL",
            "MSFT",
        ]


@pytest.mark.parametrize("symbol", ["", "   ", "AA PL", "AA|PL", "a" * 33, "café"])
def test_a_malformed_symbol_is_refused_and_writes_nothing(database: Database, symbol: str) -> None:
    with database.session() as session, pytest.raises(ValueError, match="ticker"):
        repositories(session).tickers.add(symbol, Timeframe.D1)

    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


# --- lookups and ordering ------------------------------------------------------------------


def test_get_returns_the_stored_record_and_none_for_a_missing_id(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session, clock=fixed_clock(START)).tickers
        stored = tickers.add("AAPL", Timeframe.D1)

        assert tickers.get(stored.id) == stored
        assert tickers.get(stored.id + 1) is None


def test_get_by_symbol_normalizes_the_symbol_and_matches_the_timeframe(
    database: Database,
) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        stored = tickers.add("AAPL", Timeframe.D1)

        assert tickers.get_by_symbol(" aapl ", Timeframe.D1) == stored
        assert tickers.get_by_symbol("AAPL", Timeframe.H1) is None
        assert tickers.get_by_symbol("MSFT", Timeframe.D1) is None


def test_list_all_is_ordered_by_symbol_and_timeframe(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        for symbol, timeframe in (
            ("MSFT", Timeframe.D1),
            ("AAPL", Timeframe.H4),
            ("AAPL", Timeframe.D1),
            ("AAPL", Timeframe.H1),
        ):
            tickers.add(symbol, timeframe)

    with database.session() as session:
        listed = repositories(session).tickers.list_all()

    assert [(ticker.symbol, ticker.timeframe.value) for ticker in listed] == [
        ("AAPL", "1d"),
        ("AAPL", "1h"),
        ("AAPL", "4h"),
        ("MSFT", "1d"),
    ]
    assert listed == tuple(sorted(listed, key=lambda row: (row.symbol, row.timeframe.value)))


def test_list_enabled_filters_the_flag_and_optionally_the_timeframe(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        tickers.add("AAPL", Timeframe.D1)
        tickers.add("MSFT", Timeframe.D1, enabled=False)
        tickers.add("NVDA", Timeframe.H1)

    with database.session() as session:
        tickers = repositories(session).tickers

        assert [row.symbol for row in tickers.list_enabled()] == ["AAPL", "NVDA"]
        assert [row.symbol for row in tickers.list_enabled(Timeframe.D1)] == ["AAPL"]
        assert [row.symbol for row in tickers.list_enabled(Timeframe.H4)] == []


# --- set_enabled and delete ----------------------------------------------------------------


def test_set_enabled_switches_the_flag(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        stored = tickers.add("AAPL", Timeframe.D1)

        disabled = tickers.set_enabled(stored.id, False)

        assert disabled.enabled is False
        assert disabled.created_at == stored.created_at


def test_set_enabled_to_the_current_value_changes_nothing(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session, clock=fixed_clock(START)).tickers
        stored = tickers.add("AAPL", Timeframe.D1)

        assert tickers.set_enabled(stored.id, True) == stored


def test_set_enabled_on_a_missing_ticker_is_refused(database: Database) -> None:
    with database.session() as session, pytest.raises(UnknownTickerError, match="404"):
        repositories(session).tickers.set_enabled(404, True)


def test_delete_reports_whether_a_row_was_removed(database: Database) -> None:
    with database.session() as session:
        tickers = repositories(session).tickers
        stored = tickers.add("AAPL", Timeframe.D1)

        assert tickers.delete(stored.id) is True
        assert tickers.delete(stored.id) is False

    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


def test_deleting_a_ticker_cascades_to_its_assignments_only(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        kept = handles.tickers.add("MSFT", Timeframe.D1)
        removed = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule())
        handles.assignments.assign(removed.id, rule.id)
        handles.assignments.assign(kept.id, rule.id)

    with database.session() as session:
        assert repositories(session).tickers.delete(removed.id) is True

    with database.session() as session:
        handles = repositories(session)
        remaining = handles.assignments.list_all()

        assert [(row.ticker_id, row.rule_id) for row in remaining] == [(kept.id, rule.id)]
        assert handles.rules.get(rule.id) is not None
        assert [row.symbol for row in handles.tickers.list_all()] == ["MSFT"]


# --- the unit of work belongs to the caller ------------------------------------------------


def test_a_failed_unit_of_work_stores_nothing(database: Database) -> None:
    def failing_unit_of_work() -> None:
        with database.session() as session:
            repositories(session).tickers.add("AAPL", Timeframe.D1)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        failing_unit_of_work()

    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


def test_the_repository_never_commits(database: Database) -> None:
    """Leaving the block without the context manager's commit must store nothing (AC11)."""
    session = database.session_factory()
    try:
        repositories(session).tickers.add("AAPL", Timeframe.D1)
    finally:
        session.rollback()
        session.close()

    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


def test_a_clock_in_another_zone_is_normalized_to_utc(database: Database) -> None:
    """Spec 012 hand-off 14: instants are normalized before they are compared."""
    local = START.astimezone(NEW_YORK)
    with database.session() as session:
        stored = repositories(session, clock=fixed_clock(local, timedelta(0))).tickers.add(
            "AAPL", Timeframe.D1
        )

    with database.session() as session:
        reloaded = repositories(session).tickers.get(stored.id)

    assert reloaded is not None
    assert stored.created_at == START
    assert reloaded.created_at == START
    assert reloaded.created_at.utcoffset() == timedelta(0)

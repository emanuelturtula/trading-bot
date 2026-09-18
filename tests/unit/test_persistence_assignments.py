"""Assignments and the D11 timeframe rule (spec 013, T7, AC5, AC6, AC16).

The repository raises a typed error before writing, and the database refuses the same row
through the composite foreign keys of decision D86: both are exercised here, the second with
raw SQL that bypasses the repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.fixtures.repositories import fixed_clock, repositories, sample_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import (
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownTickerError,
)
from trading_bot.persistence.records import Assignment

START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
STEP = timedelta(seconds=1)
RAW_ASSIGNMENT = text(
    "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
    " VALUES (:ticker_id, :rule_id, :timeframe, '2026-01-02 03:04:05.000000')"
)


# --- assign --------------------------------------------------------------------------------


def test_assign_stores_the_pair_with_the_shared_timeframe(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, timedelta(0)))
        ticker = handles.tickers.add("AAPL", Timeframe.H1)
        rule = handles.rules.add(sample_rule("Hourly breakout", timeframe="1h"))

        assignment = handles.assignments.assign(ticker.id, rule.id)

    assert assignment == Assignment(
        ticker_id=ticker.id, rule_id=rule.id, timeframe=Timeframe.H1, created_at=START
    )


def test_assigning_twice_is_idempotent(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, STEP))
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule())

        first = handles.assignments.assign(ticker.id, rule.id)
        second = handles.assignments.assign(ticker.id, rule.id)

        assert second == first
        assert handles.assignments.list_all() == (first,)


def test_a_timeframe_mismatch_is_refused_with_both_codes(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.H1)
        rule = handles.rules.add(sample_rule("Daily breakout", timeframe="1d"))

        with pytest.raises(TimeframeMismatchError) as error:
            handles.assignments.assign(ticker.id, rule.id)

        assert error.value.ticker_timeframe is Timeframe.H1
        assert error.value.rule_timeframe is Timeframe.D1
        assert "1h" in str(error.value)
        assert "1d" in str(error.value)

    with database.session() as session:
        assert repositories(session).assignments.list_all() == ()


def test_assigning_a_missing_ticker_or_rule_is_refused(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule())

        with pytest.raises(UnknownTickerError, match="404"):
            handles.assignments.assign(404, rule.id)

        with pytest.raises(UnknownRuleError, match="404"):
            handles.assignments.assign(ticker.id, 404)


def test_unassign_reports_whether_a_row_was_removed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule())
        handles.assignments.assign(ticker.id, rule.id)

        assert handles.assignments.unassign(ticker.id, rule.id) is True
        assert handles.assignments.unassign(ticker.id, rule.id) is False
        assert handles.assignments.list_all() == ()


# --- the database backstop (AC5) -----------------------------------------------------------


def test_a_raw_insert_with_mismatched_timeframes_is_refused_by_the_database(
    database: Database,
) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.H1)
        rule = handles.rules.add(sample_rule("Daily breakout", timeframe="1d"))

    for timeframe in ("1h", "1d"):
        with (
            pytest.raises(IntegrityError, match="FOREIGN KEY"),
            database.session() as session,
        ):
            session.execute(
                RAW_ASSIGNMENT,
                {"ticker_id": ticker.id, "rule_id": rule.id, "timeframe": timeframe},
            )

    with database.session() as session:
        assert repositories(session).assignments.list_all() == ()


def test_changing_a_parent_timeframe_while_an_assignment_exists_is_refused(
    database: Database,
) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule())
        handles.assignments.assign(ticker.id, rule.id)

    updates = (
        text("UPDATE tickers SET timeframe = '1h' WHERE id = :id"),
        text("UPDATE rules SET timeframe = '1h' WHERE id = :id"),
    )
    for statement, identifier in zip(updates, (ticker.id, rule.id), strict=True):
        with (
            pytest.raises(IntegrityError, match="FOREIGN KEY"),
            database.session() as session,
        ):
            session.execute(statement, {"id": identifier})

    with database.session() as session:
        assert len(repositories(session).assignments.list_all()) == 1


# --- listings ------------------------------------------------------------------------------


def test_rules_for_ticker_is_ordered_by_name_and_can_filter_the_flag(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        other = handles.tickers.add("MSFT", Timeframe.D1)
        zulu = handles.rules.add(sample_rule("Zulu"), enabled=True)
        alpha = handles.rules.add(sample_rule("Alpha"))
        elsewhere = handles.rules.add(sample_rule("Elsewhere"), enabled=True)
        handles.assignments.assign(ticker.id, zulu.id)
        handles.assignments.assign(ticker.id, alpha.id)
        handles.assignments.assign(other.id, elsewhere.id)

    with database.session() as session:
        handles = repositories(session)

        assert [rule.name for rule in handles.assignments.rules_for_ticker(ticker.id)] == [
            "Alpha",
            "Zulu",
        ]
        enabled = handles.assignments.rules_for_ticker(ticker.id, enabled_only=True)

        assert [rule.name for rule in enabled] == ["Zulu"]


def test_rules_for_ticker_ignores_the_tickers_own_flag(database: Database) -> None:
    """``enabled_only`` filters the returned side only (spec 013, Design 7)."""
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1, enabled=False)
        rule = handles.rules.add(sample_rule("Alpha"), enabled=True)
        handles.assignments.assign(ticker.id, rule.id)

        listed = handles.assignments.rules_for_ticker(ticker.id, enabled_only=True)

        assert [stored.name for stored in listed] == ["Alpha"]


def test_tickers_for_rule_is_ordered_by_symbol_and_timeframe_and_can_filter(
    database: Database,
) -> None:
    with database.session() as session:
        handles = repositories(session)
        rule = handles.rules.add(sample_rule())
        msft = handles.tickers.add("MSFT", Timeframe.D1)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        nvda = handles.tickers.add("NVDA", Timeframe.D1, enabled=False)
        for ticker in (msft, aapl, nvda):
            handles.assignments.assign(ticker.id, rule.id)

    with database.session() as session:
        handles = repositories(session)

        assert [row.symbol for row in handles.assignments.tickers_for_rule(rule.id)] == [
            "AAPL",
            "MSFT",
            "NVDA",
        ]
        enabled = handles.assignments.tickers_for_rule(rule.id, enabled_only=True)

        assert [row.symbol for row in enabled] == ["AAPL", "MSFT"]


def test_list_all_is_ordered_by_identifier(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        first = handles.tickers.add("AAPL", Timeframe.D1)
        second = handles.tickers.add("MSFT", Timeframe.D1)
        alpha = handles.rules.add(sample_rule("Alpha"))
        bravo = handles.rules.add(sample_rule("Bravo"))
        for ticker in (second, first):
            for rule in (bravo, alpha):
                handles.assignments.assign(ticker.id, rule.id)

    with database.session() as session:
        listed = repositories(session).assignments.list_all()

    assert [(row.ticker_id, row.rule_id) for row in listed] == [
        (first.id, alpha.id),
        (first.id, bravo.id),
        (second.id, alpha.id),
        (second.id, bravo.id),
    ]


# --- both delete paths (AC6) ---------------------------------------------------------------


def test_deleting_the_rule_removes_its_assignments_and_keeps_the_tickers(
    database: Database,
) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        removed = handles.rules.add(sample_rule("Removed"))
        kept = handles.rules.add(sample_rule("Kept"))
        handles.assignments.assign(ticker.id, removed.id)
        handles.assignments.assign(ticker.id, kept.id)

    with database.session() as session:
        assert repositories(session).rules.delete(removed.id) is True

    with database.session() as session:
        handles = repositories(session)

        assert [row.rule_id for row in handles.assignments.list_all()] == [kept.id]
        assert handles.tickers.get(ticker.id) is not None


def test_deleting_the_ticker_removes_its_assignments_and_keeps_the_rules(
    database: Database,
) -> None:
    with database.session() as session:
        handles = repositories(session)
        removed = handles.tickers.add("AAPL", Timeframe.D1)
        kept = handles.tickers.add("MSFT", Timeframe.D1)
        rule = handles.rules.add(sample_rule())
        handles.assignments.assign(removed.id, rule.id)
        handles.assignments.assign(kept.id, rule.id)

    with database.session() as session:
        assert repositories(session).tickers.delete(removed.id) is True

    with database.session() as session:
        handles = repositories(session)

        assert [row.ticker_id for row in handles.assignments.list_all()] == [kept.id]
        assert handles.rules.get(rule.id) is not None

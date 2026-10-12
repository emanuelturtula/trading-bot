"""One engine run serves every user (spec 024, AC7-AC8).

Each symbol is downloaded once per run whoever watches it; every user's rules are evaluated on
their own tickers, signals carry their owner, and a disabled user's tickers are not evaluated.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    EngineHarness,
    engine_harness,
    publish,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from tests.fixtures.repositories import repositories
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.repositories.users import SqlUserRepository

NOW = utc("2024-07-02T20:00:30")  # just after the close of the 2024-07-02 daily candle
HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-05T00:00")


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    with engine_harness(tmp_path) as built:
        yield built


def second_user(harness: EngineHarness) -> int:
    """Add ``ana`` and give her AAPL 1d with a rule of the same name as the admin's."""
    with harness.database.session() as session:
        ana = SqlUserRepository(session).add("ana", password_hash="x").id
        repos = repositories(session, clock=harness.clock, owner=ana)
        ticker = repos.tickers.add("AAPL", Timeframe.D1)
        rule = repos.rules.add(triggering_rule("Alpha"), enabled=True)
        repos.assignments.assign(ticker.id, rule.id)
    return ana


def test_a_symbol_two_users_watch_is_fetched_once_and_evaluated_for_both(
    harness: EngineHarness,
) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha")])
    ana = second_user(harness)
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert [call.ticker for call in harness.provider.fetch_calls] == ["AAPL"]
    assert len(report.tickers) == 2
    assert report.notified == 2
    assert sorted(n.owner_id for n in harness.notifier.notifications) == [1, ana]
    assert len(stored_signals(harness)) == 2


def test_a_disabled_users_tickers_are_not_evaluated(harness: EngineHarness) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha")])
    ana = second_user(harness)
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)
    with harness.database.session() as session:
        SqlUserRepository(session).set_active(ana, False)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert len(report.tickers) == 1
    assert [n.owner_id for n in harness.notifier.notifications] == [1]

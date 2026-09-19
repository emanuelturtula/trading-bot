"""Two units of work recording one signal at the same time (spec 014, T4, AC8; issue AC2).

The ordered harness of ``tests/fixtures/concurrency.py`` forces the overlap: the second unit of
work **starts** while the first holds the write lock over its uncommitted row. The control, with
the second handle's busy timeout at ``0``, proves the collision is real, so the main cases
cannot pass vacuously. No sleep, no elapsed-time assertion; the clocks are injected and
different, so "B got A's row" is a statement about the stored row and not a coincidence.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from tests.fixtures.concurrency import ordered_race
from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import frozen_clock, repositories, sample_rule, sample_signal
from trading_bot.domain.signals import Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database, connect_database
from trading_bot.persistence.engine import BUSY_TIMEOUT_MS
from trading_bot.persistence.repositories.signals import SqlSignalRepository

FIRST_RECORDED_AT = datetime(2024, 1, 2, 21, 0, 5, tzinfo=UTC)
SECOND_RECORDED_AT = datetime(2024, 1, 2, 21, 30, 45, tzinfo=UTC)  # a clock of its own
CLOCK_FIRST = frozen_clock(FIRST_RECORDED_AT)
CLOCK_SECOND = frozen_clock(SECOND_RECORDED_AT)


@dataclass(frozen=True, slots=True)
class Contenders:
    """One migrated database, the ids of the pair and the signal both threads record."""

    handle: Database
    data_dir: Path
    ticker_id: int
    rule_id: int
    signal: Signal


@pytest.fixture
def contenders(tmp_path: Path) -> Iterator[Contenders]:
    """The production busy timeout, not the fixture's 200 ms: the waiter must wait it out."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir, busy_timeout_ms=BUSY_TIMEOUT_MS) as handle:
        with handle.session() as session:
            tools = repositories(session)
            ticker = tools.tickers.add("AAPL", Timeframe.D1)
            rule = tools.rules.add(sample_rule("Daily breakout"))
        yield Contenders(
            handle=handle,
            data_dir=data_dir,
            ticker_id=ticker.id,
            rule_id=rule.id,
            signal=sample_signal(ticker, rule),
        )


def stored_rows(handle: Database) -> list[tuple[int, str]]:
    with handle.engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT id, created_at FROM signals ORDER BY id").all()
    return [(int(row[0]), str(row[1])) for row in rows]


@pytest.mark.parametrize("read_first", [False, True], ids=["write-first", "read-then-write"])
def test_two_units_of_work_on_one_handle_neither_raise_nor_duplicate(
    contenders: Contenders, read_first: bool
) -> None:
    race = ordered_race(
        contenders.handle,
        contenders.handle,
        contenders.signal,
        clock_first=CLOCK_FIRST,
        clock_second=CLOCK_SECOND,
        ticker_id=contenders.ticker_id,
        rule_id=contenders.rule_id,
        read_first=read_first,
    )

    assert race.second_error is None, race.second_error
    assert race.second is not None
    assert race.first.is_new is True
    assert race.second.is_new is False
    assert race.second.stored == race.first.stored
    assert race.second.stored.created_at == FIRST_RECORDED_AT  # the first writer's clock
    assert stored_rows(contenders.handle) == [(race.first.stored.id, "2024-01-02 21:00:05.000000")]


@pytest.mark.parametrize("read_first", [False, True], ids=["write-first", "read-then-write"])
def test_two_units_of_work_on_two_handles_neither_raise_nor_duplicate(
    contenders: Contenders, read_first: bool
) -> None:
    """The second handle is a second engine on the same file, as the CLI is next to the app."""
    second = connect_database(contenders.data_dir, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        race = ordered_race(
            contenders.handle,
            second,
            contenders.signal,
            clock_first=CLOCK_FIRST,
            clock_second=CLOCK_SECOND,
            ticker_id=contenders.ticker_id,
            rule_id=contenders.rule_id,
            read_first=read_first,
        )
    finally:
        second.dispose()

    assert race.second_error is None, race.second_error
    assert race.second is not None
    assert (race.first.is_new, race.second.is_new) == (True, False)
    assert race.second.stored == race.first.stored
    assert race.second.stored.created_at == FIRST_RECORDED_AT
    assert stored_rows(contenders.handle) == [(race.first.stored.id, "2024-01-02 21:00:05.000000")]


@pytest.mark.parametrize("read_first", [False, True], ids=["write-first", "read-then-write"])
def test_the_control_proves_the_two_units_of_work_really_collide(
    contenders: Contenders, read_first: bool
) -> None:
    """With a zero busy timeout and the lock held until it is done, the second one is refused."""
    second = connect_database(contenders.data_dir, busy_timeout_ms=0)
    try:
        race = ordered_race(
            contenders.handle,
            second,
            contenders.signal,
            clock_first=CLOCK_FIRST,
            clock_second=CLOCK_SECOND,
            ticker_id=contenders.ticker_id,
            rule_id=contenders.rule_id,
            read_first=read_first,
            hold_until_second_finishes=True,
        )
    finally:
        second.dispose()

    assert race.second is None
    assert isinstance(race.second_error, OperationalError)
    assert "database is locked" in str(race.second_error)
    assert race.first.is_new is True
    assert stored_rows(contenders.handle) == [(race.first.stored.id, "2024-01-02 21:00:05.000000")]


def test_the_loser_of_the_race_can_be_read_back_afterwards(contenders: Contenders) -> None:
    """Whatever the interleaving, one committed row carries the first writer's payload."""
    race = ordered_race(
        contenders.handle,
        contenders.handle,
        contenders.signal,
        clock_first=CLOCK_FIRST,
        clock_second=CLOCK_SECOND,
        ticker_id=contenders.ticker_id,
        rule_id=contenders.rule_id,
        read_first=True,
    )

    with contenders.handle.session() as session:
        repository = SqlSignalRepository(session, clock=CLOCK_SECOND)
        stored = repository.latest(contenders.ticker_id, contenders.rule_id)

        assert repository.count() == 1
    assert stored is not None
    assert stored == race.first.stored
    assert stored.signal == contenders.signal

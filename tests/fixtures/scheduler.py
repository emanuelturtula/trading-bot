"""Doubles for the candle-close scheduler (spec 016, Design 12; decision D157).

Nothing here reads the wall clock and nothing sleeps: ``ManualClock`` moves only when a test or
``recording_sleep`` moves it, so "a week of closes" is a millisecond-long test and every timing
assertion is exact instead of approximate. ``FakeSignalRunner`` records what the runner asked
the engine for — the timeframe, the ``now`` and the ticker subset — which is how "the engine
receives the scheduled close" and "a retry reuses the same ``now``" are checked.

``FakeSignalRunner.run`` yields to the event loop once before it finishes, so two runs that were
allowed to overlap really would: a test that sees no interleaving is seeing a lock, not a lucky
scheduling order.

Always import this module as ``tests.fixtures.scheduler``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import frozen_clock
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import FailureKind, RunReport, TickerOutcome, TickerStatus
from trading_bot.engine.sql_unit_of_work import sql_unit_of_work
from trading_bot.engine.unit_of_work import UnitOfWork
from trading_bot.persistence.database import Database
from trading_bot.scheduler.policy import DEFAULT_POLICY, SchedulerPolicy
from trading_bot.scheduler.runner import RunAttempt, SignalRunner, Sleep, TimeframeRunner
from trading_bot.scheduler.trigger import SlotPredicate, run_every_slot

__all__ = [
    "STATE_CLOCK",
    "FakeSignalRunner",
    "ManualClock",
    "RunCall",
    "SchedulerHarness",
    "as_signal_runner",
    "failing_report",
    "fire",
    "paused_report",
    "quiet_report",
    "recording_sleep",
    "scheduler_harness",
]

# The instant the repositories stamp ``completed_at`` with: the state clock is not the
# scheduler's clock (spec 013, D92), and pinning it keeps the assertions literal.
STATE_CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class ManualClock:
    """A clock that only moves when a test or the recording sleep moves it."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError(f"a manual clock never moves backwards, got {delta}")
        self._now += delta

    def set(self, instant: datetime) -> None:
        """Jump to ``instant``; a step backwards is what an NTP correction looks like."""
        self._now = instant


def recording_sleep(clock: ManualClock) -> tuple[Sleep, list[float]]:
    """A ``Sleep`` that records the waits and advances ``clock`` instead of sleeping."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        await asyncio.sleep(0)  # a yield to the loop, never a wait

    return sleep, waits


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCall:
    """One recorded ``engine.run``, with the clock readings that prove runs never overlap."""

    timeframe: Timeframe
    now: datetime
    tickers: tuple[str, ...] | None
    started_at: datetime
    finished_at: datetime


class FakeSignalRunner:
    """Records calls, answers scripted ``RunReport``s and raises scripted failures, FIFO."""

    def __init__(self, *, clock: ManualClock, duration: timedelta = timedelta(0)) -> None:
        self._clock = clock
        self._duration = duration
        self._calls: list[RunCall] = []
        self._reports: deque[RunReport] = deque()
        self._failures: deque[Exception] = deque()
        self._block: asyncio.Event | None = None
        self._entered = 0
        self._called = asyncio.Event()

    @property
    def calls(self) -> tuple[RunCall, ...]:
        """Every completed call, in order, including the ones that raised."""
        return tuple(self._calls)

    async def wait_until_called(self, count: int) -> None:
        """Wait until ``count`` calls have **entered** the engine, without polling or sleeping.

        It is how a test says "the run has reached the engine" instead of guessing how many
        yields that takes, which is what keeps the concurrency tests deterministic. It counts
        entries rather than completions, so it also works while a run is held by ``block_on``.
        """
        while self._entered < count:
            self._called.clear()
            await self._called.wait()

    def script(self, report: RunReport, *, times: int = 1) -> None:
        """Answer the next ``times`` calls with ``report`` (first in first out)."""
        self._reports.extend([report] * times)

    def fail_next(self, error: Exception, *, times: int = 1) -> None:
        """Raise ``error`` on the next ``times`` calls (first in first out)."""
        if not isinstance(error, Exception):
            raise TypeError(f"error must be an Exception, got {type(error).__name__}")
        self._failures.extend([error] * times)

    def block_on(self, event: asyncio.Event) -> None:
        """Hold every run in flight until ``event`` is set."""
        self._block = event

    async def run(
        self, timeframe: Timeframe, now: datetime, *, tickers: Sequence[str] | None = None
    ) -> RunReport:
        started_at = self._clock()
        self._entered += 1
        self._called.set()
        await asyncio.sleep(0)  # a yield: two runs allowed to overlap would interleave here
        if self._block is not None:
            await self._block.wait()
        self._clock.advance(self._duration)
        self._calls.append(
            RunCall(
                timeframe=timeframe,
                now=now,
                tickers=None if tickers is None else tuple(tickers),
                started_at=started_at,
                finished_at=self._clock(),
            )
        )
        if self._failures:
            raise self._failures.popleft().with_traceback(None)
        if self._reports:
            return self._reports.popleft()
        return quiet_report(timeframe, now)


def as_signal_runner(runner: FakeSignalRunner) -> SignalRunner:
    """Returns its argument; strict mypy checks that the fake conforms to the port."""
    return runner


def quiet_report(timeframe: Timeframe, now: datetime, *symbols: str) -> RunReport:
    """A run in which every ticker was evaluated and nothing failed."""
    return RunReport(
        timeframe=timeframe,
        now=now,
        paused=False,
        tickers=tuple(
            TickerOutcome(
                ticker=symbol,
                ticker_id=position,
                status=TickerStatus.EVALUATED,
                failure=None,
                retryable=False,
                rules_evaluated=1,
                signals=(),
            )
            for position, symbol in enumerate(symbols, start=1)
        ),
    )


def failing_report(
    timeframe: Timeframe, now: datetime, *symbols: str, retryable: bool = True
) -> RunReport:
    """A run in which every named ticker failed; ``retryable`` is what #15 re-runs."""
    return RunReport(
        timeframe=timeframe,
        now=now,
        paused=False,
        tickers=tuple(
            TickerOutcome(
                ticker=symbol,
                ticker_id=position,
                status=TickerStatus.FAILED,
                failure=FailureKind.NOT_PUBLISHED if retryable else FailureKind.NO_DATA,
                retryable=retryable,
                rules_evaluated=0,
                signals=(),
            )
            for position, symbol in enumerate(symbols, start=1)
        ),
    )


def paused_report(timeframe: Timeframe, now: datetime) -> RunReport:
    """What the engine returns while ``paused_since`` is set (spec 015, D133)."""
    return RunReport(timeframe=timeframe, now=now, paused=True, tickers=())


@dataclass(frozen=True, slots=True)
class SchedulerHarness:
    """One wired ``TimeframeRunner`` and every double behind it."""

    data_dir: Path
    database: Database
    calendar: MarketCalendar
    engine: FakeSignalRunner
    clock: ManualClock
    waits: list[float]
    unit_of_work: UnitOfWork
    policy: SchedulerPolicy
    runner: TimeframeRunner


@contextmanager
def scheduler_harness(
    path: Path,
    *,
    now: datetime,
    policy: SchedulerPolicy = DEFAULT_POLICY,
    should_run: SlotPredicate = run_every_slot,
    duration: timedelta = timedelta(0),
) -> Iterator[SchedulerHarness]:
    """A ``TimeframeRunner`` over a migrated database, a fake engine and a manual clock.

    ``duration`` is how far the fake engine advances the clock on each call, which is what makes
    an overlap between two runs visible in the recorded call log.
    """
    data_dir = path / "database"
    calendar = nyse_test_calendar()
    clock = ManualClock(now)
    sleep, waits = recording_sleep(clock)
    engine = FakeSignalRunner(clock=clock, duration=duration)
    with temporary_database(data_dir) as database:
        unit_of_work = sql_unit_of_work(database, clock=frozen_clock(STATE_CLOCK))
        yield SchedulerHarness(
            data_dir=data_dir,
            database=database,
            calendar=calendar,
            engine=engine,
            clock=clock,
            waits=waits,
            unit_of_work=unit_of_work,
            policy=policy,
            runner=TimeframeRunner(
                engine=as_signal_runner(engine),
                calendar=calendar,
                unit_of_work=unit_of_work,
                policy=policy,
                clock=clock,
                sleep=sleep,
                should_run=should_run,
            ),
        )


def fire(harness: SchedulerHarness, timeframe: Timeframe, *, catch_up: bool = False) -> RunAttempt:
    """One fire, on a loop of its own, as the data and engine tests run their coroutines."""
    return asyncio.run(harness.runner.run_timeframe(timeframe, catch_up=catch_up))

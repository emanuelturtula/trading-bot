"""A ready-made ``SignalEngine`` over doubles and a temporary database (spec 015, Design 12).

``engine_harness`` assembles the fixtures that already exist — a migrated database under
``tmp_path``, ``nyse_test_calendar()``, ``FakeMarketDataProvider`` over ``session_candles``
frames, ``FakeNotifier`` and ``sql_unit_of_work`` with a frozen clock — so a test states what it
is about instead of wiring six objects. It adds no second database fixture and no second
calendar.

``SignalFailures`` scripts the persistence errors a real database cannot produce on demand (a
ticker removed between two units of work), by wrapping the ``SignalRepository`` of every unit of
work of a run.

There is no network here and no clock is read: ``now`` is always the caller's literal and the
repositories write the injected ``frozen_clock``. Always import this module as
``tests.fixtures.engine``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.database import temporary_database
from tests.fixtures.fake_provider import FakeMarketDataProvider, as_market_data_provider
from tests.fixtures.notifiers import FakeNotifier, as_notifier
from tests.fixtures.repositories import Repositories, frozen_clock, repositories
from tests.fixtures.rules import (
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    value_operand,
)
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.rules.schema import Rule, parse_rule
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import RunReport
from trading_bot.engine.signal_engine import SignalEngine
from trading_bot.engine.sql_unit_of_work import sql_unit_of_work
from trading_bot.engine.unit_of_work import EngineRepositories, UnitOfWork
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.engine import database_path
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.protocols import SignalRepository
from trading_bot.persistence.signal_records import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RecordOutcome,
    SignalCursor,
    SignalPage,
    StoredSignal,
)

__all__ = [
    "ENGINE_CLOCK",
    "EngineHarness",
    "SignalFailures",
    "engine_harness",
    "publish",
    "quiet_rule",
    "run_engine",
    "stored_signals",
    "track",
    "triggering_rule",
    "write_lock_is_free",
]

# A literal instant far from any wall clock: every ``created_at`` and ``notified_at`` a test
# reads is this one, so nothing depends on when the suite runs.
ENGINE_CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

type SignalMethod = Literal["record", "get", "mark_notified", "latest", "history", "count"]


def write_lock_is_free(data_dir: Path) -> bool:
    """Whether another connection could take the write lock right now (spec 014, T3).

    The probe never waits, so no elapsed time is ever measured. Any refusal other than
    ``database is locked`` propagates: a probe that failed for another reason would silently
    make every caller read "a session is open".
    """
    with closing(
        sqlite3.connect(database_path(data_dir), timeout=0, isolation_level=None)
    ) as probe:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "database is locked" not in str(error):
                raise
            return False
        probe.execute("ROLLBACK")
        return True


class SignalFailures:
    """A script of ``SignalRepository`` failures, shared by the units of work of a run."""

    def __init__(self) -> None:
        self._scripted: dict[SignalMethod, deque[Exception]] = {}

    def fail_next(self, method: SignalMethod, error: Exception, *, times: int = 1) -> None:
        """Raise ``error`` on the next ``times`` calls of ``method`` (first in first out)."""
        if not isinstance(error, Exception):
            raise TypeError(f"error must be an Exception, got {type(error).__name__}")
        if isinstance(times, bool) or not isinstance(times, int):
            raise TypeError(f"times must be an int, got {type(times).__name__}")
        if times < 1:
            raise ValueError(f"times must be at least 1, got {times}")
        self._scripted.setdefault(method, deque()).extend([error] * times)

    def take(self, method: SignalMethod) -> None:
        """Raise the next scripted error of ``method``, if there is one."""
        queue = self._scripted.get(method)
        if queue:
            raise queue.popleft().with_traceback(None)


class ScriptedSignalRepository:
    """A ``SignalRepository`` that delegates, after the script of ``SignalFailures`` had a say."""

    def __init__(self, delegate: SignalRepository, failures: SignalFailures) -> None:
        self._delegate = delegate
        self._failures = failures

    def record(self, signal: Signal) -> RecordOutcome:
        self._failures.take("record")
        return self._delegate.record(signal)

    def get(self, signal_id: int) -> StoredSignal | None:
        self._failures.take("get")
        return self._delegate.get(signal_id)

    def mark_notified(self, signal_id: int) -> StoredSignal:
        self._failures.take("mark_notified")
        return self._delegate.mark_notified(signal_id)

    def latest(
        self, ticker_id: int, rule_id: int, *, notified_only: bool = False
    ) -> StoredSignal | None:
        self._failures.take("latest")
        return self._delegate.latest(ticker_id, rule_id, notified_only=notified_only)

    def history(
        self,
        *,
        ticker_id: int | None = None,
        rule_id: int | None = None,
        timeframe: Timeframe | None = None,
        notified: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        before: SignalCursor | None = None,
    ) -> SignalPage:
        self._failures.take("history")
        return self._delegate.history(
            ticker_id=ticker_id,
            rule_id=rule_id,
            timeframe=timeframe,
            notified=notified,
            since=since,
            until=until,
            limit=limit,
            before=before,
        )

    def count(self, *, ticker_id: int | None = None, rule_id: int | None = None) -> int:
        self._failures.take("count")
        return self._delegate.count(ticker_id=ticker_id, rule_id=rule_id)


def scripted_unit_of_work(unit_of_work: UnitOfWork, failures: SignalFailures) -> UnitOfWork:
    """``unit_of_work`` with its signal repository wrapped by the script."""

    @contextmanager
    def scripted() -> Iterator[EngineRepositories]:
        with unit_of_work() as engine_repositories:
            yield replace(
                engine_repositories,
                signals=ScriptedSignalRepository(engine_repositories.signals, failures),
            )

    def factory() -> AbstractContextManager[EngineRepositories]:
        return scripted()

    return factory


@dataclass(frozen=True, slots=True)
class EngineHarness:
    """One wired ``SignalEngine`` and every double behind it."""

    data_dir: Path
    database: Database
    calendar: MarketCalendar
    provider: FakeMarketDataProvider
    notifier: FakeNotifier
    failures: SignalFailures
    unit_of_work: UnitOfWork
    clock: Clock
    engine: SignalEngine


@contextmanager
def engine_harness(path: Path, *, clock: Clock | None = None) -> Iterator[EngineHarness]:
    """A migrated database under ``path`` with a ``SignalEngine`` wired over the doubles."""
    ticks = clock if clock is not None else frozen_clock(ENGINE_CLOCK)
    data_dir = path / "database"
    calendar = nyse_test_calendar()
    with temporary_database(data_dir) as database:
        provider = FakeMarketDataProvider(calendar=calendar)
        notifier = FakeNotifier(lock_probe=lambda: write_lock_is_free(data_dir))
        failures = SignalFailures()
        unit_of_work = scripted_unit_of_work(sql_unit_of_work(database, clock=ticks), failures)
        yield EngineHarness(
            data_dir=data_dir,
            database=database,
            calendar=calendar,
            provider=provider,
            notifier=notifier,
            failures=failures,
            unit_of_work=unit_of_work,
            clock=ticks,
            engine=SignalEngine(
                provider=as_market_data_provider(provider),
                unit_of_work=unit_of_work,
                notifier=as_notifier(notifier),
            ),
        )


@contextmanager
def configuration(harness: EngineHarness) -> Iterator[Repositories]:
    """The five repositories over one unit of work, for a test that edits the configuration."""
    with harness.database.session() as session:
        yield repositories(session, clock=harness.clock)


def stored_signals(harness: EngineHarness) -> tuple[StoredSignal, ...]:
    """Every recorded signal, newest first, read in a unit of work of its own."""
    with configuration(harness) as repos:
        return repos.signals.history(limit=MAX_PAGE_SIZE).items


def track(
    harness: EngineHarness,
    symbol: str,
    timeframe: Timeframe,
    rules: Sequence[Rule],
    *,
    enabled: bool = True,
    rules_enabled: bool = True,
) -> tuple[StoredTicker, tuple[StoredRule, ...]]:
    """Track ``symbol`` and assign ``rules`` to it, reusing a rule already stored by name."""
    with configuration(harness) as repos:
        ticker = repos.tickers.add(symbol, timeframe, enabled=enabled)
        stored: list[StoredRule] = []
        for rule in rules:
            existing = repos.rules.get_by_name(rule.name)
            if existing is None:
                existing = repos.rules.add(rule, enabled=rules_enabled)
            repos.assignments.assign(ticker.id, existing.id)
            stored.append(existing)
        return ticker, tuple(stored)


def triggering_rule(
    name: str,
    *,
    side: Side = Side.BUY,
    cooldown_bars: int = 0,
    timeframe: Timeframe = Timeframe.D1,
    history: int = 20,
) -> Rule:
    """A rule that fires on every warmed-up candle of the synthetic frames.

    ``close > 1`` holds for every price ``synthetic_candles`` draws around 100, and
    ``sma(length=history) > 0`` holds for every positive price: the second condition is there
    to give the rule a warmup, and with it a lookback, that a cooldown can be counted over.
    """
    conditions: JsonValue = {
        "all": [
            condition(price_operand(), ">", value_operand(1.0)),
            condition(indicator_operand("sma", {"length": history}), ">", value_operand(0.0)),
        ]
    }
    return parse_rule(
        rule_payload(
            conditions,
            name=name,
            signal=side.value,
            timeframe=timeframe.value,
            cooldown_bars=cooldown_bars,
        )
    )


def quiet_rule(name: str, *, timeframe: Timeframe = Timeframe.D1) -> Rule:
    """A rule that never fires on the synthetic frames: ``close < 1``."""
    conditions: JsonValue = {"all": [condition(price_operand(), "<", value_operand(1.0))]}
    return parse_rule(rule_payload(conditions, name=name, timeframe=timeframe.value))


def publish(
    harness: EngineHarness,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    seed: int = 0,
) -> pd.DataFrame:
    """Give the provider the candles of ``[start, end)`` on the calendar grid, as Yahoo shapes
    them, and return the canonical frame they were built from."""
    candles = session_candles(harness.calendar, timeframe, start, end, seed=seed)
    harness.provider.set_candles(symbol, timeframe, provider_shaped(candles))
    return candles


def run_engine(
    harness: EngineHarness,
    timeframe: Timeframe,
    now: datetime,
    *,
    tickers: Sequence[str] | None = None,
) -> RunReport:
    """One run, on a loop of its own, as the data-layer tests run their coroutines."""
    return asyncio.run(harness.engine.run(timeframe, now, tickers=tickers))

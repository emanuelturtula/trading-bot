"""Idempotency and stress, the tester's own load on CLAUDE.md rule 5 (spec 014, T14, AC8, AC11).

Complements the developer's ``test_persistence_signal_concurrency.py`` (T4), which proves the
race is safe with a **forced** interleaving, and ``test_persistence_signals.py`` (T5), which
proves it twice in one session, across sessions and across a restart. This file adds:

- a **real**, unordered race (``threading.Barrier``, not events) of several threads per key, so
  the database's own locking is what keeps the outcome correct, not the test's orchestration
  (the statistical guard decision D110 itself was measured with: 217 of 280 losers failed with
  ``database is locked`` under a deferred ``BEGIN``, none under ``BEGIN IMMEDIATE``);
- the ordered harness of ``tests/fixtures/concurrency.py`` repeated 50 times per variant, so a
  flake that only shows up once in a while is a permanent regression check and not something the
  tester merely observed once from a shell loop;
- longer idempotency chains (50 sessions in turn, several restarts, 50 delivery confirmations)
  than the developer's T5 and T6.

A mutation check for D110 itself (a session bound to a deferred ``BEGIN`` can lose the exact race
``Database.session()`` always wins) lives in ``test_persistence_adversarial.py``, next to T17's
other adversarial cases, so it is not duplicated here.

No wall clock, no elapsed-time assertion; every wait carries the harness's own 60 s guard.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures.concurrency import GUARD_SECONDS, ordered_race
from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import frozen_clock, repositories, sample_rule, sample_signal
from trading_bot.domain.signals import Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database, connect_database
from trading_bot.persistence.engine import BUSY_TIMEOUT_MS
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.signal_records import RecordOutcome

FIRST_AT = datetime(2024, 1, 2, 21, 0, 5, tzinfo=UTC)
SECOND_AT = datetime(2024, 1, 2, 21, 30, 45, tzinfo=UTC)
CLOCK_FIRST = frozen_clock(FIRST_AT)
CLOCK_SECOND = frozen_clock(SECOND_AT)

BASE_CLOSE = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Contenders:
    """A migrated database with the production busy timeout, and the pair signals share."""

    handle: Database
    data_dir: Path
    ticker_id: int
    rule_id: int
    ticker: StoredTicker
    rule: StoredRule


@pytest.fixture
def contenders(tmp_path: Path) -> Iterator[Contenders]:
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
            ticker=ticker,
            rule=rule,
        )


def signal_of(contenders: Contenders, *, days: int = 0) -> Signal:
    return sample_signal(
        contenders.ticker, contenders.rule, candle_close_ts=BASE_CLOSE + timedelta(days=days)
    )


# --- the ordered harness, repeated: a one-off green run is not a regression check -----------


@pytest.mark.parametrize(
    ("two_handles", "read_first"),
    [
        pytest.param(False, False, id="one-handle-write-first"),
        pytest.param(False, True, id="one-handle-read-then-write"),
        pytest.param(True, False, id="two-handles-write-first"),
        pytest.param(True, True, id="two-handles-read-then-write"),
    ],
)
def test_the_ordered_harness_wins_the_same_way_fifty_times_in_a_row(
    contenders: Contenders, two_handles: bool, read_first: bool
) -> None:
    """Fifty repetitions per variant, each on a signal of its own so every one is a fresh race.

    Baked into the permanent suite instead of a one-off shell loop, so a rare flake that a
    single CI run would miss becomes a real, repeatable failure here (spec 014, D123: "150 of
    150 runs" was the developer's own bar; this pins a slice of it forever).
    """
    second = (
        connect_database(contenders.data_dir, busy_timeout_ms=BUSY_TIMEOUT_MS)
        if two_handles
        else contenders.handle
    )
    try:
        for repetition in range(50):
            race = ordered_race(
                contenders.handle,
                second,
                signal_of(contenders, days=repetition),
                clock_first=CLOCK_FIRST,
                clock_second=CLOCK_SECOND,
                ticker_id=contenders.ticker_id,
                rule_id=contenders.rule_id,
                read_first=read_first,
            )

            assert race.second_error is None, (repetition, race.second_error)
            assert race.second is not None, repetition
            assert race.first.is_new is True, repetition
            assert race.second.is_new is False, repetition
            assert race.second.stored == race.first.stored, repetition
            assert race.second.stored.created_at == FIRST_AT, repetition
    finally:
        if two_handles:
            second.dispose()


# --- a real, unordered race: the database's own locking, not the test's orchestration -------

STRESS_KEYS = 12
STRESS_THREADS = 8


def _barrier_race(
    writer: Database,
    reader: Database,
    signal: Signal,
    *,
    threads: int,
    read_first: bool,
    ticker_id: int,
    rule_id: int,
) -> tuple[list[RecordOutcome | None], list[BaseException | None]]:
    """``threads`` units of work recording ``signal`` at once, released together by a barrier.

    Unlike ``ordered_race`` nothing here forces an interleaving: only the database's own locking
    (D110) can keep the outcome correct. Threads alternate between ``writer`` and ``reader`` by
    index, so a mismatched pair exercises two engines over one file, as the CLI runs next to the
    application, while a matched pair exercises one connection pool.
    """
    barrier = threading.Barrier(threads)
    outcomes: list[RecordOutcome | None] = [None] * threads
    failures: list[BaseException | None] = [None] * threads

    def worker(index: int) -> None:
        handle = writer if index % 2 == 0 else reader
        barrier.wait(GUARD_SECONDS)
        try:
            with handle.session() as session:
                repository = SqlSignalRepository(session, clock=CLOCK_FIRST)
                if read_first:
                    repository.latest(ticker_id, rule_id)
                outcomes[index] = repository.record(signal)
        except BaseException as error:  # reported through the result, never allowed to escape
            failures[index] = error

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(GUARD_SECONDS)
        assert not worker_thread.is_alive(), "a racing thread did not finish within the guard"
    return outcomes, failures


@pytest.mark.parametrize(
    ("two_handles", "read_first"),
    [
        pytest.param(False, False, id="one-handle-write-first"),
        pytest.param(False, True, id="one-handle-read-then-write"),
        pytest.param(True, False, id="two-handles-write-first"),
        pytest.param(True, True, id="two-handles-read-then-write"),
    ],
)
def test_eight_threads_per_key_racing_without_forced_ordering_produce_one_row_each(
    contenders: Contenders, two_handles: bool, read_first: bool
) -> None:
    """8 threads x 12 keys, no ordering forced: exactly one ``is_new`` per key, no exception, one
    row per key, and every outcome of a key names the same stored id (issue AC2, D110)."""
    second = (
        connect_database(contenders.data_dir, busy_timeout_ms=BUSY_TIMEOUT_MS)
        if two_handles
        else contenders.handle
    )
    try:
        for key in range(STRESS_KEYS):
            signal = signal_of(contenders, days=key)
            outcomes, failures = _barrier_race(
                contenders.handle,
                second,
                signal,
                threads=STRESS_THREADS,
                read_first=read_first,
                ticker_id=contenders.ticker_id,
                rule_id=contenders.rule_id,
            )

            assert failures == [None] * STRESS_THREADS, (key, failures)
            resolved = [outcome for outcome in outcomes if outcome is not None]
            assert len(resolved) == STRESS_THREADS, key
            assert sum(1 for outcome in resolved if outcome.is_new) == 1, key
            assert len({outcome.stored.id for outcome in resolved}) == 1, key
            assert len({outcome.stored.created_at for outcome in resolved}) == 1, key
    finally:
        if two_handles:
            second.dispose()

    with contenders.handle.session() as session:
        assert SqlSignalRepository(session).count() == STRESS_KEYS


# --- longer idempotency chains than the developer's T5 ---------------------------------------


def test_fifty_sessions_recording_the_same_signal_in_turn_produce_one_row(
    database: Database,
) -> None:
    with database.session() as session:
        tools = repositories(session)
        ticker = tools.tickers.add("AAPL", Timeframe.D1)
        rule = tools.rules.add(sample_rule("Daily breakout"))
    signal = sample_signal(ticker, rule)

    first_id: int | None = None
    new_count = 0
    for _ in range(50):
        with database.session() as session:
            outcome = SqlSignalRepository(session, clock=CLOCK_FIRST).record(signal)
        if outcome.is_new:
            new_count += 1
        if first_id is None:
            first_id = outcome.stored.id
        assert outcome.stored.id == first_id

    assert new_count == 1
    with database.session() as session:
        assert SqlSignalRepository(session).count() == 1


def test_recording_survives_several_restarts_in_a_row(tmp_path: Path) -> None:
    """Record, dispose, reopen, record - three times over: the constraint, not memory, decides."""
    data_dir = tmp_path / "volume"
    stored_id: int | None = None

    with temporary_database(data_dir) as database:
        with database.session() as session:
            tools = repositories(session)
            ticker = tools.tickers.add("AAPL", Timeframe.D1)
            rule = tools.rules.add(sample_rule("Daily breakout"))
        signal = sample_signal(ticker, rule)
        with database.session() as session:
            outcome = SqlSignalRepository(session, clock=CLOCK_FIRST).record(signal)
        stored_id = outcome.stored.id
        assert outcome.is_new is True

    for _ in range(3):
        with temporary_database(data_dir) as database, database.session() as session:
            outcome = SqlSignalRepository(session, clock=CLOCK_SECOND).record(signal)
            assert outcome.is_new is False
            assert outcome.stored.id == stored_id
            assert outcome.stored.created_at == FIRST_AT

    with temporary_database(data_dir) as database, database.session() as session:
        assert SqlSignalRepository(session).count() == 1


def test_fifty_mark_notified_calls_across_sessions_never_move_the_instant(
    database: Database,
) -> None:
    """Repeated delivery confirmations (retries, a duplicated Telegram callback) are harmless."""
    with database.session() as session:
        tools = repositories(session)
        ticker = tools.tickers.add("AAPL", Timeframe.D1)
        rule = tools.rules.add(sample_rule("Daily breakout"))
    with database.session() as session:
        stored = (
            SqlSignalRepository(session, clock=CLOCK_FIRST)
            .record(sample_signal(ticker, rule))
            .stored
        )

    first_notified: datetime | None = None
    for step in range(50):
        clock = frozen_clock(SECOND_AT + timedelta(minutes=step))
        with database.session() as session:
            updated = SqlSignalRepository(session, clock=clock).mark_notified(stored.id)
        if first_notified is None:
            first_notified = updated.notified_at
        assert updated.notified_at == first_notified

    assert first_notified == SECOND_AT  # the very first call's clock, never a later one

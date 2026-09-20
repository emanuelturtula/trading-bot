"""Adversarial and stress scenarios for the signal engine (spec 015, T13, AC2, AC12, AC13).

Also exercises three things the developer flagged as easy to get wrong silently: the write/send
order under two tickers that share one rule, the cooldown gate over a history the provider capped
below ``cooldown_bars + 1`` (Design 5.1, the "Risks" section), and ``FakeNotifier.lock_free``
(AC5) on every new notifier path this file adds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    EngineHarness,
    engine_harness,
    publish,
    quiet_rule,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from tests.fixtures.fake_provider import FakeMarketDataProvider
from tests.fixtures.rules import condition, price_operand, rule_payload, value_operand
from trading_bot.domain.market_calendar.sessions import CandleSlot
from trading_bot.domain.rules.schema import Rule, parse_rule
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine import signal_engine
from trading_bot.engine.results import SignalDisposition, TickerStatus
from trading_bot.engine.unit_of_work import EngineRepositories, UnitOfWork

NOW = utc("2024-07-02T20:00:30")  # just after the close of the 2024-07-02 daily candle
LATER = utc("2024-07-03T17:00:30")  # just after the close of the 2024-07-03 half day
HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-05T00:00")


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    with engine_harness(tmp_path) as built:
        yield built


def watch(harness: EngineHarness, *symbols: str, rule_name: str = "Alpha") -> None:
    """Track each symbol with its own triggering rule and its candles, one rule per symbol."""
    for symbol in symbols:
        track(harness, symbol, Timeframe.D1, [triggering_rule(f"{rule_name}-{symbol}")])
        publish(harness, symbol, Timeframe.D1, HISTORY_START, HISTORY_END)


def _after_close(slot: CandleSlot) -> datetime:
    """A wall instant safely past ``slot``'s real close (never the nominal close, spec 010)."""
    return slot.close_time + timedelta(seconds=30)


def _minimal_rule(name: str) -> Rule:
    """A rule with ``stable_warmup() == 1``: no indicator, so a one-candle frame is enough."""
    return parse_rule(
        rule_payload({"all": [condition(price_operand(), ">", value_operand(1.0))]}, name=name)
    )


# --- A ticker with twenty rules, one that always triggers (AC2, AC4) --------------------------


def test_twenty_rules_on_one_ticker_evaluates_all_and_notifies_only_the_triggering_one(
    harness: EngineHarness,
) -> None:
    rules = [quiet_rule(f"Rule{index:02d}") for index in range(1, 21)]
    rules[9] = triggering_rule("Rule10")  # alphabetically the tenth of twenty
    track(harness, "AAPL", Timeframe.D1, rules)
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)

    report = run_engine(harness, Timeframe.D1, NOW)

    outcome = report.tickers[0]
    assert outcome.status is TickerStatus.EVALUATED
    assert outcome.rules_evaluated == 20
    assert [signal.disposition for signal in outcome.signals] == [SignalDisposition.NOTIFIED]
    assert len(harness.notifier.calls) == 1
    assert harness.notifier.calls[0].rule_name == "Rule10"
    assert harness.notifier.lock_free == (True,)


# --- Cooldown over a history the provider capped short (Design 5.1, "Risks") ------------------


def test_a_high_cooldown_over_a_provider_capped_history_errs_towards_blocked(
    harness: EngineHarness,
) -> None:
    """``cooldown_bars=500`` asks for 501 candles; the provider only ever has about ten.

    Even though the true calendar gap between the two signals below is about two years (far
    more than 500 trading days), the fetched frame never grows past what was published, so the
    gate cannot tell the true gap from a short one and blocks, the conservative direction Design
    5.1 documents.
    """
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha", cooldown_bars=500, history=2)])
    first_window = (utc("2024-05-01T00:00"), utc("2024-05-11T00:00"))
    publish(harness, "AAPL", Timeframe.D1, *first_window)
    first_slots = harness.calendar.candle_slots(Timeframe.D1, *first_window)

    first = run_engine(harness, Timeframe.D1, _after_close(first_slots[-1]))
    assert first.tickers[0].signals[0].disposition is SignalDisposition.NOTIFIED

    # Two years later: a provider that only ever publishes a short recent window, never the
    # candles in between.
    second_window = (utc("2026-05-01T00:00"), utc("2026-05-11T00:00"))
    publish(harness, "AAPL", Timeframe.D1, *second_window)
    second_slots = harness.calendar.candle_slots(Timeframe.D1, *second_window)
    assert len(second_slots) < 501  # the fetched frame is far shorter than the cooldown window

    second = run_engine(harness, Timeframe.D1, _after_close(second_slots[-1]))

    # The plan really did ask for the full 501-candle window (D127): it is the provider, not the
    # engine, that could only offer a short one. A silently narrower request would make this
    # test pass for the wrong reason (the published data alone would be short either way).
    assert [call.lookback for call in harness.provider.fetch_calls] == [501, 501]
    assert second.tickers[0].signals[0].disposition is SignalDisposition.COOLDOWN
    assert len(harness.notifier.calls) == 1  # only the first, distant signal was ever sent
    assert len(stored_signals(harness)) == 1


# --- A rule whose warmup the available candles never reach (AC12/AC13 boundary) ----------------


def test_a_rule_whose_warmup_exceeds_the_available_candles_never_fires(
    harness: EngineHarness,
) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha", history=50)])
    window = (utc("2024-05-01T00:00"), utc("2024-05-15T00:00"))  # about ten trading days
    publish(harness, "AAPL", Timeframe.D1, *window)
    slots = harness.calendar.candle_slots(Timeframe.D1, *window)
    assert len(slots) < 50

    report = run_engine(harness, Timeframe.D1, _after_close(slots[-1]))

    outcome = report.tickers[0]
    assert outcome.status is TickerStatus.EVALUATED
    assert outcome.rules_evaluated == 1
    assert outcome.signals == ()
    assert harness.notifier.calls == ()
    assert stored_signals(harness) == ()


# --- A frame of exactly one candle -------------------------------------------------------------


def test_a_frame_of_exactly_one_candle_is_evaluated_and_may_notify(harness: EngineHarness) -> None:
    track(harness, "AAPL", Timeframe.D1, [_minimal_rule("Alpha")])
    window = (utc("2024-07-01T00:00"), utc("2024-07-02T00:00"))  # a single Monday session
    publish(harness, "AAPL", Timeframe.D1, *window)
    slots = harness.calendar.candle_slots(Timeframe.D1, *window)
    assert len(slots) == 1

    report = run_engine(harness, Timeframe.D1, _after_close(slots[0]))

    assert harness.notifier.frames[0].shape[0] == 1
    assert report.tickers[0].signals[0].disposition is SignalDisposition.NOTIFIED


# --- A symbol in lower case with whitespace in the configuration (AC20/normalization) ----------


def test_a_messy_symbol_in_the_configuration_is_stored_and_run_normalized(
    harness: EngineHarness,
) -> None:
    track(harness, " aapl ", Timeframe.D1, [triggering_rule("Alpha")])
    publish(harness, " aapl ", Timeframe.D1, HISTORY_START, HISTORY_END)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert [call.ticker for call in harness.provider.fetch_calls] == ["AAPL"]
    assert report.tickers[0].ticker == "AAPL"
    assert harness.notifier.calls[0].key.ticker == "AAPL"


# --- A provider that returns the same frame object twice ---------------------------------------


class RepeatingProvider:
    """Ignores the arguments of any call after the first: returns the very first frame again.

    A pathological provider (a caching bug), used to prove that the engine's idempotency does
    not depend on ``now`` bookkeeping: it is the frame's own last candle that names the key.
    """

    def __init__(self, delegate: FakeMarketDataProvider) -> None:
        self._delegate = delegate
        self._first: object = None

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> object:
        frame = await self._delegate.fetch_candles(ticker, timeframe, lookback, now=now)
        if self._first is None:
            self._first = frame
            return frame
        return self._first

    async def validate_ticker(self, ticker: str) -> object:  # pragma: no cover - unused here
        return await self._delegate.validate_ticker(ticker)


def test_a_provider_returning_the_same_frame_twice_still_yields_one_notification(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")
    instrumented = replace(
        harness,
        engine=signal_engine.SignalEngine(
            provider=RepeatingProvider(harness.provider),  # type: ignore[arg-type]
            unit_of_work=harness.unit_of_work,
            notifier=harness.notifier,
        ),
    )

    first = run_engine(instrumented, Timeframe.D1, NOW)
    second = run_engine(instrumented, Timeframe.D1, LATER)

    assert first.tickers[0].signals[0].disposition is SignalDisposition.NOTIFIED
    # The stale, identical frame names the same candle again: the unique constraint, not `now`,
    # is what makes this a duplicate (CLAUDE.md rule 5).
    assert second.tickers[0].signals[0].disposition is SignalDisposition.DUPLICATE
    assert len(harness.notifier.calls) == 1
    assert len(stored_signals(harness)) == 1


# --- A notifier that mutates the frame it receives (AC18, the port's documented bug) ------------


class MutatingNotifier:
    """A broken ``Notifier``: mutates the shared candles after delegating (a notifier bug).

    The mutation happens only after ``FakeNotifier`` has taken its pristine snapshot, exactly
    as a chart-drawing step that forgets to copy the frame would corrupt it after the message
    was already handed off: it is the object the engine kept evaluating, not a fresh one.
    """

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    async def notify(self, notification: object) -> None:
        await self._delegate.notify(notification)  # type: ignore[attr-defined]
        notification.candles.iloc[0, 0] = -1.0  # type: ignore[attr-defined]


def test_a_notifier_that_mutates_the_frame_still_lets_the_run_complete(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")
    instrumented = replace(
        harness,
        engine=signal_engine.SignalEngine(
            provider=harness.provider,
            unit_of_work=harness.unit_of_work,
            notifier=MutatingNotifier(harness.notifier),  # type: ignore[arg-type]
        ),
    )

    report = run_engine(instrumented, Timeframe.D1, NOW)

    assert report.tickers[0].signals[0].disposition is SignalDisposition.NOTIFIED
    assert harness.notifier.mutated_frames == (0,)


# --- The write/send order under two tickers sharing one rule (AC5, D131) -----------------------


class _JournalSignals:
    """Records the three signal-repository calls the order is about, then delegates."""

    def __init__(self, delegate: object, journal: list[str]) -> None:
        self._delegate = delegate
        self._journal = journal

    def latest(self, ticker_id: int, rule_id: int, *, notified_only: bool = False) -> object:
        self._journal.append("latest")
        return self._delegate.latest(  # type: ignore[attr-defined]
            ticker_id, rule_id, notified_only=notified_only
        )

    def record(self, signal: object) -> object:
        self._journal.append("record")
        return self._delegate.record(signal)  # type: ignore[attr-defined]

    def mark_notified(self, signal_id: int) -> object:
        self._journal.append("mark_notified")
        return self._delegate.mark_notified(signal_id)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _JournalProvider:
    def __init__(self, delegate: FakeMarketDataProvider, journal: list[str]) -> None:
        self._delegate = delegate
        self._journal = journal

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> object:
        self._journal.append(f"fetch {ticker}")
        return await self._delegate.fetch_candles(ticker, timeframe, lookback, now=now)

    async def validate_ticker(self, ticker: str) -> object:  # pragma: no cover - unused here
        return await self._delegate.validate_ticker(ticker)


class _JournalNotifier:
    def __init__(self, delegate: object, journal: list[str]) -> None:
        self._delegate = delegate
        self._journal = journal

    async def notify(self, notification: object) -> None:
        self._journal.append(f"notify {notification.signal.rule_name}")  # type: ignore[attr-defined]
        await self._delegate.notify(notification)  # type: ignore[attr-defined]


def _journal_unit_of_work(delegate: UnitOfWork, journal: list[str]) -> UnitOfWork:
    @contextmanager
    def scoped() -> Iterator[EngineRepositories]:
        journal.append("unit of work begin")
        with delegate() as repositories:
            yield replace(repositories, signals=_JournalSignals(repositories.signals, journal))  # type: ignore[arg-type]
        journal.append("unit of work commit")

    return scoped


def test_the_order_stays_correct_with_two_tickers_and_a_rule_that_fires_twice(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Developer's ask: break the single-signal journal of T4 with a second ticker and a rule
    shared by both (so it fires twice in the same run), and confirm the journal still reads
    plan, fetch, evaluate, [record+commit, notify, stamp] once per triggered signal, in order.
    """
    # "Extra" sorts before "Shared", so AAPL evaluates it first; MSFT only has "Shared".
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Extra"), triggering_rule("Shared")])
    track(harness, "MSFT", Timeframe.D1, [triggering_rule("Shared")])
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)
    publish(harness, "MSFT", Timeframe.D1, HISTORY_START, HISTORY_END)

    journal: list[str] = []
    real_evaluate = signal_engine.evaluate

    def journaled_evaluate(rule: Rule, candles: object, **kwargs: object) -> object:
        journal.append(f"evaluate {rule.name}")
        return real_evaluate(rule, candles, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(signal_engine, "evaluate", journaled_evaluate)
    unit_of_work = _journal_unit_of_work(harness.unit_of_work, journal)
    instrumented = replace(
        harness,
        unit_of_work=unit_of_work,
        engine=signal_engine.SignalEngine(
            provider=_JournalProvider(harness.provider, journal),  # type: ignore[arg-type]
            unit_of_work=unit_of_work,
            notifier=_JournalNotifier(harness.notifier, journal),  # type: ignore[arg-type]
        ),
    )

    run_engine(instrumented, Timeframe.D1, NOW)

    def claim_and_send(rule_name: str) -> list[str]:
        return [
            "unit of work begin",
            "latest",
            "record",
            "unit of work commit",
            f"notify {rule_name}",
            "unit of work begin",
            "mark_notified",
            "unit of work commit",
        ]

    assert journal == [
        "unit of work begin",
        "unit of work commit",
        "fetch AAPL",
        "evaluate Extra",
        "evaluate Shared",
        *claim_and_send("Extra"),
        *claim_and_send("Shared"),
        "fetch MSFT",
        "evaluate Shared",
        *claim_and_send("Shared"),
    ]
    assert len(harness.notifier.calls) == 3
    assert harness.notifier.lock_free == (True, True, True)


# --- Many tickers, rerun dozens of times (developer's ask: repeat the suites yourself) ----------


def test_many_tickers_stay_idempotent_across_dozens_of_reruns(harness: EngineHarness) -> None:
    symbols = tuple(f"TICK{index}" for index in range(5))
    watch(harness, *symbols)

    first = run_engine(harness, Timeframe.D1, NOW)
    assert [signal.disposition for ticker in first.tickers for signal in ticker.signals] == [
        SignalDisposition.NOTIFIED
    ] * len(symbols)

    for _ in range(29):
        report = run_engine(harness, Timeframe.D1, NOW)
        assert [signal.disposition for ticker in report.tickers for signal in ticker.signals] == [
            SignalDisposition.DUPLICATE
        ] * len(symbols)

    assert len(harness.notifier.calls) == len(symbols)
    assert len(stored_signals(harness)) == len(symbols)
    assert all(harness.notifier.lock_free)


# --- A fixture contract worth locking in: track() reuses a rule by name (not a src/ behaviour) --


def test_track_reuses_a_stored_rule_by_name_and_keeps_the_first_definition(
    harness: EngineHarness,
) -> None:
    """``tests/fixtures/engine.py`` ``track()`` looks up a rule by name before adding one.

    Two ``Rule`` objects that share a name are therefore stored once, as whichever one was
    tracked first: this is a fixture contract, not a ``src/`` behaviour, documented here so a
    test that tracks the "same" rule twice with different fields does not silently pass against
    the wrong definition.
    """
    first_rule = triggering_rule("Shared", side=Side.BUY)
    second_rule = triggering_rule("Shared", side=Side.SELL)

    _, first_stored = track(harness, "AAPL", Timeframe.D1, [first_rule])
    _, second_stored = track(harness, "MSFT", Timeframe.D1, [second_rule])

    assert first_stored[0].id == second_stored[0].id
    assert second_stored[0].rule.signal is Side.BUY

"""The run pipeline, its isolation rings and a configuration that changed (spec 015, T4, T5, T6).

Covers AC2, AC4, AC5, AC11, AC12, AC13, AC14 and AC15. The order of operations is asserted from
a recorded call log, never from inspection, and no test sleeps, waits on a barrier or reads the
wall clock: ``now`` is a literal and the repositories write an injected frozen clock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    ENGINE_CLOCK,
    EngineHarness,
    configuration,
    engine_harness,
    publish,
    quiet_rule,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from tests.fixtures.fake_provider import FakeMarketDataProvider, FetchCall
from tests.fixtures.session_candles import provider_shaped, session_candles
from trading_bot.data.errors import (
    CandleNotPublishedError,
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
    ProviderDataError,
    ProviderFailure,
    ProviderUnavailableError,
    UnpublishedReason,
)
from trading_bot.domain.market_calendar.sessions import CalendarRangeError
from trading_bot.domain.rules.errors import RuleErrorKind, RuleProblem
from trading_bot.domain.signals import Side, SignalKey
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine import signal_engine
from trading_bot.engine.results import FailureKind, SignalDisposition, TickerStatus
from trading_bot.engine.unit_of_work import UnitOfWork
from trading_bot.persistence.errors import (
    StoredRuleError,
    StoredSignalError,
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownSignalError,
    UntrackedTickerError,
)

NOW = utc("2024-07-02T20:00:30")  # just after the close of the 2024-07-02 daily candle
LAST_LABEL = utc("2024-07-02T04:00")
LAST_CLOSE = utc("2024-07-03T04:00")  # the nominal close, which identifies the candle
HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-05T00:00")
LATER = utc("2024-07-03T17:00:30")  # just after the close of the 2024-07-03 half day
LOGGER_NAME = "trading_bot.engine"


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    with engine_harness(tmp_path) as built:
        yield built


def watch(harness: EngineHarness, *symbols: str, rules: Sequence[object] | None = None) -> None:
    """Track each symbol with the given rules (one triggering rule by default) and its candles."""
    built = list(rules) if rules is not None else [triggering_rule("Alpha")]
    for symbol in symbols:
        track(harness, symbol, Timeframe.D1, built)  # type: ignore[arg-type]
        publish(harness, symbol, Timeframe.D1, HISTORY_START, HISTORY_END)


class RaisingProxy:
    """A repository double: delegates everything, raising ``error`` on one method."""

    def __init__(self, delegate: object, method: str, error: Exception) -> None:
        self._delegate = delegate
        self._method = method
        self._error = error

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._delegate, name)
        if name != self._method:
            return attribute

        def raising(*args: object, **kwargs: object) -> object:
            raise self._error

        return raising


def raising_unit_of_work(
    delegate: UnitOfWork, port: str, method: str, error: Exception
) -> UnitOfWork:
    """``delegate`` with one repository replaced by a proxy that raises on ``method``."""

    @contextmanager
    def scoped() -> Iterator[object]:
        with delegate() as repositories:
            yield replace(
                repositories, **{port: RaisingProxy(getattr(repositories, port), method, error)}
            )

    return scoped  # type: ignore[return-value]


class ExplodingProvider:
    """A provider that raises an arbitrary error for one symbol and delegates the rest."""

    def __init__(self, delegate: FakeMarketDataProvider, symbol: str, error: BaseException) -> None:
        self._delegate = delegate
        self._symbol = symbol
        self._error = error

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame:
        if ticker == self._symbol:
            raise self._error
        return await self._delegate.fetch_candles(ticker, timeframe, lookback, now=now)

    async def validate_ticker(self, ticker: str) -> object:  # pragma: no cover - unused here
        return await self._delegate.validate_ticker(ticker)


def with_provider(harness: EngineHarness, provider: object) -> EngineHarness:
    return replace(
        harness,
        engine=signal_engine.SignalEngine(
            provider=provider,  # type: ignore[arg-type]
            unit_of_work=harness.unit_of_work,
            notifier=harness.notifier,
        ),
    )


def with_unit_of_work(harness: EngineHarness, unit_of_work: UnitOfWork) -> EngineHarness:
    return replace(
        harness,
        unit_of_work=unit_of_work,
        engine=signal_engine.SignalEngine(
            provider=harness.provider,  # type: ignore[arg-type]
            unit_of_work=unit_of_work,
            notifier=harness.notifier,
        ),
    )


# --- T4: one fetch per planned ticker (AC2) --------------------------------------------------


def test_one_fetch_per_planned_ticker_with_the_exact_arguments(harness: EngineHarness) -> None:
    watch(harness, "AAPL", "MSFT")

    report = run_engine(harness, Timeframe.D1, NOW)

    assert harness.provider.fetch_calls == (
        FetchCall(ticker="AAPL", timeframe=Timeframe.D1, lookback=20, now=NOW),
        FetchCall(ticker="MSFT", timeframe=Timeframe.D1, lookback=20, now=NOW),
    )
    assert [ticker.ticker for ticker in report.tickers] == ["AAPL", "MSFT"]


def test_the_lookback_is_the_plan_of_the_ticker_rules(harness: EngineHarness) -> None:
    watch(harness, "AAPL", rules=[triggering_rule("Alpha", history=30, cooldown_bars=2)])

    run_engine(harness, Timeframe.D1, NOW)

    assert harness.provider.fetch_calls[0].lookback == 30


def test_the_frame_is_evaluated_exactly_as_the_provider_returned_it(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")
    reference = FakeMarketDataProvider(calendar=harness.calendar)
    reference.set_candles(
        "AAPL",
        Timeframe.D1,
        provider_shaped(
            session_candles(harness.calendar, Timeframe.D1, HISTORY_START, HISTORY_END)
        ),
    )
    expected = asyncio.run(reference.fetch_candles("AAPL", Timeframe.D1, 20, now=NOW))

    run_engine(harness, Timeframe.D1, NOW)

    delivered = harness.notifier.frames[0]
    pd.testing.assert_frame_equal(delivered, expected)
    assert harness.notifier.mutated_frames == ()
    assert len(harness.provider.fetch_calls) == 1


def test_a_disabled_ticker_is_never_fetched(harness: EngineHarness) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha")], enabled=False)
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert harness.provider.fetch_calls == ()
    assert report.tickers == ()


# --- T4: evaluation and the signal it builds (AC4) -------------------------------------------


def test_every_enabled_rule_is_evaluated_once_and_only_the_triggered_one_is_reported(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL", rules=[triggering_rule("Alpha"), quiet_rule("Zulu")])

    report = run_engine(harness, Timeframe.D1, NOW)
    outcome = report.tickers[0]

    assert outcome.status is TickerStatus.EVALUATED
    assert outcome.rules_evaluated == 2
    assert [signal.disposition for signal in outcome.signals] == [SignalDisposition.NOTIFIED]
    assert len(harness.notifier.calls) == 1


def test_the_signal_carries_the_fields_of_the_evaluation(harness: EngineHarness) -> None:
    watch(harness, "AAPL", rules=[triggering_rule("Alpha", side=Side.SELL)])
    with configuration(harness) as repos:
        rule_id = repos.rules.get_by_name("Alpha").id  # type: ignore[union-attr]

    run_engine(harness, Timeframe.D1, NOW)

    signal = stored_signals(harness)[0].signal
    frame = harness.notifier.frames[0]
    assert signal.ticker == "AAPL"
    assert signal.timeframe is Timeframe.D1
    assert signal.rule_id == str(rule_id)
    assert signal.side is Side.SELL
    assert signal.candle_close_ts == LAST_CLOSE
    assert signal.close_price == frame["close"].iloc[-1]
    assert list(signal.indicator_values) == ["sma(length=20).value"]
    assert str(signal.idempotency_key) == f"AAPL|1d|{rule_id}|2024-07-03T04:00:00+00:00"


def test_a_ticker_whose_rules_never_trigger_records_and_notifies_nothing(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL", rules=[quiet_rule("Zulu")])

    report = run_engine(harness, Timeframe.D1, NOW)

    assert report.tickers[0].signals == ()
    assert harness.notifier.calls == ()
    assert stored_signals(harness) == ()


# --- T4: the mandatory order (AC5) ------------------------------------------------------------


def test_the_recorded_order_is_plan_fetch_evaluate_record_notify_stamp(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch(harness, "AAPL")
    journal: list[str] = []
    real_evaluate = signal_engine.evaluate

    def journaled_evaluate(rule: object, candles: pd.DataFrame, **kwargs: object) -> object:
        journal.append(f"evaluate {rule.name}")  # type: ignore[attr-defined]
        return real_evaluate(rule, candles, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(signal_engine, "evaluate", journaled_evaluate)
    unit_of_work = journal_unit_of_work(harness.unit_of_work, journal)
    instrumented = replace(
        harness,
        unit_of_work=unit_of_work,
        engine=signal_engine.SignalEngine(
            provider=JournalProvider(harness.provider, journal),  # type: ignore[arg-type]
            unit_of_work=unit_of_work,
            notifier=JournalNotifier(harness.notifier, journal),  # type: ignore[arg-type]
        ),
    )

    run_engine(instrumented, Timeframe.D1, NOW)

    assert journal == [
        "unit of work begin",  # the configuration snapshot
        "unit of work commit",
        "fetch AAPL",
        "evaluate Alpha",
        "unit of work begin",  # the cooldown read and the claim, in one unit of work
        "latest",
        "record",
        "unit of work commit",  # committed before anything is sent
        "notify",
        "unit of work begin",  # the stamp, in a unit of work of its own
        "mark_notified",
        "unit of work commit",
    ]


class JournalProvider:
    """Records each fetch in the shared journal, then delegates."""

    def __init__(self, delegate: FakeMarketDataProvider, journal: list[str]) -> None:
        self._delegate = delegate
        self._journal = journal

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame:
        self._journal.append(f"fetch {ticker}")
        return await self._delegate.fetch_candles(ticker, timeframe, lookback, now=now)

    async def validate_ticker(self, ticker: str) -> object:  # pragma: no cover - unused here
        return await self._delegate.validate_ticker(ticker)


class JournalNotifier:
    """Records each delivery in the shared journal, then delegates."""

    def __init__(self, delegate: object, journal: list[str]) -> None:
        self._delegate = delegate
        self._journal = journal

    async def notify(self, notification: object) -> None:
        self._journal.append("notify")
        await self._delegate.notify(notification)  # type: ignore[attr-defined]


class JournalSignals:
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


def journal_unit_of_work(delegate: UnitOfWork, journal: list[str]) -> UnitOfWork:
    @contextmanager
    def scoped() -> Iterator[object]:
        journal.append("unit of work begin")
        with delegate() as repositories:
            yield replace(repositories, signals=JournalSignals(repositories.signals, journal))
        journal.append("unit of work commit")

    return scoped  # type: ignore[return-value]


def test_no_database_session_is_open_while_a_notification_is_sent(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL", "MSFT")

    run_engine(harness, Timeframe.D1, NOW)

    assert harness.notifier.lock_free == (True, True)


# --- T4/T6: the stamp (AC11) --------------------------------------------------------------------


def test_a_delivered_signal_is_stamped_from_the_injected_clock(harness: EngineHarness) -> None:
    watch(harness, "AAPL")

    run_engine(harness, Timeframe.D1, NOW)

    stored = stored_signals(harness)[0]
    assert stored.created_at == ENGINE_CLOCK
    assert stored.notified_at == ENGINE_CLOCK


def test_a_signal_that_vanished_before_the_stamp_stays_notified(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL")
    harness.failures.fail_next("mark_notified", UnknownSignalError(1))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    assert report.tickers[0].signals[0].disposition is SignalDisposition.NOTIFIED
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "was removed before it was stamped" in warnings[0].getMessage()


# --- T5: a failing ticker does not stop the rest (AC12) ---------------------------------------


@pytest.mark.parametrize(
    ("error", "kind", "retryable", "level"),
    [
        (
            InvalidTickerError(
                InvalidTickerReason.NOT_FOUND, "the provider does not know this ticker"
            ),
            FailureKind.INVALID_TICKER,
            False,
            logging.WARNING,
        ),
        (
            NoDataError("no candle closed at now survives"),
            FailureKind.NO_DATA,
            False,
            logging.WARNING,
        ),
        (
            ProviderDataError("missing_column", "the response cannot be normalized"),
            FailureKind.PROVIDER_DATA,
            False,
            logging.ERROR,
        ),
        (
            CandleNotPublishedError(
                UnpublishedReason.MISSING,
                ticker="AAPL",
                timeframe=Timeframe.D1,
                expected_label=LAST_LABEL,
                last_label=None,
            ),
            FailureKind.NOT_PUBLISHED,
            True,
            logging.INFO,
        ),
        (
            ProviderUnavailableError(ProviderFailure.TIMEOUT),
            FailureKind.UNAVAILABLE,
            True,
            logging.WARNING,
        ),
    ],
)
def test_a_market_data_failure_isolates_one_ticker(
    harness: EngineHarness,
    caplog: pytest.LogCaptureFixture,
    error: MarketDataError,
    kind: FailureKind,
    retryable: bool,
    level: int,
) -> None:
    watch(harness, "AAPL", "MSFT", "NVDA")
    harness.provider.fail_next(error, ticker="AAPL")

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    failure = report.tickers[0]
    assert failure.ticker == "AAPL"
    assert failure.status is TickerStatus.FAILED
    assert failure.failure is kind
    assert failure.retryable is retryable
    assert failure.rules_evaluated == 0
    assert failure.signals == ()
    assert [ticker.ticker for ticker in report.tickers[1:]] == ["MSFT", "NVDA"]
    assert [call.key.ticker for call in harness.notifier.calls] == ["MSFT", "NVDA"]
    assert {stored.signal.ticker for stored in stored_signals(harness)} == {"MSFT", "NVDA"}
    named = [
        record
        for record in caplog.records
        if record.levelno == level and "AAPL" in record.getMessage()
    ]
    assert len(named) == 1
    assert report.retryable_tickers == (("AAPL",) if retryable else ())


def test_an_unpublished_candle_says_the_scheduler_will_retry(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL")
    harness.provider.fail_next(
        CandleNotPublishedError(
            UnpublishedReason.MISSING,
            ticker="AAPL",
            timeframe=Timeframe.D1,
            expected_label=LAST_LABEL,
            last_label=None,
        ),
        ticker="AAPL",
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("candle not published for AAPL 1d:")
        and message.endswith("; the scheduler will retry")
        for message in messages
    )
    assert report.retryable_tickers == ("AAPL",)


def test_an_unknown_market_data_subclass_is_reported_as_unexpected(
    harness: EngineHarness,
) -> None:
    class FutureMarketDataError(MarketDataError):
        """A subclass this release does not know."""

    watch(harness, "AAPL", "MSFT")
    harness.provider.fail_next(FutureMarketDataError("something new"), ticker="AAPL")

    report = run_engine(harness, Timeframe.D1, NOW)

    assert report.tickers[0].failure is FailureKind.UNEXPECTED
    assert report.tickers[1].signals[0].disposition is SignalDisposition.NOTIFIED


# --- T5: an unexpected error is isolated but never silent (AC13) -------------------------------


def test_an_arbitrary_provider_error_is_unexpected_and_names_only_the_class(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL", "MSFT")
    secret = "the provider answered " + "9" * 12 + ":" + "x" * 35
    instrumented = with_provider(
        harness, ExplodingProvider(harness.provider, "AAPL", RuntimeError(secret))
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(instrumented, Timeframe.D1, NOW)

    assert report.tickers[0].failure is FailureKind.UNEXPECTED
    assert report.tickers[0].retryable is False
    assert report.tickers[1].signals[0].disposition is SignalDisposition.NOTIFIED
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage() == "AAPL 1d failed (unexpected): RuntimeError"
    assert errors[0].exc_info is None
    assert secret not in caplog.text


def test_an_arbitrary_evaluation_error_is_unexpected(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch(harness, "AAPL", "MSFT")
    real_evaluate = signal_engine.evaluate

    def exploding(rule: object, candles: pd.DataFrame, **kwargs: object) -> object:
        if rule.name == "Alpha" and len(candles) == 20:  # type: ignore[attr-defined]
            raise ZeroDivisionError("an indicator divided by zero")
        return real_evaluate(rule, candles, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(signal_engine, "evaluate", exploding)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert [ticker.failure for ticker in report.tickers] == [
        FailureKind.UNEXPECTED,
        FailureKind.UNEXPECTED,
    ]
    assert harness.notifier.calls == ()


def test_a_cancellation_is_never_caught(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch(harness, "AAPL")
    instrumented = with_provider(
        harness, ExplodingProvider(harness.provider, "AAPL", asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        run_engine(instrumented, Timeframe.D1, NOW)


# --- T5: loud failures propagate (AC14) ---------------------------------------------------------


def test_a_corrupt_stored_signal_stops_the_run(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL", "MSFT")
    harness.failures.fail_next("latest", StoredSignalError(4, "payload"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), pytest.raises(StoredSignalError):
        run_engine(harness, Timeframe.D1, NOW)

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "1d run at 2024-07-02T20:00:30+00:00 stopped: StoredSignalError"
    )
    assert harness.notifier.calls == ()


def test_a_corrupt_stored_rule_stops_the_run(harness: EngineHarness) -> None:
    watch(harness, "AAPL")
    error = StoredRuleError(
        7,
        [
            RuleProblem(
                kind=RuleErrorKind.UNKNOWN_INDICATOR,
                path="conditions",
                message="the stored document names an indicator that no longer exists",
            )
        ],
    )
    instrumented = with_unit_of_work(
        harness,
        raising_unit_of_work(harness.unit_of_work, "assignments", "rules_for_ticker", error),
    )

    with pytest.raises(StoredRuleError):
        run_engine(instrumented, Timeframe.D1, NOW)

    assert harness.provider.fetch_calls == ()


def test_a_calendar_that_does_not_cover_the_window_stops_the_run(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL", "MSFT")
    error = CalendarRangeError(
        "the calendar does not cover the requested window",
        calendar="NYSE",
        first_day=date(2021, 1, 1),
        last_day=date(2027, 12, 31),
    )
    instrumented = with_provider(harness, ExplodingProvider(harness.provider, "AAPL", error))

    with pytest.raises(CalendarRangeError):
        run_engine(instrumented, Timeframe.D1, NOW)

    assert harness.notifier.calls == ()


# --- T6: the configuration changed during the run (AC15) -----------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        UntrackedTickerError("AAPL", Timeframe.D1),
        UnknownRuleError(7),
        TimeframeMismatchError(Timeframe.D1, Timeframe.H1),
    ],
)
def test_a_ticker_or_rule_removed_after_the_snapshot_is_rejected(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    watch(harness, "AAPL", "MSFT")
    harness.failures.fail_next("record", error)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    rejected = report.tickers[0].signals[0]
    assert rejected.disposition is SignalDisposition.REJECTED
    assert rejected.signal_id is None
    assert [call.key.ticker for call in harness.notifier.calls] == ["MSFT"]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        f"rejected AAPL|1d|{rejected.rule_id}|2024-07-03T04:00:00+00:00: {type(error).__name__}"
    )
    assert stored_signals(harness) != ()


def test_a_rejected_signal_stores_nothing_for_that_ticker(harness: EngineHarness) -> None:
    watch(harness, "AAPL")
    harness.failures.fail_next("record", UntrackedTickerError("AAPL", Timeframe.D1))

    run_engine(harness, Timeframe.D1, NOW)

    assert stored_signals(harness) == ()
    assert harness.notifier.calls == ()


def test_the_report_key_names_the_candle_the_run_was_about(harness: EngineHarness) -> None:
    watch(harness, "AAPL")

    report = run_engine(harness, Timeframe.D1, NOW)
    outcome = report.tickers[0].signals[0]

    assert isinstance(outcome.key, SignalKey)
    assert outcome.key.candle_close_ts == LAST_CLOSE
    assert outcome.signal_id == 1


# --- AC16: the global pause skips the whole run ------------------------------------------------


def test_a_paused_bot_does_nothing_at_all(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL", "MSFT")
    with configuration(harness) as repos:
        paused_at = repos.state.pause()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    assert report.paused is True
    assert report.tickers == ()
    assert report.now == NOW
    assert harness.provider.fetch_calls == ()
    assert harness.notifier.calls == ()
    assert stored_signals(harness) == ()
    assert [record.getMessage() for record in caplog.records] == [
        f"1d run at 2024-07-02T20:00:30+00:00: skipped, paused since {paused_at.isoformat()}"
    ]


def test_the_candles_closed_during_a_pause_are_never_evaluated_afterwards(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")
    with configuration(harness) as repos:
        repos.state.pause()
    run_engine(harness, Timeframe.D1, NOW)
    with configuration(harness) as repos:
        repos.state.resume()

    report = run_engine(harness, Timeframe.D1, LATER)

    assert [str(signal.key) for signal in report.signals] == [
        f"AAPL|1d|{report.signals[0].rule_id}|2024-07-04T04:00:00+00:00"
    ]
    assert len(stored_signals(harness)) == 1


# --- AC20: a filtered run, the seam the scheduler retries with ---------------------------------


def test_a_filtered_run_plans_only_the_requested_symbols(harness: EngineHarness) -> None:
    watch(harness, "AAPL", "MSFT")

    report = run_engine(harness, Timeframe.D1, NOW, tickers=(" aapl ",))

    assert [call.ticker for call in harness.provider.fetch_calls] == ["AAPL"]
    assert [ticker.ticker for ticker in report.tickers] == ["AAPL"]
    assert report.now == NOW


def test_a_filtered_run_ignores_symbols_that_are_not_enabled(harness: EngineHarness) -> None:
    watch(harness, "AAPL")

    report = run_engine(harness, Timeframe.D1, NOW, tickers=("NVDA",))

    assert report.tickers == ()
    assert harness.provider.fetch_calls == ()


def test_a_string_of_tickers_is_a_type_error_before_any_io(harness: EngineHarness) -> None:
    watch(harness, "AAPL")

    with pytest.raises(TypeError, match="sequence of symbols"):
        run_engine(harness, Timeframe.D1, NOW, tickers="AAPL")  # type: ignore[arg-type]

    assert harness.provider.fetch_calls == ()


def test_an_invalid_symbol_is_a_value_error_before_any_io(harness: EngineHarness) -> None:
    watch(harness, "AAPL")

    with pytest.raises(ValueError, match="invalid ticker"):
        run_engine(harness, Timeframe.D1, NOW, tickers=("AA PL",))

    assert harness.provider.fetch_calls == ()


def test_a_non_timeframe_and_a_naive_instant_are_rejected_before_any_io(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")

    with pytest.raises(TypeError, match="Timeframe"):
        run_engine(harness, "1d", NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="aware"):
        run_engine(harness, Timeframe.D1, NOW.replace(tzinfo=None))

    assert harness.provider.fetch_calls == ()


def test_a_loud_error_raised_while_evaluating_is_never_downgraded(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The evaluation ring must not turn a corrupt row into a ticker failure (decision D134)."""
    watch(harness, "AAPL", "MSFT")

    def corrupt(rule: object, candles: pd.DataFrame, **kwargs: object) -> object:
        raise StoredSignalError(11, "payload")

    monkeypatch.setattr(signal_engine, "evaluate", corrupt)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), pytest.raises(StoredSignalError):
        run_engine(harness, Timeframe.D1, NOW)

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage().endswith("stopped: StoredSignalError")

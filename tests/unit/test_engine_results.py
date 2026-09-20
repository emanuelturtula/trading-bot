"""The run report and its value objects (spec 015, T9, AC17).

Every expectation is a literal: the summary line is what F7 (#26) will parse, so it is written
out here instead of being recomputed with the code under test. No clock is read; every instant
is a literal.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_bot.domain.signals import SignalKey
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import (
    FailureKind,
    RunReport,
    SignalDisposition,
    SignalOutcome,
    TickerOutcome,
    TickerStatus,
)

NOW = datetime(2024, 7, 5, 20, 0, tzinfo=UTC)
CLOSE = datetime(2024, 7, 6, 4, 0, tzinfo=UTC)


def key(symbol: str, rule_id: str = "7") -> SignalKey:
    return SignalKey(ticker=symbol, timeframe=Timeframe.D1, rule_id=rule_id, candle_close_ts=CLOSE)


def outcome(
    symbol: str,
    disposition: SignalDisposition,
    *,
    rule_id: int = 7,
    signal_id: int | None = 1,
) -> SignalOutcome:
    return SignalOutcome(
        key=key(symbol, str(rule_id)),
        rule_id=rule_id,
        disposition=disposition,
        signal_id=signal_id,
    )


def evaluated(
    symbol: str, *, ticker_id: int = 1, rules: int = 1, signals: tuple[SignalOutcome, ...] = ()
) -> TickerOutcome:
    return TickerOutcome(
        ticker=symbol,
        ticker_id=ticker_id,
        status=TickerStatus.EVALUATED,
        failure=None,
        retryable=False,
        rules_evaluated=rules,
        signals=signals,
    )


def failed(
    symbol: str, kind: FailureKind, *, retryable: bool = False, ticker_id: int = 2
) -> TickerOutcome:
    return TickerOutcome(
        ticker=symbol,
        ticker_id=ticker_id,
        status=TickerStatus.FAILED,
        failure=kind,
        retryable=retryable,
        rules_evaluated=0,
        signals=(),
    )


# --- The enumerations ----------------------------------------------------------------------


def test_the_enumerations_hold_exactly_the_documented_values() -> None:
    assert [member.value for member in FailureKind] == [
        "invalid_ticker",
        "no_data",
        "not_published",
        "provider_data",
        "unavailable",
        "unexpected",
    ]
    assert [member.value for member in TickerStatus] == ["evaluated", "failed"]
    assert [member.value for member in SignalDisposition] == [
        "notified",
        "undelivered",
        "duplicate",
        "cooldown",
        "superseded",
        "rejected",
    ]


# --- Frozen, slotted, keyword-only records -------------------------------------------------


@pytest.mark.parametrize("record_type", [SignalOutcome, TickerOutcome, RunReport])
def test_every_record_is_frozen_slotted_and_keyword_only(record_type: type) -> None:
    parameters = dataclasses.fields(record_type)

    assert record_type.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert record_type.__dataclass_params__.kw_only is True  # type: ignore[attr-defined]
    assert "__slots__" in record_type.__dict__
    assert parameters != ()


def test_a_signal_outcome_is_immutable_and_equal_by_value() -> None:
    first = outcome("AAPL", SignalDisposition.NOTIFIED)
    second = outcome("AAPL", SignalDisposition.NOTIFIED)

    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.disposition = SignalDisposition.DUPLICATE  # type: ignore[misc]


def test_a_ticker_outcome_must_carry_a_failure_exactly_when_it_failed() -> None:
    with pytest.raises(ValueError, match="failure"):
        TickerOutcome(
            ticker="AAPL",
            ticker_id=1,
            status=TickerStatus.EVALUATED,
            failure=FailureKind.NO_DATA,
            retryable=False,
            rules_evaluated=0,
            signals=(),
        )
    with pytest.raises(ValueError, match="failure"):
        TickerOutcome(
            ticker="AAPL",
            ticker_id=1,
            status=TickerStatus.FAILED,
            failure=None,
            retryable=False,
            rules_evaluated=0,
            signals=(),
        )


# --- The report ----------------------------------------------------------------------------


def test_the_report_keeps_the_argument_instant_in_utc() -> None:
    report = RunReport(
        timeframe=Timeframe.D1,
        now=NOW.astimezone(timezone(timedelta(hours=2))),
        paused=False,
        tickers=(),
    )

    assert report.now == NOW
    assert report.now.tzinfo is UTC
    assert report.now.isoformat() == "2024-07-05T20:00:00+00:00"


def test_a_naive_instant_is_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        RunReport(timeframe=Timeframe.D1, now=NOW.replace(tzinfo=None), paused=False, tickers=())


def test_the_ticker_outcomes_keep_the_order_they_were_built_in() -> None:
    report = RunReport(
        timeframe=Timeframe.D1,
        now=NOW,
        paused=False,
        tickers=(evaluated("MSFT"), evaluated("AAPL"), failed("SPY", FailureKind.NO_DATA)),
    )

    assert [ticker.ticker for ticker in report.tickers] == ["MSFT", "AAPL", "SPY"]


def test_the_derived_properties_count_what_the_run_produced() -> None:
    report = RunReport(
        timeframe=Timeframe.D1,
        now=NOW,
        paused=False,
        tickers=(
            evaluated(
                "AAPL",
                rules=3,
                signals=(
                    outcome("AAPL", SignalDisposition.NOTIFIED),
                    outcome("AAPL", SignalDisposition.DUPLICATE, rule_id=9),
                ),
            ),
            failed("SPY", FailureKind.NOT_PUBLISHED, retryable=True),
            failed("TSLA", FailureKind.INVALID_TICKER),
        ),
    )

    assert report.notified == 1
    assert [ticker.ticker for ticker in report.failures] == ["SPY", "TSLA"]
    assert report.retryable_tickers == ("SPY",)


def test_retryable_tickers_is_empty_when_nothing_retryable_failed() -> None:
    report = RunReport(
        timeframe=Timeframe.H1,
        now=NOW,
        paused=False,
        tickers=(evaluated("AAPL"), failed("SPY", FailureKind.PROVIDER_DATA)),
    )

    assert report.retryable_tickers == ()
    assert report.notified == 0


def test_the_summary_of_a_full_run_is_the_documented_line() -> None:
    tickers = [evaluated(f"T{index}", ticker_id=index, rules=4) for index in range(11)]
    tickers[0] = evaluated(
        "AAPL",
        rules=3,
        signals=(
            outcome("AAPL", SignalDisposition.NOTIFIED),
            outcome("AAPL", SignalDisposition.NOTIFIED, rule_id=8),
            outcome("AAPL", SignalDisposition.DUPLICATE, rule_id=9),
        ),
    )
    report = RunReport(
        timeframe=Timeframe.D1,
        now=NOW,
        paused=False,
        tickers=(*tickers, failed("SPY", FailureKind.NOT_PUBLISHED, retryable=True)),
    )

    assert report.summary == (
        "1d run at 2024-07-05T20:00:00+00:00: 12 tickers, 43 rules,"
        " 3 signals (2 notified, 1 duplicate), 1 failed (1 retryable)"
    )


def test_the_summary_of_a_run_without_signals_or_failures() -> None:
    report = RunReport(
        timeframe=Timeframe.H4,
        now=NOW,
        paused=False,
        tickers=(evaluated("AAPL", rules=2),),
    )

    assert report.summary == (
        "4h run at 2024-07-05T20:00:00+00:00: 1 tickers, 2 rules, 0 signals, 0 failed"
    )


def test_the_summary_lists_the_dispositions_in_their_declared_order() -> None:
    report = RunReport(
        timeframe=Timeframe.D1,
        now=NOW,
        paused=False,
        tickers=(
            evaluated(
                "AAPL",
                rules=5,
                signals=(
                    outcome("AAPL", SignalDisposition.REJECTED, rule_id=5, signal_id=None),
                    outcome("AAPL", SignalDisposition.SUPERSEDED, rule_id=4, signal_id=None),
                    outcome("AAPL", SignalDisposition.COOLDOWN, rule_id=3, signal_id=None),
                    outcome("AAPL", SignalDisposition.UNDELIVERED, rule_id=2),
                    outcome("AAPL", SignalDisposition.NOTIFIED, rule_id=1),
                ),
            ),
        ),
    )

    assert report.summary == (
        "1d run at 2024-07-05T20:00:00+00:00: 1 tickers, 5 rules, 5 signals"
        " (1 notified, 1 undelivered, 1 cooldown, 1 superseded, 1 rejected), 0 failed"
    )


def test_the_summary_of_a_paused_run() -> None:
    report = RunReport(timeframe=Timeframe.D1, now=NOW, paused=True, tickers=())

    assert report.summary == "1d run at 2024-07-05T20:00:00+00:00: skipped, paused"
    assert report.notified == 0
    assert report.failures == ()
    assert report.retryable_tickers == ()

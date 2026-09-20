"""Reprocessing a candle never creates or sends a duplicate (spec 015, T7, AC9, AC10).

The core of the feature and of CLAUDE.md rule 5. Every test asserts **both** halves: no new row
and no notifier call. The database is a temporary one under ``tmp_path``; a "restart" disposes
the handle and reopens the same directory, which is the only way to observe that the claim, not
a process-local memory, is what prevents a resend.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    ENGINE_CLOCK,
    EngineHarness,
    configuration,
    engine_harness,
    publish,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import SignalDisposition
from trading_bot.notifications.notifier import NotificationError
from trading_bot.persistence.signal_records import StoredSignal

NOW = utc("2024-07-02T20:00:30")  # just after the close of the 2024-07-02 daily candle
NEXT = utc("2024-07-03T17:00:30")  # just after the close of the 2024-07-03 half day
EARLIER = utc("2024-06-28T20:00:30")  # a replay of an older candle
HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-05T00:00")
LOGGER_NAME = "trading_bot.engine"


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    with engine_harness(tmp_path) as built:
        yield built


def watch(harness: EngineHarness, *, cooldown_bars: int = 0) -> None:
    track(
        harness,
        "AAPL",
        Timeframe.D1,
        [triggering_rule("Alpha", cooldown_bars=cooldown_bars)],
    )
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)


def dispositions(harness: EngineHarness, *, now: object = NOW) -> list[SignalDisposition]:
    report = run_engine(harness, Timeframe.D1, now)  # type: ignore[arg-type]
    return [signal.disposition for signal in report.signals]


def only(signals: tuple[StoredSignal, ...]) -> StoredSignal:
    assert len(signals) == 1
    return signals[0]


# --- AC9: the same run twice ------------------------------------------------------------------


def test_a_second_run_of_the_same_candle_creates_no_row_and_sends_nothing(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness)
    assert dispositions(harness) == [SignalDisposition.NOTIFIED]
    first = only(stored_signals(harness))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        second = dispositions(harness)

    assert second == [SignalDisposition.DUPLICATE]
    assert only(stored_signals(harness)) == first
    assert len(harness.notifier.calls) == 1
    assert any(
        record.getMessage() == f"duplicate {first.key}: already recorded as signal {first.id}"
        for record in caplog.records
    )


def test_a_rerun_never_moves_the_delivery_instant(harness: EngineHarness) -> None:
    watch(harness)
    dispositions(harness)

    dispositions(harness)

    assert only(stored_signals(harness)).notified_at == ENGINE_CLOCK


def test_a_restart_does_not_resend(tmp_path: Path) -> None:
    with engine_harness(tmp_path) as first:
        watch(first)
        assert dispositions(first) == [SignalDisposition.NOTIFIED]
        before = stored_signals(first)
    # The handle is disposed here: the next harness reopens the same directory, as a restart
    # of the process does.
    with engine_harness(tmp_path) as second:
        publish(second, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)

        assert dispositions(second) == [SignalDisposition.DUPLICATE]
        assert stored_signals(second) == before
        assert second.notifier.calls == ()


def test_a_run_whose_candle_is_older_than_the_last_signal_is_superseded(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness)
    dispositions(harness)
    before = stored_signals(harness)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        replay = dispositions(harness, now=EARLIER)

    assert replay == [SignalDisposition.SUPERSEDED]
    assert stored_signals(harness) == before
    assert len(harness.notifier.calls) == 1
    assert any("superseded AAPL|1d|" in record.getMessage() for record in caplog.records)


# --- AC10: an interrupted delivery is never re-sent ---------------------------------------------


def test_a_failed_delivery_leaves_the_row_unstamped_and_is_never_re_sent(
    harness: EngineHarness,
) -> None:
    watch(harness)
    harness.notifier.fail_next(NotificationError("the transport gave up"))

    assert dispositions(harness) == [SignalDisposition.UNDELIVERED]
    stored = only(stored_signals(harness))
    assert stored.notified_at is None

    assert dispositions(harness) == [SignalDisposition.DUPLICATE]
    assert only(stored_signals(harness)).notified_at is None
    assert len(harness.notifier.calls) == 1  # the failed attempt, and nothing since


def test_the_next_candle_is_a_new_key_and_is_notified(harness: EngineHarness) -> None:
    watch(harness)
    harness.notifier.fail_next(NotificationError("the transport gave up"))
    dispositions(harness)

    assert dispositions(harness, now=NEXT) == [SignalDisposition.NOTIFIED]

    keys = sorted(str(stored.key) for stored in stored_signals(harness))
    rule_id = stored_signals(harness)[0].rule_id
    assert keys == [
        f"AAPL|1d|{rule_id}|2024-07-03T04:00:00+00:00",
        f"AAPL|1d|{rule_id}|2024-07-04T04:00:00+00:00",
    ]
    assert [call.key.candle_close_ts.isoformat() for call in harness.notifier.calls] == [
        "2024-07-03T04:00:00+00:00",
        "2024-07-04T04:00:00+00:00",
    ]


def test_a_cooldown_of_one_candle_suppresses_the_next_one(harness: EngineHarness) -> None:
    watch(harness, cooldown_bars=1)

    assert dispositions(harness) == [SignalDisposition.NOTIFIED]
    assert dispositions(harness, now=NEXT) == [SignalDisposition.COOLDOWN]

    assert len(stored_signals(harness)) == 1
    assert len(harness.notifier.calls) == 1


def test_a_cooldown_counts_from_the_recorded_signal_not_the_delivered_one(
    harness: EngineHarness,
) -> None:
    """Decision D130: one delivery failure must not turn into a burst."""
    watch(harness, cooldown_bars=1)
    harness.notifier.fail_next(NotificationError("the transport gave up"))

    assert dispositions(harness) == [SignalDisposition.UNDELIVERED]
    assert dispositions(harness, now=NEXT) == [SignalDisposition.COOLDOWN]

    assert len(harness.notifier.calls) == 1


def test_the_configuration_can_change_between_two_runs(harness: EngineHarness) -> None:
    """Decision D126: a change takes effect at the next run, and never mid-run."""
    watch(harness)
    dispositions(harness)
    with configuration(harness) as repos:
        ticker = repos.tickers.get_by_symbol("AAPL", Timeframe.D1)
        assert ticker is not None
        repos.tickers.set_enabled(ticker.id, False)

    report = run_engine(harness, Timeframe.D1, NEXT)

    assert report.tickers == ()
    assert len(stored_signals(harness)) == 1
    assert len(harness.notifier.calls) == 1


def test_a_revised_candle_keeps_the_stored_signal_and_says_so(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """The first write wins (spec 014, D112): the record reports the difference, never hides it."""
    watch(harness)
    assert dispositions(harness) == [SignalDisposition.NOTIFIED]
    first = only(stored_signals(harness))
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END, seed=7)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        second = dispositions(harness)

    assert second == [SignalDisposition.DUPLICATE]
    assert only(stored_signals(harness)) == first
    assert len(harness.notifier.calls) == 1
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.getMessage() for record in warnings] == [
        f"duplicate {first.key}: signal {first.id} was recorded from different values;"
        " the stored one stands"
    ]

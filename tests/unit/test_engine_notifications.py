"""What a notification carries and what a failed delivery costs (spec 015, T8, AC18, AC22).

No secret exists in this feature and no ``TB_*`` variable is added, so the redaction check is
made with a token-shaped string **built at runtime** (CLAUDE.md rule 2) inside a notifier
exception: neither it nor the exception text may reach a log record, because the engine names
the exception class only (decision D135).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    EngineHarness,
    configuration,
    engine_harness,
    publish,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from tests.fixtures.notifiers import FakeNotifier, NotifyCall, as_notifier
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine import signal_engine
from trading_bot.engine.results import SignalDisposition
from trading_bot.notifications.notifier import NotificationError, Notifier, SignalNotification

NOW = utc("2024-07-02T20:00:30")
HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-05T00:00")
LOGGER_NAME = "trading_bot.engine"


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    with engine_harness(tmp_path) as built:
        yield built


def watch(harness: EngineHarness, *symbols: str) -> None:
    for symbol in symbols:
        track(harness, symbol, Timeframe.D1, [triggering_rule("Alpha", side=Side.SELL)])
        publish(harness, symbol, Timeframe.D1, HISTORY_START, HISTORY_END)


def fake_token() -> str:
    """A token-shaped string built at runtime, so no literal reaches the repository."""
    return "123456789" + ":" + "x" * 35


# --- AC18: the port and what it delivers -------------------------------------------------------


def test_the_notification_carries_the_committed_row_the_rule_and_the_frame(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")
    with configuration(harness) as repos:
        stored_rule = repos.rules.get_by_name("Alpha")
    assert stored_rule is not None

    run_engine(harness, Timeframe.D1, NOW)

    notification = harness.notifier.notifications[0]
    stored = stored_signals(harness)[0]
    assert isinstance(notification, SignalNotification)
    # The row as committed, before the stamp: only ``notified_at`` differs afterwards.
    assert notification.signal == replace(stored, notified_at=None)
    assert notification.signal.notified_at is None
    assert notification.signal.id == stored.id
    assert notification.signal.signal == stored.signal
    assert notification.signal.rule_name == "Alpha"
    assert notification.rule == stored_rule.rule
    assert notification.rule.signal is Side.SELL
    assert isinstance(notification.candles, pd.DataFrame)
    assert len(notification.candles) == 20
    assert notification.candles.index[-1] == utc("2024-07-02T04:00")


def test_the_frame_is_shared_with_the_notifier_and_never_copied(
    harness: EngineHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch(harness, "AAPL")
    seen: list[pd.DataFrame] = []
    real_evaluate = signal_engine.evaluate

    def remembering(rule: object, candles: pd.DataFrame, **kwargs: object) -> object:
        seen.append(candles)
        return real_evaluate(rule, candles, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(signal_engine, "evaluate", remembering)

    run_engine(harness, Timeframe.D1, NOW)

    assert harness.notifier.frames[0] is seen[0]
    assert harness.notifier.mutated_frames == ()


def test_the_recorded_call_names_the_key_the_row_the_rule_and_the_last_label(
    harness: EngineHarness,
) -> None:
    watch(harness, "AAPL")

    report = run_engine(harness, Timeframe.D1, NOW)

    stored = stored_signals(harness)[0]
    assert harness.notifier.calls == (
        NotifyCall(
            key=stored.key,
            signal_id=stored.id,
            rule_name="Alpha",
            last_label=utc("2024-07-02T04:00"),
        ),
    )
    assert report.signals[0].signal_id == stored.id


def test_the_fake_notifier_conforms_to_the_port() -> None:
    notifier: Notifier = as_notifier(FakeNotifier())

    assert callable(notifier.notify)


# --- AC18: a failed delivery -------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        NotificationError("the transport gave up"),
        RuntimeError("an implementation bug"),
        TimeoutError("the transport never answered"),
    ],
)
def test_any_notifier_failure_gives_undelivered_and_lets_the_run_continue(
    harness: EngineHarness, error: Exception
) -> None:
    watch(harness, "AAPL", "MSFT")
    harness.notifier.fail_next(error)

    report = run_engine(harness, Timeframe.D1, NOW)

    assert [signal.disposition for signal in report.signals] == [
        SignalDisposition.UNDELIVERED,
        SignalDisposition.NOTIFIED,
    ]
    assert [stored.notified_at is None for stored in stored_signals(harness)] == [False, True]
    assert report.notified == 1


def test_a_failed_delivery_is_one_error_record_naming_only_the_exception_class(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL")
    harness.notifier.fail_next(NotificationError("the transport gave up"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    key = report.signals[0].key
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage() == f"delivery failed for {key}: NotificationError"
    assert "the transport gave up" not in caplog.text


# --- AC22: nothing a notifier says reaches the log ----------------------------------------------


def test_a_token_shaped_exception_message_never_reaches_the_log(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL")
    token = fake_token()
    harness.notifier.fail_next(NotificationError(f"POST https://example.invalid/bot{token}/send"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        run_engine(harness, Timeframe.D1, NOW)

    assert token not in caplog.text
    assert "example.invalid" not in caplog.text


@pytest.mark.parametrize("failing", [True, False])
def test_no_record_of_a_run_carries_a_traceback(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture, failing: bool
) -> None:
    watch(harness, "AAPL")
    if failing:
        harness.notifier.fail_next(NotificationError("boom " + fake_token()))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        run_engine(harness, Timeframe.D1, NOW)

    assert caplog.records != []
    assert all(record.exc_info is None for record in caplog.records)
    assert all(record.stack_info is None for record in caplog.records)


def test_the_notified_record_names_only_safe_fields(
    harness: EngineHarness, caplog: pytest.LogCaptureFixture
) -> None:
    watch(harness, "AAPL")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        report = run_engine(harness, Timeframe.D1, NOW)

    stored = stored_signals(harness)[0]
    close = stored.signal.close_price
    assert [record.getMessage() for record in caplog.records] == [
        f"notified {stored.key} (SELL, close {close})",
        report.summary,
    ]

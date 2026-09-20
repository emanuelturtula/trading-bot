"""The real engine driven by ``TimeframeRunner`` over a session week (spec 016, T11).

AAPL and MSFT, ``1d``, one rule each that always fires and never cools down, over the same
holiday week as spec 015's own end-to-end test (2024-07-04 is a holiday): every closed candle
is evaluated once, a ticker whose candle is not yet published is retried with the **same**
scheduled close and notified exactly once when it appears, a paused fire sends nothing and is
never replayed once resumed, and a restart (a fresh provider and a fresh ``TimeframeRunner``
over the same database) notifies nothing twice. ``bot_state`` ends with the last fire's close.

Every fire time and every expectation below is a literal, never recomputed with ``next_fire`` or
any other code under test (spec 016, Testing rules).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    EngineHarness,
    configuration,
    engine_harness,
    publish,
    track,
    triggering_rule,
)
from tests.fixtures.scheduler import ManualClock, recording_sleep
from trading_bot.data.errors import CandleNotPublishedError, UnpublishedReason
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.state import LastRun
from trading_bot.scheduler.policy import DEFAULT_POLICY
from trading_bot.scheduler.runner import FireStatus, RunAttempt, TimeframeRunner

HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-12T00:00")

MONDAY_LABEL = utc("2024-07-01T04:00")  # the 1d slot label of the Monday session
MONDAY_CLOSE = utc("2024-07-01T20:00")
TUESDAY_CLOSE = utc("2024-07-02T20:00")
HALF_DAY_CLOSE = utc("2024-07-03T17:00")  # early close, the day before the 07-04 holiday
FRIDAY_CLOSE = utc("2024-07-05T20:00")  # 2024-07-04 is a holiday

DELAY = DEFAULT_POLICY.close_delay  # 120 s


def watch(harness: EngineHarness) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha")])
    track(harness, "MSFT", Timeframe.D1, [triggering_rule("Beta")])


def publish_both(harness: EngineHarness) -> None:
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)
    publish(harness, "MSFT", Timeframe.D1, HISTORY_START, HISTORY_END)


def build_runner(harness: EngineHarness, clock: ManualClock) -> tuple[TimeframeRunner, list[float]]:
    sleep, waits = recording_sleep(clock)
    runner = TimeframeRunner(
        engine=harness.engine,
        calendar=harness.calendar,
        unit_of_work=harness.unit_of_work,
        policy=DEFAULT_POLICY,
        clock=clock,
        sleep=sleep,
    )
    return runner, waits


def fire_at(runner: TimeframeRunner, clock: ManualClock, close: datetime) -> RunAttempt:
    """Jump the clock to the real fire time of ``close`` (the pinned close plus the delay)."""
    clock.set(close + DELAY)
    return asyncio.run(runner.run_timeframe(Timeframe.D1))


def notified(harness: EngineHarness) -> list[str]:
    return [call.key.ticker for call in harness.notifier.calls]


def test_a_week_with_a_retry_a_pause_and_a_restart_notifies_exactly_once(
    tmp_path: Path,
) -> None:
    with engine_harness(tmp_path) as harness:
        watch(harness)
        publish_both(harness)
        # MSFT's Monday candle is not published yet: the first fetch of the run fails with the
        # retryable error, and the runner retries only MSFT with the same scheduled close.
        harness.provider.fail_next(
            CandleNotPublishedError(
                UnpublishedReason.MISSING,
                ticker="MSFT",
                timeframe=Timeframe.D1,
                expected_label=MONDAY_LABEL,
                last_label=None,
            ),
            ticker="MSFT",
        )
        clock = ManualClock(MONDAY_CLOSE + DELAY)
        runner, waits = build_runner(harness, clock)

        monday = fire_at(runner, clock, MONDAY_CLOSE)

        assert monday.status is FireStatus.RAN
        assert monday.engine_calls == 2  # the full run, then the MSFT-only retry
        assert monday.unresolved == ()
        assert waits == [30.0]  # the first backoff step resolves it; no second wait
        assert notified(harness) == ["AAPL", "MSFT"]  # MSFT notified once, not twice

        tuesday = fire_at(runner, clock, TUESDAY_CLOSE)

        assert tuesday.status is FireStatus.RAN
        assert tuesday.engine_calls == 1  # both tickers publish on time; no retry
        assert notified(harness) == ["AAPL", "MSFT", "AAPL", "MSFT"]

        with configuration(harness) as repos:
            repos.state.pause()
        notified_before_the_pause = len(harness.notifier.calls)

        half_day = fire_at(runner, clock, HALF_DAY_CLOSE)

        assert half_day.status is FireStatus.RAN  # U4: still recorded, still RAN
        assert half_day.report is not None
        assert half_day.report.paused is True
        assert half_day.engine_calls == 1
        assert len(harness.notifier.calls) == notified_before_the_pause  # nothing sent, paused

        with configuration(harness) as repos:
            repos.state.resume()

        # The candle closed during the pause is never evaluated afterwards (spec 015, U3): a
        # second fire for the same close, even once resumed, is a no-op.
        replayed_half_day = fire_at(runner, clock, HALF_DAY_CLOSE)

        assert replayed_half_day.status is FireStatus.ALREADY_RUN
        assert len(harness.notifier.calls) == notified_before_the_pause

    # The process restarts: a fresh provider is wired, but the database and its history, the
    # ticker/rule configuration and bot_state all survive under the same data directory.
    with engine_harness(tmp_path) as harness:
        publish_both(harness)
        clock = ManualClock(FRIDAY_CLOSE + DELAY)
        runner, waits = build_runner(harness, clock)

        friday = fire_at(runner, clock, FRIDAY_CLOSE)

        assert friday.status is FireStatus.RAN
        assert notified(harness) == ["AAPL", "MSFT"]  # a fresh notifier for this process

        # Re-firing Monday's close after the restart must not resend it: it is exactly the
        # close a coalesced burst or a crash-and-restart replays.
        replayed_monday = fire_at(runner, clock, MONDAY_CLOSE)

        assert replayed_monday.status is FireStatus.ALREADY_RUN
        assert notified(harness) == ["AAPL", "MSFT"]  # unchanged: nothing new, nothing twice

        with configuration(harness) as repos:
            state = repos.state.load()

        assert state.last_run(Timeframe.D1) == LastRun(
            timeframe=Timeframe.D1, scheduled_at=FRIDAY_CLOSE, completed_at=harness.clock()
        )

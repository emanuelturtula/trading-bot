"""One fire of one timeframe: the candle, the retries, the locks and the errors (T4-T7).

Every instant is a literal or a ``ManualClock`` reading and every wait is recorded instead of
endured, so the whole file runs in milliseconds and nothing depends on when the suite runs.

The reference session is 2024-07-05 (Friday, EDT, regular): 13:30-20:00Z. Its last ``1h`` slot
is 19:30-20:00Z, its ``1d`` slot is labelled 04:00Z and both close at 20:00Z, so the fire time
with the default 120 s delay is 20:02Z.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.scheduler import (
    STATE_CLOCK,
    SchedulerHarness,
    as_signal_runner,
    failing_report,
    fire,
    paused_report,
    quiet_report,
    scheduler_harness,
)
from trading_bot.domain.market_calendar.sessions import CandleSlot
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.unit_of_work import EngineRepositories
from trading_bot.persistence.errors import StoredRuleError
from trading_bot.persistence.state import LastRun
from trading_bot.scheduler.policy import SchedulerPolicy
from trading_bot.scheduler.runner import FireStatus, RunAttempt, TimeframeRunner
from trading_bot.scheduler.slots import skip_unpublished_hours

LOGGER_NAME = "trading_bot.scheduler"

CLOSE = utc("2024-07-05T20:00")  # the real close of the 2024-07-05 session
FIRE = utc("2024-07-05T20:02")  # CLOSE + the default 120 s delay
PREVIOUS_CLOSE = utc("2024-07-03T17:00")  # the half day before the 2024-07-04 holiday

# The window is close + close_delay + misfire_grace = 20:00 + 120 s + 900 s.
LAST_INSTANT_INSIDE_THE_WINDOW = utc("2024-07-05T20:17")
STALE_INSTANT = utc("2024-07-05T20:32:30")  # 1830 s after the fire time

# A guard that turns a deadlock into a failure. It is never compared with an elapsed time: on
# the passing path nothing waits at all.
DEADLOCK_GUARD_SECONDS = 10.0


def stored_last_run(harness: SchedulerHarness, timeframe: Timeframe) -> LastRun | None:
    with harness.unit_of_work() as repositories:
        return repositories.state.load().last_run(timeframe)


def record_previous_run(
    harness: SchedulerHarness, timeframe: Timeframe, scheduled_close: datetime
) -> None:
    with harness.unit_of_work() as repositories:
        repositories.state.record_run(timeframe, scheduled_close)


def messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The scheduler's records of one level; another logger's records are never asserted on."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == LOGGER_NAME
    ]


# --- T4: one run per fire (AC6, AC7, AC8, AC13) ---------------------------------------------


def test_the_engine_receives_the_scheduled_close_not_the_firing_time(tmp_path: Path) -> None:
    """AC6 and spec 015 hand-off 1: ``now`` is the close, an instant at or before the clock."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = fire(harness, Timeframe.D1)

        assert len(harness.engine.calls) == 1
        call = harness.engine.calls[0]
        assert call.timeframe is Timeframe.D1
        assert call.now == CLOSE
        assert call.now <= FIRE
        assert call.now.tzinfo is not None
        assert call.tickers is None
        assert attempt.status is FireStatus.RAN


def test_the_hourly_fire_evaluates_the_last_hourly_candle(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = fire(harness, Timeframe.H1)

        assert harness.engine.calls[0].now == CLOSE
        assert attempt.scheduled_close == CLOSE


def test_a_clock_far_past_several_closes_still_runs_only_the_last_candle(tmp_path: Path) -> None:
    """Decision D128: a run is never a backfill, whatever the fire missed."""
    wide = SchedulerPolicy(misfire_grace=timedelta(hours=1))
    with scheduler_harness(tmp_path, now=utc("2024-07-05T20:50"), policy=wide) as harness:
        attempt = fire(harness, Timeframe.H1)

        assert [call.now for call in harness.engine.calls] == [CLOSE]
        assert attempt.status is FireStatus.RAN


def test_a_completed_run_is_recorded_once_after_the_engine(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = fire(harness, Timeframe.D1)

        assert attempt.status is FireStatus.RAN
        assert stored_last_run(harness, Timeframe.D1) == LastRun(
            timeframe=Timeframe.D1, scheduled_at=CLOSE, completed_at=STATE_CLOCK
        )


def test_a_second_fire_for_the_same_close_runs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC7, CLAUDE.md rule 5: two fires for one close produce exactly one engine run."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        first = fire(harness, Timeframe.D1)
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            second = fire(harness, Timeframe.D1)

        assert first.status is FireStatus.RAN
        assert second.status is FireStatus.ALREADY_RUN
        assert second.engine_calls == 0
        assert second.report is None
        assert len(harness.engine.calls) == 1
        assert "1d fire for 2024-07-05T20:00:00+00:00 skipped: already run" in messages(
            caplog, logging.DEBUG
        )


def test_a_recorded_run_later_than_the_close_also_skips(tmp_path: Path) -> None:
    """A clock stepped backwards by NTP must not produce a second run (decision D144)."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        record_previous_run(harness, Timeframe.H1, utc("2024-07-08T14:30"))

        attempt = fire(harness, Timeframe.H1)

        assert attempt.status is FireStatus.ALREADY_RUN
        assert harness.engine.calls == ()


def test_a_recorded_run_older_than_the_close_still_runs(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        record_previous_run(harness, Timeframe.D1, PREVIOUS_CLOSE)

        attempt = fire(harness, Timeframe.D1)

        assert attempt.status is FireStatus.RAN
        assert harness.engine.calls[0].now == CLOSE


def test_a_fire_inside_the_misfire_window_runs(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=LAST_INSTANT_INSIDE_THE_WINDOW) as harness:
        assert fire(harness, Timeframe.H1).status is FireStatus.RAN


def test_a_fire_outside_the_misfire_window_is_stale(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC8: a bot that comes back much later does not send that candle as if it were fresh."""
    with scheduler_harness(tmp_path, now=STALE_INSTANT) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.H1)

        assert stored_last_run(harness, Timeframe.H1) is None
        assert attempt.status is FireStatus.STALE
        assert attempt.scheduled_close == CLOSE
        assert attempt.engine_calls == 0
        assert harness.engine.calls == ()
        assert messages(caplog, logging.WARNING) == [
            "1h fire for 2024-07-05T20:00:00+00:00 skipped: stale by 1830s"
        ]


def test_the_boundary_of_the_misfire_window_is_the_last_instant_that_runs(
    tmp_path: Path,
) -> None:
    just_outside = LAST_INSTANT_INSIDE_THE_WINDOW + timedelta(microseconds=1)
    with scheduler_harness(tmp_path, now=just_outside) as harness:
        assert fire(harness, Timeframe.H1).status is FireStatus.STALE


def test_a_slot_the_predicate_rejects_is_not_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC4/D150: the truncated 16:30-17:00Z slot of the 2024-07-03 half day."""
    predicate = skip_unpublished_hours(nyse_test_calendar())
    with scheduler_harness(tmp_path, now=utc("2024-07-03T17:02"), should_run=predicate) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.H1)

        assert attempt.status is FireStatus.SLOT_SKIPPED
        assert attempt.scheduled_close == utc("2024-07-03T17:00")
        assert harness.engine.calls == ()
        assert "1h fire for 2024-07-03T17:00:00+00:00 skipped: slot not evaluated" in messages(
            caplog, logging.DEBUG
        )


def test_the_run_attempt_holds_only_values(tmp_path: Path) -> None:
    """AC13: no exception object and no provider text ever reaches a ``RunAttempt``."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(quiet_report(Timeframe.D1, CLOSE, "AAPL"))

        attempt = fire(harness, Timeframe.D1)

        assert attempt.timeframe is Timeframe.D1
        assert attempt.scheduled_close == CLOSE
        assert attempt.status is FireStatus.RAN
        assert attempt.engine_calls == 1
        assert attempt.unresolved == ()
        assert attempt.report is not None
        assert attempt.report.timeframe is Timeframe.D1
        assert not hasattr(attempt, "__dict__")
        with pytest.raises(AttributeError):
            attempt.status = FireStatus.FAILED  # type: ignore[misc]
        with pytest.raises(TypeError):
            RunAttempt(Timeframe.D1)  # type: ignore[call-arg,misc]


def test_the_catch_up_run_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        record_previous_run(harness, Timeframe.D1, PREVIOUS_CLOSE)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.D1, catch_up=True)

        assert attempt.status is FireStatus.RAN
        assert messages(caplog, logging.INFO) == [
            "1d catch-up run for 2024-07-05T20:00:00+00:00 (last run 2024-07-03T17:00:00+00:00)"
        ]


def test_the_first_catch_up_run_of_a_fresh_database_says_never(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            fire(harness, Timeframe.D1, catch_up=True)

        assert messages(caplog, logging.INFO) == [
            "1d catch-up run for 2024-07-05T20:00:00+00:00 (last run never)"
        ]


def test_a_scheduled_fire_logs_no_run_line_of_its_own(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The engine already logs ``report.summary``; the scheduler does not repeat it."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            fire(harness, Timeframe.D1)

        assert messages(caplog, logging.INFO) == []


def test_a_timeframe_of_the_wrong_type_is_rejected(tmp_path: Path) -> None:
    with (
        scheduler_harness(tmp_path, now=FIRE) as harness,
        pytest.raises(TypeError, match="timeframe"),
    ):
        asyncio.run(harness.runner.run_timeframe("1d"))  # type: ignore[arg-type]


def test_the_runner_rejects_arguments_of_the_wrong_type(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with pytest.raises(TypeError, match="calendar"):
            TimeframeRunner(
                engine=as_signal_runner(harness.engine),
                calendar="NYSE",  # type: ignore[arg-type]
                unit_of_work=harness.unit_of_work,
            )
        with pytest.raises(TypeError, match="policy"):
            TimeframeRunner(
                engine=as_signal_runner(harness.engine),
                calendar=harness.calendar,
                unit_of_work=harness.unit_of_work,
                policy=900,  # type: ignore[arg-type]
            )


# --- T5: the bounded retry window (AC9, AC11) -----------------------------------------------

GIVE_UP_POLICY = SchedulerPolicy(
    retry_backoff=(timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=120))
)


def test_only_the_retryable_tickers_are_retried_with_the_same_now(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(failing_report(Timeframe.H1, CLOSE, "AAPL", "MSFT"), times=2)
        harness.engine.script(quiet_report(Timeframe.H1, CLOSE, "AAPL", "MSFT"))

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.H1)

        assert [call.tickers for call in harness.engine.calls] == [
            None,
            ("AAPL", "MSFT"),
            ("AAPL", "MSFT"),
        ]
        assert {call.now for call in harness.engine.calls} == {CLOSE}
        assert harness.waits == [30.0, 60.0]
        assert attempt.status is FireStatus.RAN
        assert attempt.engine_calls == 3
        assert attempt.unresolved == ()
        assert messages(caplog, logging.INFO) == [
            "1h run for 2024-07-05T20:00:00+00:00: resolved 2 tickers after 2 retries"
        ]
        assert [line for line in messages(caplog, logging.DEBUG) if "retry" in line] == [
            "1h retry 0 in 30s",
            "1h retry 1 in 60s",
        ]


def test_a_clean_report_is_never_retried(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = fire(harness, Timeframe.D1)

        assert attempt.engine_calls == 1
        assert harness.waits == []


def test_a_failure_that_is_not_retryable_is_never_retried(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(failing_report(Timeframe.D1, CLOSE, "AAPL", retryable=False))

        attempt = fire(harness, Timeframe.D1)

        assert attempt.engine_calls == 1
        assert attempt.unresolved == ()
        assert harness.waits == []


def test_the_retry_budget_ends_with_one_warning_naming_the_symbols(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with scheduler_harness(tmp_path, now=FIRE, policy=GIVE_UP_POLICY) as harness:
        harness.engine.script(failing_report(Timeframe.H1, CLOSE, "AAPL", "MSFT"), times=4)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.H1)

        assert stored_last_run(harness, Timeframe.H1) is not None
        assert attempt.status is FireStatus.RAN
        assert attempt.engine_calls == 4
        assert attempt.unresolved == ("AAPL", "MSFT")
        assert harness.waits == [30.0, 60.0, 120.0]
        assert messages(caplog, logging.WARNING) == [
            "1h run for 2024-07-05T20:00:00+00:00: gave up on 2 tickers after 4 attempts"
            " (AAPL, MSFT)"
        ]


def test_the_retry_window_bounds_the_attempts(tmp_path: Path) -> None:
    """The ten-minute budget, the bound that stops a pointless retry of a delisted symbol."""
    short = SchedulerPolicy(retry_window=timedelta(minutes=2))
    with scheduler_harness(tmp_path, now=FIRE, policy=short) as harness:
        harness.engine.script(failing_report(Timeframe.D1, CLOSE, "AAPL"), times=5)

        attempt = fire(harness, Timeframe.D1)

        assert attempt.engine_calls == 3
        assert harness.waits == [30.0, 60.0]  # a third wait would end at 20:05:30, past 20:04
        assert attempt.unresolved == ("AAPL",)


def test_the_next_candle_close_bounds_the_attempts(tmp_path: Path) -> None:
    """Spec 010 D44's hard bound: a retry window never overlaps the next run."""
    long_backoff = SchedulerPolicy(
        retry_window=timedelta(hours=1),
        retry_backoff=(timedelta(minutes=10), timedelta(minutes=20), timedelta(minutes=40)),
    )
    # The 18:30-19:30Z slot of 2024-07-05: the next 1h close is 20:00Z, half an hour later.
    with scheduler_harness(tmp_path, now=utc("2024-07-05T19:32"), policy=long_backoff) as harness:
        harness.engine.script(
            failing_report(Timeframe.H1, utc("2024-07-05T19:30"), "AAPL"), times=4
        )

        attempt = fire(harness, Timeframe.H1)

        assert attempt.scheduled_close == utc("2024-07-05T19:30")
        assert attempt.engine_calls == 2
        assert harness.waits == [600.0]  # 19:42 + 20 min is 20:02, past the 20:00 close
        assert attempt.unresolved == ("AAPL",)


def test_an_empty_backoff_disables_retries(tmp_path: Path) -> None:
    none_at_all = SchedulerPolicy(retry_backoff=())
    with scheduler_harness(tmp_path, now=FIRE, policy=none_at_all) as harness:
        harness.engine.script(failing_report(Timeframe.D1, CLOSE, "AAPL"), times=2)

        attempt = fire(harness, Timeframe.D1)

        assert attempt.engine_calls == 1
        assert attempt.unresolved == ("AAPL",)
        assert harness.waits == []


def test_a_paused_run_is_not_retried_and_is_still_recorded(tmp_path: Path) -> None:
    """User decision U4: a paused bot must not also look dead to the missing-run alert."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(paused_report(Timeframe.D1, CLOSE))

        attempt = fire(harness, Timeframe.D1)
        recorded = stored_last_run(harness, Timeframe.D1)

        assert attempt.status is FireStatus.RAN
        assert attempt.engine_calls == 1
        assert attempt.report is not None
        assert attempt.report.paused is True
        assert harness.waits == []
        assert recorded is not None
        assert recorded.scheduled_at == CLOSE


# --- T6: concurrency (AC10) -----------------------------------------------------------------


def test_a_second_fire_of_a_timeframe_in_flight_is_busy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """User decision U5: a run that overruns the next close makes that fire skipped."""

    async def scenario(harness: SchedulerHarness) -> tuple[RunAttempt, RunAttempt]:
        release = asyncio.Event()
        harness.engine.block_on(release)
        first = asyncio.create_task(harness.runner.run_timeframe(Timeframe.D1))
        await harness.engine.wait_until_called(1)
        second = await harness.runner.run_timeframe(Timeframe.D1)
        release.set()
        return await first, second

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            ran, busy = asyncio.run(scenario(harness))

        assert ran.status is FireStatus.RAN
        assert busy.status is FireStatus.BUSY
        assert busy.engine_calls == 0
        assert busy.report is None
        assert len(harness.engine.calls) == 1
        assert messages(caplog, logging.WARNING) == [
            "1d fire for 2024-07-05T20:00:00+00:00 skipped: busy"
        ]


def test_runs_of_different_timeframes_never_overlap(tmp_path: Path) -> None:
    """The three timeframes close at the same instant at every session close (Design 8.2)."""

    async def scenario(harness: SchedulerHarness) -> None:
        await asyncio.gather(
            harness.runner.run_timeframe(Timeframe.H1),
            harness.runner.run_timeframe(Timeframe.H4),
            harness.runner.run_timeframe(Timeframe.D1),
        )

    with scheduler_harness(tmp_path, now=FIRE, duration=timedelta(seconds=5)) as harness:
        asyncio.run(scenario(harness))

        calls = harness.engine.calls
        assert len(calls) == 3
        assert {call.timeframe for call in calls} == set(Timeframe)
        for earlier, later in pairwise(calls):
            assert earlier.finished_at <= later.started_at


def test_the_global_lock_is_free_while_a_retry_backoff_sleeps(tmp_path: Path) -> None:
    """A ``1h`` retry window must not block the ``1d`` run for minutes (AC10).

    The probe runs **inside** the backoff wait: were the global lock held there, the daily run
    could never reach the engine and the guard would fail the test instead of hanging it.
    """
    daily: list[RunAttempt] = []

    with scheduler_harness(tmp_path, now=FIRE) as harness:

        async def probing_sleep(seconds: float) -> None:
            harness.waits.append(seconds)
            harness.clock.advance(timedelta(seconds=seconds))
            daily.append(
                await asyncio.wait_for(runner.run_timeframe(Timeframe.D1), DEADLOCK_GUARD_SECONDS)
            )

        runner = TimeframeRunner(
            engine=as_signal_runner(harness.engine),
            calendar=harness.calendar,
            unit_of_work=harness.unit_of_work,
            policy=harness.policy,
            clock=harness.clock,
            sleep=probing_sleep,
        )
        harness.engine.script(failing_report(Timeframe.H1, CLOSE, "AAPL"))
        harness.engine.script(quiet_report(Timeframe.D1, CLOSE))
        harness.engine.script(quiet_report(Timeframe.H1, CLOSE))

        hourly = asyncio.run(runner.run_timeframe(Timeframe.H1))

        assert hourly.status is FireStatus.RAN
        assert [attempt.status for attempt in daily] == [FireStatus.RAN]
        assert [call.timeframe for call in harness.engine.calls] == [
            Timeframe.H1,
            Timeframe.D1,
            Timeframe.H1,
        ]


def test_the_timeframe_lock_is_released_after_a_failure(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.fail_next(RuntimeError("the engine broke"))

        failed = fire(harness, Timeframe.D1)
        again = fire(harness, Timeframe.D1)

        assert failed.status is FireStatus.FAILED
        assert again.status is FireStatus.RAN


def test_cancellation_propagates_untouched_and_releases_the_lock(tmp_path: Path) -> None:
    """Cancellation is how #16 shuts the bot down, and it is never an ``ERROR`` (D155)."""

    async def scenario(harness: SchedulerHarness) -> RunAttempt:
        release = asyncio.Event()
        harness.engine.block_on(release)
        running = asyncio.create_task(harness.runner.run_timeframe(Timeframe.D1))
        await harness.engine.wait_until_called(1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        harness.engine.block_on(release)
        release.set()
        return await harness.runner.run_timeframe(Timeframe.D1)

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = asyncio.run(scenario(harness))

        assert attempt.status is FireStatus.RAN
        assert stored_last_run(harness, Timeframe.D1) is not None


def test_stop_refuses_new_runs(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.runner.stop()

        attempt = fire(harness, Timeframe.D1)

        assert attempt.status is FireStatus.STOPPING
        assert attempt.scheduled_close is None
        assert harness.engine.calls == ()


def test_stop_makes_a_retry_window_give_up_before_its_next_sleep(tmp_path: Path) -> None:
    async def scenario(harness: SchedulerHarness) -> RunAttempt:
        release = asyncio.Event()
        harness.engine.block_on(release)
        running = asyncio.create_task(harness.runner.run_timeframe(Timeframe.H1))
        await harness.engine.wait_until_called(1)
        harness.runner.stop()
        release.set()
        return await running

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(failing_report(Timeframe.H1, CLOSE, "AAPL"), times=3)

        attempt = asyncio.run(scenario(harness))

        assert attempt.status is FireStatus.RAN
        assert attempt.engine_calls == 1
        assert attempt.unresolved == ("AAPL",)
        assert harness.waits == []


def test_drain_waits_for_the_run_in_flight(tmp_path: Path) -> None:
    async def scenario(harness: SchedulerHarness) -> tuple[bool, tuple[Timeframe, ...]]:
        release = asyncio.Event()
        harness.engine.block_on(release)
        running = asyncio.create_task(harness.runner.run_timeframe(Timeframe.D1))
        await harness.engine.wait_until_called(1)
        in_flight = harness.runner.in_flight
        release.set()
        drained = await harness.runner.drain(timedelta(seconds=DEADLOCK_GUARD_SECONDS))
        await running
        return drained, in_flight

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        drained, in_flight = asyncio.run(scenario(harness))

        assert in_flight == (Timeframe.D1,)
        assert drained is True
        assert harness.runner.in_flight == ()
        assert len(harness.engine.calls) == 1


def test_drain_returns_true_when_nothing_is_in_flight(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        assert asyncio.run(harness.runner.drain(timedelta(seconds=1))) is True
        assert harness.runner.in_flight == ()


# --- T7: errors (AC12) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        StoredRuleError(1, ()),
        RuntimeError("the database is gone"),
        Exception("something nobody planned for"),
    ],
    ids=["stored-rule", "database", "unexpected"],
)
def test_an_exception_from_the_engine_fails_the_run_and_records_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.fail_next(error)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            failed = fire(harness, Timeframe.H1)

        assert stored_last_run(harness, Timeframe.H1) is None
        assert failed.status is FireStatus.FAILED
        assert failed.scheduled_close == CLOSE
        assert failed.report is None
        assert messages(caplog, logging.ERROR) == [
            f"1h run for 2024-07-05T20:00:00+00:00 failed: {type(error).__name__}"
        ]
        assert fire(harness, Timeframe.H1).status is FireStatus.RAN  # the next fire runs


def test_a_failure_never_logs_the_exception_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A notifier exception can carry a bot token in a request URL (spec 015, D135)."""
    secret = "123456789" + ":" + "x" * 35
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.fail_next(RuntimeError(f"POST https://api.example/bot{secret}/sendMessage"))

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.D1)

        assert attempt.status is FireStatus.FAILED
        assert secret not in caplog.text
        assert "api.example" not in caplog.text
        assert messages(caplog, logging.ERROR) == [
            "1d run for 2024-07-05T20:00:00+00:00 failed: RuntimeError"
        ]


def test_a_calendar_that_cannot_answer_is_an_ordinary_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with scheduler_harness(tmp_path, now=utc("2020-06-01T12:00")) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.H1)

        assert attempt.status is FireStatus.FAILED
        assert attempt.scheduled_close is None
        assert attempt.engine_calls == 0
        assert messages(caplog, logging.ERROR) == ["1h run failed: CalendarRangeError"]


def test_a_predicate_that_raises_is_an_ordinary_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(slot: CandleSlot) -> bool:
        raise ValueError("the predicate is broken")

    with scheduler_harness(tmp_path, now=FIRE, should_run=explode) as harness:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.D1)

        assert attempt.status is FireStatus.FAILED
        assert messages(caplog, logging.ERROR) == [
            "1d run for 2024-07-05T20:00:00+00:00 failed: ValueError"
        ]


def test_a_repository_failure_fails_the_run_and_records_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The unit of work is the third thing that can fail inside a fire (Design 8.4)."""

    def broken_unit_of_work() -> AbstractContextManager[EngineRepositories]:
        raise RuntimeError("the database is gone")

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        runner = TimeframeRunner(
            engine=as_signal_runner(harness.engine),
            calendar=harness.calendar,
            unit_of_work=broken_unit_of_work,
            clock=harness.clock,
        )

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = asyncio.run(runner.run_timeframe(Timeframe.D1))

        assert attempt.status is FireStatus.FAILED
        assert attempt.engine_calls == 0
        assert harness.engine.calls == ()
        assert messages(caplog, logging.ERROR) == [
            "1d run for 2024-07-05T20:00:00+00:00 failed: RuntimeError"
        ]

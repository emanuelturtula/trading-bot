"""Adversarial cases beyond the developer's own suite (spec 016, T13).

Each case here targets a specific hazard the developer flagged for the tester to attack: the
0-based retry index and the give-up count when the *deadline*, not the exhausted backoff, ends
the loop; the deadline arithmetic reading the clock only after the state read; reusing one
``TimeframeRunner`` safely across many separate event loops; a ``FAILED`` run losing an earlier
successful report; an overrunning run leaving a visible gap in ``bot_state``; the maximum close
delay against the 30-minute gap of a session's final ``1h`` slot; one trigger's
``CalendarRangeError`` never touching a sibling trigger's own schedule; and the order of the
early exits, which keeps a ``BUSY`` fire from ever touching ``bot_state``.

Every instant is a literal or a ``ManualClock`` reading; nothing here sleeps for real or reads
the wall clock. mypy strict does not scan ``tests/unit`` (only ``src`` and ``tests/fixtures``,
per ``pyproject.toml``), so the doubles below stay untyped where that keeps them short.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.fixtures.calendars import utc
from tests.fixtures.scheduler import (
    SchedulerHarness,
    as_signal_runner,
    failing_report,
    fire,
    recording_sleep,
    scheduler_harness,
)
from trading_bot.domain.market_calendar.nyse import build_nyse_calendar
from trading_bot.domain.market_calendar.sessions import CalendarRangeError
from trading_bot.domain.timeframe import Timeframe
from trading_bot.scheduler.policy import SchedulerPolicy
from trading_bot.scheduler.runner import FireStatus, TimeframeRunner
from trading_bot.scheduler.trigger import CandleCloseTrigger, next_fire

LOGGER_NAME = "trading_bot.scheduler"

CLOSE = utc("2024-07-05T20:00")  # the real close of the 2024-07-05 session's last 1h slot
FIRE = utc("2024-07-05T20:02")  # CLOSE + the default 120 s delay


def messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == LOGGER_NAME
    ]


def stored_last_run(harness: SchedulerHarness, timeframe: Timeframe):
    with harness.unit_of_work() as repositories:
        return repositories.state.load().last_run(timeframe)


# --- The give-up count and the retry index when the deadline, not the backoff, ends it -------


def test_the_give_up_warning_counts_attempts_when_the_deadline_ends_the_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The developer's own T5 pins this literal only for a backoff exhausted by the *table*.

    Here the ten-minute (default) ``retry_window`` ends the loop instead, so the attempt count
    and the 0-based retry index it is built from come from the deadline branch of the same
    ``if`` (Design 8.1, step 7), never exercised by T5's own literal.
    """
    short = SchedulerPolicy(retry_window=timedelta(minutes=2))
    with scheduler_harness(tmp_path, now=FIRE, policy=short) as harness:
        harness.engine.script(failing_report(Timeframe.D1, CLOSE, "AAPL"), times=5)

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            attempt = fire(harness, Timeframe.D1)

        assert attempt.engine_calls == 3
        assert attempt.unresolved == ("AAPL",)
        assert [line for line in messages(caplog, logging.DEBUG) if "retry" in line] == [
            "1d retry 0 in 30s",
            "1d retry 1 in 60s",
        ]
        assert messages(caplog, logging.WARNING) == [
            "1d run for 2024-07-05T20:00:00+00:00: gave up on 1 tickers after 3 attempts (AAPL)"
        ]


# --- The deadline is read after the state read: a slow read shortens the budget --------------


def test_a_slow_state_read_shortens_the_attempts_the_next_close_deadline_allows(
    tmp_path: Path,
) -> None:
    """Decision D149: ``started = clock()`` is read in ``_deadline``, right after the state read.

    The next ``1h`` close is a fixed, absolute bound (spec 010 D44): it does not move when the
    state read is slow. A slow read therefore eats into the wall-clock room *before* that fixed
    bound instead of pushing the bound itself back, so fewer retries fit. Both runs below share
    the same policy, the same scripted failures and the same starting clock; only the slow read
    differs, and it alone turns T5's own pinned ``engine_calls == 2`` for this exact scenario
    (``test_the_next_candle_close_bounds_the_attempts``) into 1.
    """
    long_backoff = SchedulerPolicy(
        retry_window=timedelta(hours=1),
        retry_backoff=(timedelta(minutes=10), timedelta(minutes=20), timedelta(minutes=40)),
    )
    hourly_close = utc("2024-07-05T19:30")  # the next 1h close is 20:00, 30 min later
    hourly_fire = utc("2024-07-05T19:32")
    slow_read_delay = timedelta(minutes=25)

    with scheduler_harness(tmp_path / "fast", now=hourly_fire, policy=long_backoff) as fast:
        fast.engine.script(failing_report(Timeframe.H1, hourly_close, "AAPL"), times=4)
        baseline = fire(fast, Timeframe.H1)

    # A database of its own: the fast run above already recorded H1's last run at hourly_close
    # (record_run happens whatever the outcome, D144), which would make a shared database
    # answer ALREADY_RUN here instead of exercising the retry loop a second time.
    with scheduler_harness(tmp_path / "slow", now=hourly_fire, policy=long_backoff) as slow:
        slow.engine.script(failing_report(Timeframe.H1, hourly_close, "AAPL"), times=4)
        state_reads = 0

        @contextmanager
        def slow_unit_of_work():
            nonlocal state_reads
            state_reads += 1
            with slow.unit_of_work() as repositories:
                if state_reads == 1:  # only read_last_run, never the later record_run
                    slow.clock.advance(slow_read_delay)
                yield repositories

        sleep, _ = recording_sleep(slow.clock)
        runner = TimeframeRunner(
            engine=as_signal_runner(slow.engine),
            calendar=slow.calendar,
            unit_of_work=slow_unit_of_work,
            policy=long_backoff,
            clock=slow.clock,
            sleep=sleep,
        )
        slowed = asyncio.run(runner.run_timeframe(Timeframe.H1))

    assert baseline.engine_calls == 2  # T5's own pinned outcome for this exact scenario
    assert baseline.unresolved == ("AAPL",)
    assert slowed.engine_calls == 1  # the 25-minute slow read alone ate the retry budget
    assert slowed.unresolved == ("AAPL",)


# --- Reusing one runner across many separate event loops --------------------------------------


def test_a_runner_is_reusable_across_many_separate_event_loops(tmp_path: Path) -> None:
    """The locks and the idle event are built once in ``__init__``, outside any loop.

    ``asyncio.Lock``/``asyncio.Event`` bind to a running loop only when a wait actually
    contends (Python 3.10+): every fire below always finds its own lock free and the idle event
    already set, so nothing here should ever bind to one loop and then choke on the next. A
    handful of alternating fires and drains, each its own ``asyncio.run``, is the regression
    guard: the pattern every other test in this feature already relies on.
    """
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        for _ in range(5):
            assert asyncio.run(harness.runner.drain(timedelta(seconds=1))) is True

        first = fire(harness, Timeframe.D1)
        assert asyncio.run(harness.runner.drain(timedelta(seconds=1))) is True
        second = fire(harness, Timeframe.H1)
        assert asyncio.run(harness.runner.drain(timedelta(seconds=1))) is True
        third = fire(harness, Timeframe.D1)  # already run: exercises the same lock again

        assert first.status is FireStatus.RAN
        assert second.status is FireStatus.RAN
        assert third.status is FireStatus.ALREADY_RUN
        assert harness.runner.in_flight == ()


# --- A FAILED run after an earlier successful engine call loses that report ------------------


def test_a_failure_after_a_successful_attempt_still_reports_none(tmp_path: Path) -> None:
    """Open interpretation #7: is dropping the last successful report on ``FAILED`` correct?

    The first attempt succeeds with a report naming a retryable ticker; the second attempt, the
    retry, raises. The whole loop of ``_run_once`` is one ``try`` block, so the
    ``except Exception`` branch reaches ``_failed``, which always builds its ``RunAttempt``
    through ``_attempt`` with ``report=None``: the report the first, successful call already
    produced is discarded. This test pins today's behaviour; it does not assert that the
    behaviour is the right one (see the tester's report on interpretation #7).
    """

    class SucceedThenBreak:
        """The first call returns a retryable report; the second raises (FIFO by call index).

        ``FakeSignalRunner`` cannot express this exact order: it always drains its scripted
        failures before its scripted reports, whichever was scripted first, so it always fails
        the *first* call in a mixed script, never the second. A minimal double sidesteps that.
        """

        def __init__(self, first_report: object, then_raise: Exception) -> None:
            self._calls = 0
            self._first_report = first_report
            self._then_raise = then_raise

        async def run(self, timeframe: Timeframe, now, *, tickers=None):
            self._calls += 1
            if self._calls == 1:
                return self._first_report
            raise self._then_raise

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        sleep, _ = recording_sleep(harness.clock)
        runner = TimeframeRunner(
            engine=SucceedThenBreak(
                failing_report(Timeframe.H1, CLOSE, "AAPL"),
                RuntimeError("the retry attempt broke"),
            ),
            calendar=harness.calendar,
            unit_of_work=harness.unit_of_work,
            policy=harness.policy,
            clock=harness.clock,
            sleep=sleep,
        )

        attempt = asyncio.run(runner.run_timeframe(Timeframe.H1))

        assert attempt.status is FireStatus.FAILED
        assert attempt.engine_calls == 1  # only the successful call is counted; see runner.py
        assert attempt.report is None  # the first call's RunReport is not surfaced
        assert attempt.unresolved == ()
        assert stored_last_run(harness, Timeframe.H1) is None  # nothing recorded either


# --- An overrunning run leaves a visible gap in bot_state, never a queued replay --------------


def test_an_overrunning_run_leaves_a_visible_gap_never_a_queued_replay(tmp_path: Path) -> None:
    """User decision U5: the fire for the next close is skipped, not queued, while one runs long.

    Decision D128 (no backfill) means the gap between the two recorded closes below is never
    filled afterwards: F7 reads exactly this kind of hole in ``bot_state.last_run`` as a missed
    run, which is the point of recording it at all.
    """
    friday_last_hour_close = utc("2024-07-05T20:00")
    friday_last_hour_fire = utc("2024-07-05T20:02")
    next_monday_first_hour_close = utc("2024-07-08T14:30")
    next_monday_first_hour_fire = utc("2024-07-08T14:32")

    async def scenario(harness: SchedulerHarness):
        release = asyncio.Event()
        harness.engine.block_on(release)
        overrunning = asyncio.create_task(harness.runner.run_timeframe(Timeframe.H1))
        await harness.engine.wait_until_called(1)

        # By the time the next Monday session opens, the Friday run is still in flight: the
        # fire for that later close is rejected outright, never queued behind it.
        harness.clock.set(next_monday_first_hour_fire)
        overrun_fire = await harness.runner.run_timeframe(Timeframe.H1)

        release.set()
        completed = await overrunning
        return completed, overrun_fire

    with scheduler_harness(tmp_path, now=friday_last_hour_fire) as harness:
        completed, overrun_fire = asyncio.run(scenario(harness))

        assert completed.status is FireStatus.RAN
        assert completed.scheduled_close == friday_last_hour_close
        assert overrun_fire.status is FireStatus.BUSY
        assert overrun_fire.scheduled_close == next_monday_first_hour_close
        assert stored_last_run(harness, Timeframe.H1).scheduled_at == friday_last_hour_close

        # The clock is already at Monday's fire time: a fresh fire now succeeds, and bot_state
        # jumps directly from Friday's last hour to Monday's first hour with nothing in between.
        recovered = fire(harness, Timeframe.H1)

        assert recovered.status is FireStatus.RAN
        assert recovered.scheduled_close == next_monday_first_hour_close
        assert stored_last_run(harness, Timeframe.H1).scheduled_at == next_monday_first_hour_close


# --- The maximum close delay against the 30-minute gap, at the runner level ------------------


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(seconds=900)], ids=["0s", "900s"])
def test_the_runner_fires_both_ends_of_the_thirty_minute_gap_correctly(
    tmp_path: Path, delay: timedelta
) -> None:
    """T2 pins this gap for ``next_fire``; here it is the *runner* that must not confuse the two.

    The last two ``1h`` closes of a regular session are 19:30 and 20:00 (Design 6): the second
    slot is truncated to thirty minutes. Neither the minimum nor the maximum allowed delay may
    make the runner misidentify which of the two candles a fire is about.
    """
    second_to_last_close = utc("2024-07-05T19:30")
    last_close = utc("2024-07-05T20:00")
    policy = SchedulerPolicy(close_delay=delay)

    with scheduler_harness(tmp_path, now=second_to_last_close + delay, policy=policy) as harness:
        first = fire(harness, Timeframe.H1)
        harness.clock.set(last_close + delay)
        second = fire(harness, Timeframe.H1)

        assert first.status is FireStatus.RAN
        assert first.scheduled_close == second_to_last_close
        assert second.status is FireStatus.RAN
        assert second.scheduled_close == last_close
        assert [call.now for call in harness.engine.calls] == [second_to_last_close, last_close]


# --- One trigger's CalendarRangeError never touches a sibling trigger's own schedule ----------


def test_one_timeframes_trigger_failure_never_reaches_a_sibling_timeframe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A calendar built for a short, fixed span, as a stand-in for "coverage ends tomorrow".

    ``1d``'s next close needs a session beyond the calendar's last covered day and fails; a
    separate ``1h`` trigger over the very same calendar object, asked about an earlier instant
    still well inside coverage, is completely unaffected: no state leaks between two triggers
    that only share a read-only calendar (AC5, decision D141).
    """
    short_calendar = build_nyse_calendar(date(2024, 7, 1), date(2024, 7, 2))  # Mon-Tue only
    daily = CandleCloseTrigger(
        calendar=short_calendar, timeframe=Timeframe.D1, delay=timedelta(seconds=120)
    )
    hourly = CandleCloseTrigger(
        calendar=short_calendar, timeframe=Timeframe.H1, delay=timedelta(seconds=120)
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        # 20:03Z is past Tuesday's own fire time (its 20:00Z close plus the 120 s delay, 20:02Z),
        # so the daily trigger must look for Wednesday's close, which the calendar does not have.
        daily_result = daily.get_next_fire_time(None, utc("2024-07-02T20:03"))
        hourly_result = hourly.get_next_fire_time(None, utc("2024-07-02T14:00"))

    assert daily_result is None
    assert hourly_result == utc("2024-07-02T14:32")
    errors = messages(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].startswith("1d trigger: no next fire after")


# --- A documented, accepted edge: a delay near coverage_start can still raise -----------------


def test_a_delay_near_coverage_start_can_raise_even_though_a_later_close_exists() -> None:
    """Design 7.1's own words: this is "a configuration problem", accepted because #16 builds a
    hundred-year calendar (2000-2099): no fire is ever computed within 15 minutes of its start.

    ``next_fire`` anchors its search at ``after - delay - 1us`` (Design 7.1, step 2) before
    checking coverage: with the maximum delay and an ``after`` a few minutes past
    ``coverage_start``, that anchor falls *before* ``coverage_start`` and ``CalendarRangeError``
    propagates, even though ``after`` itself is covered and a correct answer (the same close a
    delay of ``0`` finds) exists. This test pins the documented behaviour so a change to it is
    never silent; it is not a claim that the behaviour is desirable in isolation.
    """
    calendar = build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))
    after = calendar.coverage_start + timedelta(minutes=5)

    slot, close_fire = next_fire(calendar, Timeframe.H1, after=after, delay=timedelta(0))
    assert close_fire == slot.close_time  # the correct, delay-independent answer

    with pytest.raises(CalendarRangeError):
        next_fire(calendar, Timeframe.H1, after=after, delay=timedelta(seconds=900))


# --- Order of the early exits: a BUSY fire never touches bot_state ----------------------------


def test_a_busy_fire_never_opens_a_unit_of_work(tmp_path: Path) -> None:
    """Design 8.1: the lock (step 4) is checked before the misfire and "already run" checks
    (steps 5-6), which are the only two that read ``bot_state``. A second, busy fire must
    therefore leave the unit-of-work call count exactly where the first, in-flight fire left it.
    """

    async def scenario(harness: SchedulerHarness, runner: TimeframeRunner, calls: list[int]):
        release = asyncio.Event()
        harness.engine.block_on(release)
        running = asyncio.create_task(runner.run_timeframe(Timeframe.D1))
        await harness.engine.wait_until_called(1)
        calls_while_in_flight = calls[0]  # the "already run" read already happened once

        busy = await runner.run_timeframe(Timeframe.D1)

        release.set()
        completed = await running
        return completed, busy, calls_while_in_flight

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        calls = [0]

        @contextmanager
        def counting_unit_of_work():
            calls[0] += 1
            with harness.unit_of_work() as repositories:
                yield repositories

        runner = TimeframeRunner(
            engine=as_signal_runner(harness.engine),
            calendar=harness.calendar,
            unit_of_work=counting_unit_of_work,
            policy=harness.policy,
            clock=harness.clock,
        )

        completed, busy, calls_while_in_flight = asyncio.run(scenario(harness, runner, calls))

        assert completed.status is FireStatus.RAN
        assert busy.status is FireStatus.BUSY
        # The busy fire adds no unit-of-work call at all: the lock rejects it before any read.
        assert calls[0] == calls_while_in_flight + 1  # +1 is record_run, after the release

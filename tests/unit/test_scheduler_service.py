"""The job set, the startup catch-up and the shutdown (T8, T9; AC14, AC15, AC16, AC17).

Most of the file drives a ``RecordingScheduler``: a real ``BaseScheduler`` subclass that records
what the service asks of it and runs nothing, so the job specification is asserted field by
field without an event loop. Two tests use a real ``AsyncIOScheduler``, because "it really
fires" and "APScheduler cancels a coroutine job in flight at shutdown" cannot be learned from a
double.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.date import DateTrigger

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.scheduler import (
    SchedulerHarness,
    failing_report,
    fire,
    scheduler_harness,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.scheduler.policy import DEFAULT_POLICY, SchedulerPolicy
from trading_bot.scheduler.runner import FireStatus, RunAttempt
from trading_bot.scheduler.service import JOB_ID_PREFIX, SchedulerService, job_id
from trading_bot.scheduler.trigger import CandleCloseTrigger

LOGGER_NAME = "trading_bot.scheduler"

CLOSE = utc("2024-07-05T20:00")
FIRE = utc("2024-07-05T20:02")
PREVIOUS_CLOSE = utc("2024-07-03T17:00")
STALE_INSTANT = utc("2024-07-05T20:32:30")

# A real-time budget for the two tests that drive a real scheduler. It is never compared with
# an elapsed time: it only turns a hang into a failure.
REAL_TIME_GUARD_SECONDS = 5.0


@dataclass
class AddedJob:
    """What the service asked the scheduler to add."""

    func: object
    trigger: object
    args: tuple[object, ...]
    kwargs: dict[str, object]
    id: str
    max_instances: object
    coalesce: object
    misfire_grace_time: object
    replace_existing: object
    next_run_time: datetime | None = None


@dataclass
class RecordingScheduler(BaseScheduler):  # type: ignore[misc]
    """A ``BaseScheduler`` that records the job set and runs nothing."""

    added: list[AddedJob] = field(default_factory=list)
    started: int = 0
    paused: int = 0
    stopped: int = 0

    def __post_init__(self) -> None:
        super().__init__()

    def wakeup(self) -> None:
        """Required by ``BaseScheduler``; there is no loop to wake."""

    def start(self, paused: bool = False) -> None:
        self.started += 1

    def pause(self) -> None:
        self.paused += 1

    def shutdown(self, wait: bool = True) -> None:
        self.stopped += 1

    def add_job(self, func: object, trigger: object = None, **options: Any) -> AddedJob:
        job = AddedJob(
            func=func,
            trigger=trigger,
            args=tuple(options.get("args") or ()),
            kwargs=dict(options.get("kwargs") or {}),
            id=str(options.get("id")),
            max_instances=options.get("max_instances"),
            coalesce=options.get("coalesce"),
            misfire_grace_time=options.get("misfire_grace_time"),
            replace_existing=options.get("replace_existing"),
        )
        self.added.append(job)
        return job

    def get_job(self, job_id: str, jobstore: object = None) -> AddedJob | None:
        return next((job for job in self.added if job.id == job_id), None)


def build_service(
    harness: SchedulerHarness,
    scheduler: BaseScheduler,
    *,
    policy: SchedulerPolicy = DEFAULT_POLICY,
    timeframes: tuple[Timeframe, ...] = tuple(Timeframe),
) -> SchedulerService:
    return SchedulerService(
        runner=harness.runner,
        calendar=harness.calendar,
        policy=policy,
        timeframes=timeframes,
        scheduler=scheduler,
    )


def messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The scheduler's records of one level; another logger's records are never asserted on."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == LOGGER_NAME
    ]


# --- T8: the job set (AC14) -----------------------------------------------------------------


def test_the_job_id_is_stable_and_names_the_timeframe() -> None:
    assert JOB_ID_PREFIX == "candle-close"
    assert [job_id(timeframe) for timeframe in Timeframe] == [
        "candle-close.1h",
        "candle-close.4h",
        "candle-close.1d",
    ]
    with pytest.raises(TypeError, match="timeframe"):
        job_id("1h")  # type: ignore[arg-type]


def test_start_adds_one_recurring_job_and_one_catch_up_job_per_timeframe(
    tmp_path: Path,
) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)

        service.start()

        assert service.running is True
        assert scheduler.started == 1
        assert [job.id for job in scheduler.added] == [
            "candle-close.1h",
            "candle-close.4h",
            "candle-close.1d",
            "candle-close.1h.catchup",
            "candle-close.4h.catchup",
            "candle-close.1d.catchup",
        ]


@pytest.mark.parametrize("position", [0, 1, 2])
def test_each_recurring_job_carries_the_pinned_options(tmp_path: Path, position: int) -> None:
    timeframe = list(Timeframe)[position]
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)

        service.start()

        job = scheduler.added[position]
        assert job.func == harness.runner.run_timeframe
        assert job.args == (timeframe,)
        assert job.kwargs == {}
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 900
        assert job.replace_existing is False
        assert isinstance(job.trigger, CandleCloseTrigger)
        assert str(job.trigger) == f"candle close {timeframe.value} + 120s"


@pytest.mark.parametrize("position", [3, 4, 5])
def test_each_catch_up_job_is_an_immediate_date_job(tmp_path: Path, position: int) -> None:
    timeframe = list(Timeframe)[position - 3]
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)

        service.start()

        job = scheduler.added[position]
        assert job.id == f"{job_id(timeframe)}.catchup"
        assert job.args == (timeframe,)
        assert job.kwargs == {"catch_up": True}
        assert job.max_instances == 1
        assert job.trigger == "date"


def test_the_misfire_grace_time_follows_the_policy(tmp_path: Path) -> None:
    policy = SchedulerPolicy(misfire_grace=timedelta(minutes=1))
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()

        build_service(harness, scheduler, policy=policy).start()

        assert {job.misfire_grace_time for job in scheduler.added} == {60}


def test_the_close_delay_of_the_policy_reaches_the_trigger(tmp_path: Path) -> None:
    policy = SchedulerPolicy(close_delay=timedelta(0))
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()

        build_service(harness, scheduler, policy=policy).start()

        assert str(scheduler.added[0].trigger) == "candle close 1h + 0s"


def test_a_second_start_raises_and_adds_nothing(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)
        service.start()

        with pytest.raises(RuntimeError, match="already running"):
            service.start()

        assert len(scheduler.added) == 6
        assert scheduler.started == 1


def test_an_empty_timeframe_set_adds_no_job(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()

        build_service(harness, scheduler, timeframes=()).start()

        assert scheduler.added == []


def test_a_single_timeframe_adds_only_its_two_jobs(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()

        build_service(harness, scheduler, timeframes=(Timeframe.D1,)).start()

        assert [job.id for job in scheduler.added] == [
            "candle-close.1d",
            "candle-close.1d.catchup",
        ]


def test_next_fire_times_reports_one_instant_per_timeframe(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)
        service.start()
        scheduler.added[0].next_run_time = FIRE
        scheduler.added[2].next_run_time = FIRE.astimezone(nyse_test_calendar().timezone)

        assert service.next_fire_times() == (
            (Timeframe.H1, FIRE),
            (Timeframe.H4, None),
            (Timeframe.D1, FIRE),
        )


def test_next_fire_times_is_none_for_a_job_that_is_no_longer_there(tmp_path: Path) -> None:
    """A job the trigger gave up on (AC5) leaves the timeframe without a next fire."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler, timeframes=(Timeframe.D1,))
        service.start()
        scheduler.added.clear()

        assert service.next_fire_times() == ((Timeframe.D1, None),)


def test_next_fire_times_before_start_is_all_none(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        service = build_service(harness, RecordingScheduler())

        assert service.next_fire_times() == (
            (Timeframe.H1, None),
            (Timeframe.H4, None),
            (Timeframe.D1, None),
        )
        assert service.running is False


def test_the_service_rejects_arguments_of_the_wrong_type(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with pytest.raises(TypeError, match="calendar"):
            SchedulerService(runner=harness.runner, calendar="NYSE")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="policy"):
            SchedulerService(
                runner=harness.runner,
                calendar=harness.calendar,
                policy=900,  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="timeframes"):
            SchedulerService(
                runner=harness.runner,
                calendar=harness.calendar,
                timeframes=("1h",),  # type: ignore[arg-type]
            )


# --- T8: the startup catch-up (AC15) --------------------------------------------------------


def test_a_fresh_close_inside_the_window_is_caught_up(tmp_path: Path) -> None:
    """No recorded run and a candle closed two minutes ago: the catch-up runs it."""
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        attempt = fire(harness, Timeframe.D1, catch_up=True)

        assert attempt.status is FireStatus.RAN
        assert [call.now for call in harness.engine.calls] == [CLOSE]


def test_a_close_already_run_is_not_caught_up(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        with harness.unit_of_work() as repositories:
            repositories.state.record_run(Timeframe.D1, CLOSE)

        attempt = fire(harness, Timeframe.D1, catch_up=True)

        assert attempt.status is FireStatus.ALREADY_RUN
        assert harness.engine.calls == ()


def test_a_close_outside_the_window_is_not_caught_up(tmp_path: Path) -> None:
    """An older recorded run, but the candle closed too long ago: that candle is lost."""
    with scheduler_harness(tmp_path, now=STALE_INSTANT) as harness:
        with harness.unit_of_work() as repositories:
            repositories.state.record_run(Timeframe.D1, PREVIOUS_CLOSE)

        attempt = fire(harness, Timeframe.D1, catch_up=True)

        assert attempt.status is FireStatus.STALE
        assert harness.engine.calls == ()


def test_a_restart_between_two_closes_of_a_session_runs_nothing(tmp_path: Path) -> None:
    """The last candle already ran; the next scheduled fire is the next close."""
    with scheduler_harness(tmp_path, now=utc("2024-07-05T14:40")) as harness:
        with harness.unit_of_work() as repositories:
            repositories.state.record_run(Timeframe.H1, utc("2024-07-05T14:30"))

        attempt = fire(harness, Timeframe.H1, catch_up=True)

        assert attempt.status is FireStatus.ALREADY_RUN
        assert attempt.scheduled_close == utc("2024-07-05T14:30")
        assert harness.engine.calls == ()

        scheduler = RecordingScheduler()
        build_service(harness, scheduler).start()
        trigger = scheduler.added[0].trigger
        assert isinstance(trigger, CandleCloseTrigger)
        assert trigger.get_next_fire_time(None, harness.clock()) == utc("2024-07-05T15:32")


def test_a_catch_up_that_coincides_with_a_scheduled_fire_runs_once(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        catch_up = fire(harness, Timeframe.D1, catch_up=True)
        scheduled = fire(harness, Timeframe.D1)

        assert catch_up.status is FireStatus.RAN
        assert scheduled.status is FireStatus.ALREADY_RUN
        assert len(harness.engine.calls) == 1


# --- T8: the shutdown (AC16) ----------------------------------------------------------------


def test_aclose_before_start_is_a_no_op(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)

        asyncio.run(service.aclose())

        assert scheduler.stopped == 0
        assert scheduler.paused == 0
        assert service.running is False


def test_aclose_stops_the_scheduler_and_the_runner(tmp_path: Path) -> None:
    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)
        service.start()

        asyncio.run(service.aclose())

        assert scheduler.paused == 1
        assert scheduler.stopped == 1
        assert service.running is False
        assert fire(harness, Timeframe.D1).status is FireStatus.STOPPING


def test_aclose_is_idempotent(tmp_path: Path) -> None:
    async def close_twice(service: SchedulerService) -> None:
        await service.aclose()
        await service.aclose()

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)
        service.start()

        asyncio.run(close_twice(service))

        assert scheduler.stopped == 1
        assert scheduler.paused == 1


def test_aclose_warns_once_per_timeframe_still_in_flight(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An engine that never returns: the drain gives up and says so (AC16)."""
    policy = SchedulerPolicy(shutdown_timeout=timedelta(seconds=1))

    async def scenario(harness: SchedulerHarness, service: SchedulerService) -> RunAttempt:
        never = asyncio.Event()
        harness.engine.block_on(never)
        running = asyncio.create_task(harness.runner.run_timeframe(Timeframe.H1))
        await harness.engine.wait_until_called(1)
        await service.aclose()
        never.set()
        return await running

    with scheduler_harness(tmp_path, now=FIRE, policy=policy) as harness:
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler, policy=policy)
        service.start()

        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            asyncio.run(scenario(harness, service))

        assert messages(caplog, logging.WARNING) == [
            "scheduler shutdown: a 1h run was still in flight after 1s"
        ]


def test_aclose_makes_a_retry_window_in_flight_give_up(tmp_path: Path) -> None:
    async def scenario(harness: SchedulerHarness, service: SchedulerService) -> RunAttempt:
        release = asyncio.Event()
        harness.engine.block_on(release)
        running = asyncio.create_task(harness.runner.run_timeframe(Timeframe.H1))
        await harness.engine.wait_until_called(1)
        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)  # let aclose() stop the runner and reach its drain
        release.set()
        attempt = await running
        await closing
        return attempt

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        harness.engine.script(failing_report(Timeframe.H1, CLOSE, "AAPL"), times=3)
        scheduler = RecordingScheduler()
        service = build_service(harness, scheduler)
        service.start()

        attempt = asyncio.run(scenario(harness, service))

        assert attempt.engine_calls == 1
        assert attempt.unresolved == ("AAPL",)
        assert harness.waits == []


# --- T9: a real scheduler really fires (AC17) -----------------------------------------------


class ImmediateTrigger(BaseTrigger):
    """A stub trigger that fires as soon as APScheduler can, again and again."""

    __slots__ = ()

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime
    ) -> datetime | None:
        return now + timedelta(milliseconds=10)


def test_a_real_scheduler_fires_the_runner_and_aclose_stops_it(tmp_path: Path) -> None:
    """The only test that touches real time, and it waits on an event, never on a duration."""
    calls: list[Timeframe] = []
    fired = asyncio.Event()

    class CountingRunner:
        async def run_timeframe(self, timeframe: Timeframe, *, catch_up: bool = False) -> None:
            calls.append(timeframe)
            fired.set()

        def stop(self) -> None: ...

        async def drain(self, timeout: timedelta) -> bool:
            return True

        @property
        def in_flight(self) -> tuple[Timeframe, ...]:
            return ()

    async def scenario() -> tuple[int, int, BaseScheduler]:
        scheduler = AsyncIOScheduler(timezone=UTC)
        service = SchedulerService(
            runner=CountingRunner(),  # type: ignore[arg-type]
            calendar=nyse_test_calendar(),
            timeframes=(Timeframe.D1,),
            scheduler=scheduler,
        )
        service.start()
        scheduler.reschedule_job(job_id(Timeframe.D1), trigger=ImmediateTrigger())
        await asyncio.wait_for(fired.wait(), REAL_TIME_GUARD_SECONDS)
        await service.aclose()
        after_close = len(calls)
        await asyncio.sleep(0.05)  # a timer left behind would fire in this window
        return after_close, len(calls), scheduler

    after_close, final, scheduler = asyncio.run(scenario())

    assert calls != []
    assert final == after_close
    assert scheduler.state == 0  # STATE_STOPPED
    assert isinstance(scheduler._jobstores["default"], MemoryJobStore)
    assert scheduler.timezone == UTC


def test_the_service_builds_its_own_asyncio_scheduler_when_none_is_injected(
    tmp_path: Path,
) -> None:
    """Production wiring (#16) injects nothing: the service builds the UTC scheduler itself."""

    async def scenario(harness: SchedulerHarness) -> None:
        service = SchedulerService(runner=harness.runner, calendar=harness.calendar, timeframes=())
        service.start()
        assert service.running is True
        await service.aclose()
        assert service.running is False

    with scheduler_harness(tmp_path, now=FIRE) as harness:
        asyncio.run(scenario(harness))

        assert harness.engine.calls == ()


def test_the_real_scheduler_cancels_a_coroutine_job_in_flight_at_shutdown(
    tmp_path: Path,
) -> None:
    """Pinned behaviour of APScheduler 3.11: ``shutdown`` cancels the task, it never awaits it.

    That is why ``aclose`` pauses the scheduler and drains **before** shutting it down: the
    contract of AC16 is that no path of this package can open a unit of work once ``aclose``
    has returned, so #16 may dispose the database right after.
    """
    started = asyncio.Event()
    outcome: list[str] = []

    async def never_returns() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    async def scenario() -> None:
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(never_returns, trigger=DateTrigger(), id="probe")
        scheduler.start()
        await asyncio.wait_for(started.wait(), REAL_TIME_GUARD_SECONDS)
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)  # let the cancellation unwind

    asyncio.run(scenario())

    assert outcome == ["cancelled"]

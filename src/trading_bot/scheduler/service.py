"""The ``AsyncIOScheduler`` and the job set (spec 016, Design 10; decisions D145, D147, D148).

The second and last module that knows APScheduler exists. It owns one scheduler, one recurring
job per timeframe with a stable id, and one immediate catch-up job per timeframe, and it never
computes a fire time itself: that is ``trigger.py``'s pure arithmetic.

**One job per ``Timeframe`` member, always** (user decision U2), not only for timeframes that
currently have enabled tickers: the engine already turns an empty configuration into one short
read, while a job set derived from the configuration would need an event the management CLI —
in another process — cannot send, or polling.

**The job store is the default in-memory one** (decision D148): a trigger holding a 100-year
calendar is never pickled, and no job built by an older release is ever resurrected. What the
bot needs across a restart is in ``bot_state``, which is what the catch-up job reads.

**Shutdown pauses first.** APScheduler 3.11's ``AsyncIOExecutor.shutdown`` **cancels** the
coroutine job in flight instead of awaiting it (its own comment says ``wait=True`` cannot be
honoured), and ``run_coroutine_job`` would then log that cancellation with ``logger.exception``.
So ``aclose`` pauses the scheduler — which stops new fires while leaving the run in flight
alone — drains the runner, and only then shuts the scheduler down. After ``aclose`` returns, no
path of this package can open a unit of work, unless the drain timed out, which is exactly what
the ``WARNING`` reports.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.scheduler.policy import DEFAULT_POLICY, SchedulerPolicy
from trading_bot.scheduler.runner import TimeframeRunner
from trading_bot.scheduler.trigger import CandleCloseTrigger, SlotPredicate, run_every_slot

__all__ = ["JOB_ID_PREFIX", "SchedulerService", "job_id"]

_LOGGER_NAME: Final = "trading_bot.scheduler"

JOB_ID_PREFIX: Final = "candle-close"
_CATCH_UP_SUFFIX: Final = "catchup"


def job_id(timeframe: Timeframe) -> str:
    """``candle-close.1h``: the stable job id of one timeframe."""
    if not isinstance(timeframe, Timeframe):
        raise TypeError(f"timeframe must be a Timeframe, got {type(timeframe).__name__}")
    return f"{JOB_ID_PREFIX}.{timeframe.value}"


class SchedulerService:
    """Owns the ``AsyncIOScheduler`` and one candle-close job per timeframe (issue #15)."""

    def __init__(
        self,
        *,
        runner: TimeframeRunner,
        calendar: MarketCalendar,
        policy: SchedulerPolicy = DEFAULT_POLICY,
        should_run: SlotPredicate = run_every_slot,
        timeframes: Sequence[Timeframe] = tuple(Timeframe),
        scheduler: BaseScheduler | None = None,  # tests inject; production builds one
    ) -> None:
        if not isinstance(calendar, MarketCalendar):
            raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
        if not isinstance(policy, SchedulerPolicy):
            raise TypeError(f"policy must be a SchedulerPolicy, got {type(policy).__name__}")
        members = tuple(timeframes)
        if not all(isinstance(timeframe, Timeframe) for timeframe in members):
            raise TypeError("timeframes must contain only Timeframe members")
        self._runner = runner
        self._calendar = calendar
        self._policy = policy
        self._should_run = should_run
        self._timeframes = members
        self._scheduler = scheduler
        self._running = False
        self._logger = logging.getLogger(_LOGGER_NAME)

    @property
    def running(self) -> bool:
        """Whether ``start`` has been called and ``aclose`` has not."""
        return self._running

    def start(self) -> None:
        """Add the jobs and start firing. Must be called from inside the running event loop."""
        if self._running:
            raise RuntimeError("the scheduler service is already running")
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=UTC)
        grace = int(self._policy.misfire_grace.total_seconds())
        for timeframe in self._timeframes:
            self._scheduler.add_job(
                self._runner.run_timeframe,
                trigger=CandleCloseTrigger(
                    calendar=self._calendar,
                    timeframe=timeframe,
                    delay=self._policy.close_delay,
                    should_run=self._should_run,
                ),
                args=[timeframe],
                id=job_id(timeframe),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=grace,
                replace_existing=False,
            )
        for timeframe in self._timeframes:
            # ``trigger="date"`` with no ``run_date`` is "now" in the scheduler's own time
            # zone: this module reads no clock of its own (decision D147).
            self._scheduler.add_job(
                self._runner.run_timeframe,
                trigger="date",
                args=[timeframe],
                kwargs={"catch_up": True},
                id=f"{job_id(timeframe)}.{_CATCH_UP_SUFFIX}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=grace,
                replace_existing=False,
            )
        self._scheduler.start()
        self._running = True

    async def aclose(self) -> None:
        """Stop firing, let a retry window give up and wait for the run in flight.

        Idempotent, and a no-op before ``start``. The order matters: pausing stops new fires
        without touching the run in flight, the drain is what actually waits for it, and the
        shutdown comes last because it cancels whatever is still running.
        """
        if not self._running or self._scheduler is None:
            return
        self._running = False
        self._runner.stop()
        self._scheduler.pause()
        drained = await self._runner.drain(self._policy.shutdown_timeout)
        if not drained:
            seconds = int(self._policy.shutdown_timeout.total_seconds())
            for timeframe in self._runner.in_flight:
                self._logger.warning(
                    "scheduler shutdown: a %s run was still in flight after %ds",
                    timeframe.value,
                    seconds,
                )
        self._scheduler.shutdown(wait=False)

    def next_fire_times(self) -> tuple[tuple[Timeframe, datetime | None], ...]:
        """One aware UTC instant per timeframe, for ``/status`` (#21) and the tests."""
        return tuple((timeframe, self._next_fire_time(timeframe)) for timeframe in self._timeframes)

    def _next_fire_time(self, timeframe: Timeframe) -> datetime | None:
        if self._scheduler is None or not self._running:
            return None
        job = self._scheduler.get_job(job_id(timeframe))
        if job is None:
            return None
        # A job that has not been scheduled yet has no ``next_run_time`` attribute at all, and
        # a job whose trigger gave up has it set to ``None``.
        instant = getattr(job, "next_run_time", None)
        if not isinstance(instant, datetime):
            return None
        return to_utc(instant)

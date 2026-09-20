"""One fire of one timeframe (spec 016, Design 8; decisions D143, D144, D146, D149, D155).

``run_timeframe`` is the whole job: derive the candle that has just closed, decide whether this
fire should do anything at all, run the engine, retry the tickers whose candle the provider has
not published yet, and record the run. It is the only coroutine APScheduler ever calls.

Three properties are worth stating on their own.

**The engine's ``now`` is a real close, never the firing instant.** The job carries no fire
time: at each fire the candle is derived from the injected clock as
``calendar.closed_candles(timeframe, clock(), 1)[-1]`` (decision D143), so a coalesced burst, a
fire delayed by a busy loop and the startup catch-up all evaluate the **last closed candle**,
which is decision D128. A retry reuses that same ``now`` with only the retryable symbols (spec
010, D44).

**Exactly once per close.** ``bot_state`` holds the last close each timeframe ran for, so a
catch-up that coincides with a scheduled fire, a clock stepped backwards by NTP and two fires
separated by a restart all collapse to one run (decision D144). The engine's unique constraint
remains the arbiter of CLAUDE.md rule 5 underneath; this only makes a duplicate silent and free.

**No ``Exception`` escapes into APScheduler.** That is a secret-handling requirement, not only a
robustness one: APScheduler's executor logs an escaped job exception with ``logger.exception``,
and ``RedactingFilter`` rewrites ``record.msg`` only (issue #50), so **a traceback is not
redacted**. A notifier exception can carry a bot token inside a request URL and a provider
exception Yahoo's session crumb. Every ``Exception`` is therefore caught and logged with the
exception **class** name alone, never its text and never with ``exc_info`` (decisions D154,
D155). ``BaseException`` — ``asyncio.CancelledError`` included — is the only thing that leaves
the job, and cancellation carries no message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import RunReport
from trading_bot.engine.unit_of_work import UnitOfWork
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.state import LastRun
from trading_bot.scheduler.policy import DEFAULT_POLICY, SchedulerPolicy
from trading_bot.scheduler.state import read_last_run, record_run
from trading_bot.scheduler.trigger import SlotPredicate, run_every_slot

__all__ = ["FireStatus", "RunAttempt", "SignalRunner", "Sleep", "TimeframeRunner"]

_LOGGER_NAME: Final = "trading_bot.scheduler"


class SignalRunner(Protocol):
    """What the scheduler needs from the engine (spec 015, Design 8.1)."""

    async def run(
        self, timeframe: Timeframe, now: datetime, *, tickers: Sequence[str] | None = None
    ) -> RunReport: ...


type Sleep = Callable[[float], Awaitable[None]]


class FireStatus(StrEnum):
    """What one fire did."""

    RAN = "ran"  # the engine ran; the run was recorded
    ALREADY_RUN = "already_run"  # bot_state already holds this close (decision D144)
    BUSY = "busy"  # a run of this timeframe is in flight (issue AC)
    STALE = "stale"  # the fire arrived outside the misfire window (decision D146)
    SLOT_SKIPPED = "slot_skipped"  # the predicate rejects this slot (decision D150)
    STOPPING = "stopping"  # aclose() was called
    FAILED = "failed"  # an Exception escaped the engine; nothing was recorded


@dataclass(frozen=True, slots=True, kw_only=True)
class RunAttempt:
    """The outcome of one fire, as values only: no exception object, no provider text."""

    timeframe: Timeframe
    scheduled_close: datetime | None  # None when the slot could not be determined
    status: FireStatus
    engine_calls: int
    # The last report of a fire that completed; a FAILED fire reports None even when an earlier
    # attempt of the same fire returned one. The engine logs every report's summary anyway.
    report: RunReport | None
    unresolved: tuple[str, ...]  # retryable symbols left when the budget ended


class TimeframeRunner:
    """One fire of one timeframe: derive the candle, run the engine, retry, record the run.

    One instance serves every timeframe and is shared by the recurring jobs and the startup
    catch-up, so the locks it owns really are process-wide: one per timeframe, plus one global
    lock held only while the engine runs.
    """

    def __init__(
        self,
        *,
        engine: SignalRunner,
        calendar: MarketCalendar,
        unit_of_work: UnitOfWork,
        policy: SchedulerPolicy = DEFAULT_POLICY,
        clock: Clock = system_clock,
        sleep: Sleep = asyncio.sleep,
        should_run: SlotPredicate = run_every_slot,
    ) -> None:
        if not isinstance(calendar, MarketCalendar):
            raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
        if not isinstance(policy, SchedulerPolicy):
            raise TypeError(f"policy must be a SchedulerPolicy, got {type(policy).__name__}")
        self._engine = engine
        self._calendar = calendar
        self._unit_of_work = unit_of_work
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._should_run = should_run
        self._logger = logging.getLogger(_LOGGER_NAME)
        # Built eagerly and never rebuilt: an ``asyncio.Lock`` binds to a loop only when it is
        # first awaited, so building them outside the loop is safe and keeps the set fixed.
        self._timeframe_locks = {timeframe: asyncio.Lock() for timeframe in Timeframe}
        self._run_lock = asyncio.Lock()
        self._in_flight: set[Timeframe] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False

    @property
    def in_flight(self) -> tuple[Timeframe, ...]:
        """The timeframes whose run has not finished yet, in ``Timeframe`` order."""
        return tuple(timeframe for timeframe in Timeframe if timeframe in self._in_flight)

    async def run_timeframe(self, timeframe: Timeframe, *, catch_up: bool = False) -> RunAttempt:
        """One fire. It never raises an ``Exception``; only ``BaseException`` leaves it.

        ``catch_up`` only changes what is logged: the startup catch-up goes through exactly the
        same checks as a scheduled fire (decision D147).
        """
        if not isinstance(timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
                "(use Timeframe.parse for text)"
            )
        if self._stopping:
            self._logger.debug("%s fire skipped: stopping", timeframe.value)
            return _attempt(timeframe, None, FireStatus.STOPPING)
        try:
            slot = self._calendar.closed_candles(timeframe, self._clock(), 1)[-1]
        except Exception as error:
            self._logger.error("%s run failed: %s", timeframe.value, type(error).__name__)
            return _attempt(timeframe, None, FireStatus.FAILED)
        return await self._fire(timeframe, slot, catch_up=catch_up)

    def stop(self) -> None:
        """Refuse new runs and make a retry window in flight give up before its next sleep."""
        self._stopping = True

    async def drain(self, timeout: timedelta) -> bool:
        """Wait for the runs in flight; ``False`` when one was still running at the timeout.

        It returns immediately when nothing is running, and ``in_flight`` names what was still
        going when it gave up, which is what ``SchedulerService.aclose`` reports.
        """
        try:
            async with asyncio.timeout(timeout.total_seconds()):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True

    async def _fire(self, timeframe: Timeframe, slot: CandleSlot, *, catch_up: bool) -> RunAttempt:
        """Steps 3 to 8 of Design 8.1, with the two locks of Design 8.2."""
        close = slot.close_time
        try:
            if not self._should_run(slot):
                self._logger.debug(
                    "%s fire for %s skipped: slot not evaluated", timeframe.value, close.isoformat()
                )
                return _attempt(timeframe, close, FireStatus.SLOT_SKIPPED)
        except Exception as error:
            return self._failed(timeframe, close, error)
        lock = self._timeframe_locks[timeframe]
        # ``locked()`` and ``acquire()`` are one synchronous step: ``acquire`` does not yield
        # when the lock is free, so no other fire can slip in between the two.
        if lock.locked():
            self._logger.warning("%s fire for %s skipped: busy", timeframe.value, close.isoformat())
            return _attempt(timeframe, close, FireStatus.BUSY)
        await lock.acquire()
        self._in_flight.add(timeframe)
        self._idle.clear()
        try:
            return await self._run_once(timeframe, close, catch_up=catch_up)
        finally:
            self._in_flight.discard(timeframe)
            if not self._in_flight:
                self._idle.set()
            lock.release()

    async def _run_once(
        self, timeframe: Timeframe, close: datetime, *, catch_up: bool
    ) -> RunAttempt:
        """The window, the "already run" guard, the attempts and the record."""
        calls = 0
        report: RunReport | None = None
        pending: tuple[str, ...] = ()
        try:
            late = self._clock() - (close + self._policy.close_delay)
            if late > self._policy.misfire_grace:
                self._logger.warning(
                    "%s fire for %s skipped: stale by %ds",
                    timeframe.value,
                    close.isoformat(),
                    int(late.total_seconds()),
                )
                return _attempt(timeframe, close, FireStatus.STALE)
            last = await read_last_run(self._unit_of_work, timeframe)
            if last is not None and last.scheduled_at >= close:
                self._logger.debug(
                    "%s fire for %s skipped: already run", timeframe.value, close.isoformat()
                )
                return _attempt(timeframe, close, FireStatus.ALREADY_RUN)
            if catch_up:
                self._logger.info(
                    "%s catch-up run for %s (last run %s)",
                    timeframe.value,
                    close.isoformat(),
                    _last_run_text(last),
                )

            deadline = self._deadline(timeframe, close)
            requested: tuple[str, ...] | None = None
            while True:
                async with self._run_lock:
                    report = await self._engine.run(timeframe, close, tickers=requested)
                calls += 1
                pending = report.retryable_tickers
                if not pending:
                    break
                if calls > len(self._policy.retry_backoff) or self._stopping:
                    break
                wait = self._policy.retry_backoff[calls - 1]
                if self._clock() + wait >= deadline:
                    break
                self._logger.debug(
                    "%s retry %d in %ds",
                    timeframe.value,
                    calls - 1,
                    int(wait.total_seconds()),
                )
                await self._sleep(wait.total_seconds())
                requested = pending

            self._log_outcome(timeframe, close, calls, requested, pending)
            await record_run(self._unit_of_work, timeframe, close)
        except Exception as error:
            return self._failed(timeframe, close, error, engine_calls=calls)
        return RunAttempt(
            timeframe=timeframe,
            scheduled_close=close,
            status=FireStatus.RAN,
            engine_calls=calls,
            report=report,
            unresolved=pending,
        )

    def _deadline(self, timeframe: Timeframe, close: datetime) -> datetime:
        """When the retry budget ends: the earlier of two bounds (decision D149).

        The next close of this timeframe is spec 010 D44's hard bound — a retry window must
        never overlap the next run — and ``retry_window`` is this spec's: the next ``1h`` close
        after the last slot of a session is the next morning, and retrying a delisted-looking
        symbol for eighteen hours is noise, not resilience.
        """
        return min(
            self._calendar.next_candle_close(timeframe, close),
            self._clock() + self._policy.retry_window,
        )

    def _log_outcome(
        self,
        timeframe: Timeframe,
        close: datetime,
        calls: int,
        requested: tuple[str, ...] | None,
        pending: tuple[str, ...],
    ) -> None:
        """One line about the retries, or nothing: the engine logs its own run summary."""
        if pending:
            self._logger.warning(
                "%s run for %s: gave up on %d tickers after %d attempts (%s)",
                timeframe.value,
                close.isoformat(),
                len(pending),
                calls,
                ", ".join(pending),
            )
        elif requested is not None:
            self._logger.info(
                "%s run for %s: resolved %d tickers after %d retries",
                timeframe.value,
                close.isoformat(),
                len(requested),
                calls - 1,
            )

    def _failed(
        self, timeframe: Timeframe, close: datetime, error: Exception, *, engine_calls: int = 0
    ) -> RunAttempt:
        """One ``ERROR`` with the exception **class** only; nothing is recorded (D155)."""
        self._logger.error(
            "%s run for %s failed: %s",
            timeframe.value,
            close.isoformat(),
            type(error).__name__,
        )
        return _attempt(timeframe, close, FireStatus.FAILED, engine_calls=engine_calls)


def _attempt(
    timeframe: Timeframe,
    close: datetime | None,
    status: FireStatus,
    *,
    engine_calls: int = 0,
) -> RunAttempt:
    return RunAttempt(
        timeframe=timeframe,
        scheduled_close=close,
        status=status,
        engine_calls=engine_calls,
        report=None,
        unresolved=(),
    )


def _last_run_text(last: LastRun | None) -> str:
    return "never" if last is None else last.scheduled_at.isoformat()

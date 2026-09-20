"""The schedule: real candle closes plus the delay (spec 016, Design 7; decisions D142, D151).

``next_fire`` is the whole schedule and it is a **pure function** of the calendar: it imports no
scheduler library, reads no clock and holds no state, so the irregular grid — holidays, half
days, DST changes and a final slot truncated to thirty minutes — is tested without starting
anything. ``CandleCloseTrigger`` is the thin adapter that hands those instants to APScheduler.

Market hours are honoured by construction (decision D151): every fire time comes from a slot of
the calendar grid, so the trigger never asks ``is_open``. Asking would be wrong, not merely
redundant: the fire for the last candle of a session happens **after** the close by definition,
so an ``is_open`` gate would drop the most interesting candle of every day and, on ``1d``, every
single run.

``nominal_close`` is never used here (CLAUDE.md rule 4, spec 004): it would fire a day late for
``1d`` and miss the last candle of every session.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final

from apscheduler.triggers.base import BaseTrigger

from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["CandleCloseTrigger", "SlotPredicate", "next_fire", "run_every_slot"]

_LOGGER_NAME: Final = "trading_bot.scheduler"

# ``MarketCalendar.next_candle_close`` is strictly after its argument (spec 009, D19), so the
# "at or after" boundary is reached by stepping back the smallest instant a datetime can hold.
RESOLUTION: Final = timedelta(microseconds=1)

# A predicate that rejects everything must fail loudly instead of walking a century of sessions.
MAX_CONSECUTIVE_REJECTIONS: Final = 64

type SlotPredicate = Callable[[CandleSlot], bool]  # True: this slot is evaluated


def run_every_slot(slot: CandleSlot) -> bool:
    """The default predicate: every candle of the grid is evaluated."""
    return True


def next_fire(
    calendar: MarketCalendar,
    timeframe: Timeframe,
    *,
    after: datetime,
    delay: timedelta,
    should_run: SlotPredicate = run_every_slot,
) -> tuple[CandleSlot, datetime]:
    """The first slot whose fire time (``close_time + delay``) is at or after ``after``.

    The returned instant is ``slot.close_time + delay``, so a fire is never earlier than the
    real close of the candle it is about, and the slots ``should_run`` rejects are passed over
    (decision D150).

    Raises ``CalendarRangeError`` when the calendar cannot answer — the instant is outside its
    coverage, or no slot closes after it — and ``ValueError`` when ``should_run`` rejects
    ``MAX_CONSECUTIVE_REJECTIONS`` slots in a row. A naive ``after`` is a ``ValueError`` and any
    argument of the wrong type a ``TypeError``.
    """
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    if not isinstance(timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )
    if isinstance(delay, bool) or not isinstance(delay, timedelta):
        raise TypeError(f"delay must be a timedelta, got {type(delay).__name__}")
    instant = to_utc(after)
    close = calendar.next_candle_close(timeframe, instant - delay - RESOLUTION)
    rejections = 0
    while True:
        # ``close`` is a real close of the grid, so exactly one slot of ``timeframe`` closed at
        # it and ``closed_candles`` returns that slot last.
        slot = calendar.closed_candles(timeframe, close, 1)[-1]
        if should_run(slot):
            return slot, close + delay
        rejections += 1
        if rejections == MAX_CONSECUTIVE_REJECTIONS:
            raise ValueError(
                f"the slot predicate rejected {MAX_CONSECUTIVE_REJECTIONS} consecutive "
                f"{timeframe} candles after {instant.isoformat()}"
            )
        close = calendar.next_candle_close(timeframe, close)


class CandleCloseTrigger(BaseTrigger):
    """An APScheduler trigger that fires at each candle close of one timeframe, plus the delay.

    It keeps no state between calls: the next fire is always recomputed from the calendar by
    ``next_fire``, so a coalesced burst, a restart or a job that ran late cannot leave the
    sequence behind. This module and ``service.py`` are the only two that may import
    APScheduler (decision D141), and a guard test enforces it.
    """

    __slots__ = ("_calendar", "_delay", "_logger", "_should_run", "_timeframe")

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        timeframe: Timeframe,
        delay: timedelta,
        should_run: SlotPredicate = run_every_slot,
    ) -> None:
        if not isinstance(calendar, MarketCalendar):
            raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"timeframe must be a Timeframe, got {type(timeframe).__name__}")
        if isinstance(delay, bool) or not isinstance(delay, timedelta):
            raise TypeError(f"delay must be a timedelta, got {type(delay).__name__}")
        self._calendar = calendar
        self._timeframe = timeframe
        self._delay = delay
        self._should_run = should_run
        self._logger = logging.getLogger(_LOGGER_NAME)

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime
    ) -> datetime | None:
        """The next fire at or after ``now``, strictly after ``previous_fire_time``.

        Taking the later of the two makes the sequence advance even when APScheduler asks with
        a ``now`` that has not moved, and a fire that ran late resumes at the present instead of
        replaying the closes that went by meanwhile.

        A calendar that cannot answer gives **one** ``ERROR`` and ``None``, which drops this job
        (AC5): raising inside APScheduler's loop would take the whole scheduler down with it.
        """
        after = to_utc(now)
        if previous_fire_time is not None:
            after = max(after, to_utc(previous_fire_time) + RESOLUTION)
        try:
            return next_fire(
                self._calendar,
                self._timeframe,
                after=after,
                delay=self._delay,
                should_run=self._should_run,
            )[1]
        # ``CalendarRangeError`` is a ``ValueError``, and so is the rejected-predicate failure:
        # both mean "this job has no next fire", and both are configuration problems.
        except ValueError as error:
            self._logger.error(
                "%s trigger: no next fire after %s (%s)",
                self._timeframe.value,
                after.isoformat(),
                type(error).__name__,
            )
            return None

    def __str__(self) -> str:
        return f"candle close {self._timeframe.value} + {int(self._delay.total_seconds())}s"

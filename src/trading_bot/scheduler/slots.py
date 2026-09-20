"""The slots one provider never publishes (spec 016, Design 7.3; decision D150, user U7).

The only module of ``scheduler/`` that knows a provider exists. Yahoo never publishes the
12:30-13:00 New York half hour of an early-close session as hourly data (spec 011, D65), so
firing for it would produce one failed run per ticker about three days a year; not firing at all
means no request, no failure and no log noise.

Keeping the answer in a predicate keeps ``trigger.py`` and ``runner.py`` provider-agnostic: a
second provider replaces this one adapter, and #16 chooses which predicate to inject.
"""

from __future__ import annotations

from trading_bot.data.yahoo.provider import is_unpublished_hour
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.scheduler.trigger import SlotPredicate

__all__ = ["skip_unpublished_hours"]


def skip_unpublished_hours(calendar: MarketCalendar) -> SlotPredicate:
    """A predicate that skips the ``1h`` slots Yahoo never publishes (spec 011, D65).

    ``4h`` and ``1d`` slots are accepted without consulting ``is_unpublished_hour``, which
    raises for any timeframe but ``1h``.
    """
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")

    def should_run(slot: CandleSlot) -> bool:
        if slot.timeframe is not Timeframe.H1:
            return True
        return not is_unpublished_hour(slot, calendar=calendar)

    return should_run

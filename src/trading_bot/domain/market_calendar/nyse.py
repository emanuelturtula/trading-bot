"""The NYSE regular-session calendar, built from ``exchange_calendars`` (spec 009, Design 6).

This is the only module that imports ``exchange_calendars``: the model and its queries live in
``sessions`` and never touch the library, so replacing the source means rewriting this module.
``build_nyse_calendar`` instantiates the library's XNYS class directly with explicit bounds and
converts its schedule once into an immutable ``MarketCalendar``; it never goes through the
library's module-level calendar registry, which caches instances and defaults its bounds from
the wall clock. No library object outlives the call.

v1 uses this single calendar for every US-listed stock and ETF (spec 009, D27): NYSE, Nasdaq,
NYSE Arca, NYSE American and Cboe listings share NYSE hours and holidays.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final

from exchange_calendars.exchange_calendar_xnys import XNYSExchangeCalendar

from trading_bot.domain.market_calendar.sessions import MarketCalendar, Session

__all__ = [
    "NYSE_CALENDAR_NAME",
    "NYSE_FIRST_SUPPORTED_DAY",
    "NYSE_LAST_SUPPORTED_DAY",
    "build_nyse_calendar",
]

NYSE_CALENDAR_NAME: Final = "XNYS"  # ISO 10383 market identifier
NYSE_FIRST_SUPPORTED_DAY: Final = date(2000, 1, 1)
NYSE_LAST_SUPPORTED_DAY: Final = date(2099, 12, 31)
# The library rejects a one-day span and a span without sessions. Every 15-day window of the
# supported range holds a session, so the library calendar is built with this padding after
# ``last_day`` and the extra sessions are dropped.
_LIBRARY_PADDING: Final = timedelta(days=14)


def build_nyse_calendar(first_day: date, last_day: date) -> MarketCalendar:
    """Every NYSE regular session from ``first_day`` to ``last_day`` (inclusive), in UTC.

    Both days must be dates (not datetimes) with ``first_day <= last_day``, inside
    ``[NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY]``. Any such span works, including one
    day and a span without sessions. The result depends only on the days and the installed
    library release: the clock is never read.
    """
    first = _plain_day(first_day, "first_day")
    last = _plain_day(last_day, "last_day")
    if first > last:
        raise ValueError(
            f"first_day {first.isoformat()} must not be after last_day {last.isoformat()}"
        )
    if first < NYSE_FIRST_SUPPORTED_DAY or last > NYSE_LAST_SUPPORTED_DAY:
        raise ValueError(
            f"first_day and last_day must be within {NYSE_FIRST_SUPPORTED_DAY.isoformat()} to "
            f"{NYSE_LAST_SUPPORTED_DAY.isoformat()}, got {first.isoformat()} to {last.isoformat()}"
        )
    library = XNYSExchangeCalendar(start=first, end=last + _LIBRARY_PADDING)
    schedule = library.schedule
    # The index holds naive session dates as timestamps; ``open`` and ``close`` are UTC
    # timestamps in whole minutes, which ``Session`` converts with ``to_utc``. XNYS has no
    # intraday breaks, so the break columns are ignored.
    sessions = tuple(
        Session(day=label.date(), open_time=opens, close_time=closes)
        for label, opens, closes in zip(
            schedule.index, schedule["open"], schedule["close"], strict=True
        )
        if label.date() <= last
    )
    return MarketCalendar(
        name=NYSE_CALENDAR_NAME,
        timezone=library.tz,
        first_day=first,
        last_day=last,
        sessions=sessions,
    )


def _plain_day(value: object, argument: str) -> date:
    """``value`` as a plain ``date``; a ``datetime`` (or ``pd.Timestamp``) is not a day."""
    if isinstance(value, datetime) or not isinstance(value, date):
        # The type name is not echoed: messages carry no caller-controlled text (AC10).
        raise TypeError(f"{argument} must be a date that is not a datetime")
    return date(value.year, value.month, value.day)

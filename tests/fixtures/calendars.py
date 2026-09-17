"""Shared market calendar helpers for the calendar tests and its consumers (spec 009, Design 7).

There is no network and no clock here: every instant is a literal. ``toy_calendar`` is a
hand-built calendar that exercises the model without ``exchange_calendars``; the NYSE calendar
is built for explicit days, once per process.

Always import this module as ``tests.fixtures.calendars``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from functools import cache
from typing import Final
from zoneinfo import ZoneInfo

from trading_bot.domain.market_calendar.nyse import build_nyse_calendar
from trading_bot.domain.market_calendar.sessions import MarketCalendar, Session

__all__ = [
    "NEW_YORK",
    "NYSE_TEST_FIRST_DAY",
    "NYSE_TEST_LAST_DAY",
    "TOY_FIRST_DAY",
    "TOY_LAST_DAY",
    "nyse_test_calendar",
    "toy_calendar",
    "utc",
    "wall_session",
]

NEW_YORK: Final = ZoneInfo("America/New_York")
NYSE_TEST_FIRST_DAY: Final = date(2021, 1, 1)
NYSE_TEST_LAST_DAY: Final = date(2027, 12, 31)
TOY_FIRST_DAY: Final = date(2024, 3, 6)
TOY_LAST_DAY: Final = date(2024, 3, 14)


def utc(text: str) -> datetime:
    """``utc("2024-07-03T17:00")``: an ISO wall time read as a stdlib UTC datetime.

    The text must not carry an offset, so a literal cannot silently name another instant.
    """
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError("utc() expects a wall time without an offset")
    return parsed.replace(tzinfo=UTC)


def wall_session(day: date, opens: time, closes: time, *, timezone: tzinfo = NEW_YORK) -> Session:
    """A session from exchange wall-clock times on ``day``."""
    return Session(
        day=day,
        open_time=datetime.combine(day, opens, tzinfo=timezone),
        close_time=datetime.combine(day, closes, tzinfo=timezone),
    )


def toy_calendar() -> MarketCalendar:
    """The hand-built calendar of spec 009, Design 7: every model test runs without the library.

    ======================  ===============  ===============  ============================
    Day                     Session (ET)     UTC              Purpose
    ======================  ===============  ===============  ============================
    2024-03-06 Wed          none             -                coverage starts without one
    2024-03-07 Thu          09:30-16:00      14:30Z-21:00Z    regular (EST)
    2024-03-08 Fri          09:30-13:00      14:30Z-18:00Z    half day (EST)
    2024-03-09, 2024-03-10  none             -                weekend with the DST change
    2024-03-11 Mon          09:30-16:00      13:30Z-20:00Z    regular (EDT)
    2024-03-12 Tue          none             -                weekday holiday
    2024-03-13 Wed          10:00-15:00      14:00Z-19:00Z    ad hoc late open, early close
    2024-03-14 Thu          none             -                no session after the last close
    ======================  ===============  ===============  ============================
    """
    return MarketCalendar(
        name="TOY",
        timezone=NEW_YORK,
        first_day=TOY_FIRST_DAY,
        last_day=TOY_LAST_DAY,
        sessions=(
            wall_session(date(2024, 3, 7), time(9, 30), time(16, 0)),
            wall_session(date(2024, 3, 8), time(9, 30), time(13, 0)),
            wall_session(date(2024, 3, 11), time(9, 30), time(16, 0)),
            wall_session(date(2024, 3, 13), time(10, 0), time(15, 0)),
        ),
    )


@cache
def nyse_test_calendar() -> MarketCalendar:
    """``build_nyse_calendar(NYSE_TEST_FIRST_DAY, NYSE_TEST_LAST_DAY)``, built once per process.

    The calendar is immutable, so sharing one instance between tests is safe.
    """
    return build_nyse_calendar(NYSE_TEST_FIRST_DAY, NYSE_TEST_LAST_DAY)

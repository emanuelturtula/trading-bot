"""Adversarial arguments and edges of the calendar model and the NYSE builder (spec 009, T14).

``test_market_calendar_sessions.py`` (T1-T8) and ``test_market_calendar_nyse.py`` (T9-T11) are the
developer's TDD tests, written before the implementation; this file re-probes the same surface
from the outside with inputs the Test plan specifically calls out for the tester: DST folds and
the spring gap across every year of the NYSE test span, microsecond boundaries around every half
day, a huge ``count``, calendars without sessions (both the toy shape and a real NYSE closed day),
a fully functional fixed-offset calendar, constructor abuse (a list of sessions, a non-``Session``
item, a ``datetime`` subclass as a day, ``first_day == date.min``, ``last_day == date.max``),
message bounds on the NYSE calendar, and ``candle_slots`` at exactly ``coverage_start`` and
``coverage_end``. No network, no wall clock: every instant is a literal or derived from one by
plain date arithmetic, and the NYSE calendar is built once per process.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, utc, wall_session
from trading_bot.domain.market_calendar.nyse import build_nyse_calendar
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleLabelError,
    CandleLabelErrorKind,
    MarketCalendar,
    Session,
)
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()


def _first_sunday(year: int, month: int) -> date:
    """The first Sunday of ``month`` in ``year``: plain date arithmetic, not the code under test."""
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7)


def _fall_back_sunday(year: int) -> date:
    """US DST ends on the first Sunday of November."""
    return _first_sunday(year, 11)


def _spring_forward_sunday(year: int) -> date:
    """US DST begins on the second Sunday of March."""
    return _first_sunday(year, 3) + timedelta(days=7)


# --- DST folds and the spring gap, across every year of the NYSE test span ----------------------


@pytest.mark.parametrize("year", range(2021, 2028))
@pytest.mark.parametrize("fold", [0, 1])
def test_ambiguous_fall_back_wall_time_is_closed_every_year(year: int, fold: int) -> None:
    sunday = _fall_back_sunday(year)
    ambiguous = datetime(sunday.year, sunday.month, sunday.day, 1, 30, tzinfo=NEW_YORK, fold=fold)

    assert NYSE.is_open(ambiguous) is False
    assert NYSE.next_candle_close(H1, ambiguous) > ambiguous


@pytest.mark.parametrize("year", range(2021, 2028))
@pytest.mark.parametrize("fold", [0, 1])
def test_nonexistent_spring_forward_wall_time_is_closed_every_year(year: int, fold: int) -> None:
    sunday = _spring_forward_sunday(year)
    nonexistent = datetime(sunday.year, sunday.month, sunday.day, 2, 30, tzinfo=NEW_YORK, fold=fold)

    assert NYSE.is_open(nonexistent) is False
    assert NYSE.next_candle_close(D1, nonexistent) > nonexistent


# --- Microsecond boundaries around every half day of the golden table ---------------------------

HALF_DAYS = [
    ("2024-07-03", "13:30", "17:00"),
    ("2025-07-03", "13:30", "17:00"),
    ("2024-11-29", "14:30", "18:00"),
    ("2024-12-24", "14:30", "18:00"),
    ("2025-11-28", "14:30", "18:00"),
    ("2025-12-24", "14:30", "18:00"),
    ("2026-11-27", "14:30", "18:00"),
    ("2026-12-24", "14:30", "18:00"),
]


@pytest.mark.parametrize(("day", "opens", "closes"), HALF_DAYS)
def test_half_day_boundaries_at_microsecond_precision(day: str, opens: str, closes: str) -> None:
    open_time = utc(f"{day}T{opens}")
    close_time = utc(f"{day}T{closes}")
    step = timedelta(microseconds=1)

    assert NYSE.is_open(open_time - step) is False
    assert NYSE.is_open(open_time) is True
    assert NYSE.is_open(close_time - step) is True
    assert NYSE.is_open(close_time) is False


# --- A huge count on the NYSE-scale calendar (not just the small toy calendar) ------------------


@pytest.mark.parametrize("timeframe", [H1, H4, D1])
def test_closed_candles_with_a_huge_count_raises_on_the_nyse_calendar(timeframe: Timeframe) -> None:
    with pytest.raises(CalendarRangeError):
        NYSE.closed_candles(timeframe, utc("2024-07-03T17:00"), 10**9)


# --- Calendars without sessions: a real NYSE closed day, every query -----------------------------


def test_queries_on_a_real_nyse_calendar_without_sessions() -> None:
    closed_day = build_nyse_calendar(date(2025, 1, 9), date(2025, 1, 9))

    assert closed_day.sessions == ()
    assert closed_day.is_open(utc("2025-01-09T16:00")) is False
    assert closed_day.candle_slots(H1, closed_day.coverage_start, closed_day.coverage_end) == ()
    assert closed_day.closed_candles(D1, utc("2025-01-09T16:00"), 0) == ()
    with pytest.raises(CalendarRangeError):
        closed_day.next_candle_close(H1, closed_day.coverage_start)
    with pytest.raises(CalendarRangeError):
        closed_day.closed_candles(D1, utc("2025-01-09T16:00"), 1)
    with pytest.raises(CandleLabelError) as caught:
        closed_day.candle_slot(H1, utc("2025-01-09T14:30"))
    assert caught.value.kind is CandleLabelErrorKind.NOT_A_SESSION


# --- A fully functional calendar with a fixed-offset tzinfo (not ZoneInfo, no DST) ---------------


def test_full_queries_on_a_fixed_offset_calendar() -> None:
    tokyo_like = timezone(timedelta(hours=9))
    calendar = MarketCalendar(
        name="FX",
        timezone=tokyo_like,
        first_day=date(2024, 1, 1),
        last_day=date(2024, 1, 5),
        sessions=(
            wall_session(date(2024, 1, 2), time(9, 0), time(15, 0), timezone=tokyo_like),
            wall_session(date(2024, 1, 4), time(9, 0), time(11, 30), timezone=tokyo_like),
        ),
    )

    session = calendar.session_bounds(date(2024, 1, 2))
    assert session is not None
    assert session.open_time == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert session.close_time == datetime(2024, 1, 2, 6, 0, tzinfo=UTC)
    assert calendar.is_open(datetime(2024, 1, 2, 3, 0, tzinfo=UTC)) is True
    assert calendar.is_open(datetime(2024, 1, 2, 6, 0, tzinfo=UTC)) is False

    hourly = calendar.candle_slots(H1, session.open_time, session.close_time)
    assert len(hourly) == 6
    assert hourly[0].open_time == session.open_time
    assert hourly[-1].close_time == session.close_time
    assert calendar.next_candle_close(H1, session.open_time) == hourly[0].close_time

    end = calendar.coverage_end - timedelta(microseconds=1)
    last_daily = calendar.closed_candles(D1, end, 2)[-1]
    assert last_daily.session_day == date(2024, 1, 4)


# --- Constructor abuse ---------------------------------------------------------------------------


class _DatetimeSubclass(datetime):
    pass


def test_session_day_rejects_a_datetime_subclass() -> None:
    with pytest.raises(TypeError, match="day"):
        Session(
            day=_DatetimeSubclass(2024, 3, 7),
            open_time=utc("2024-03-07T14:30"),
            close_time=utc("2024-03-07T21:00"),
        )


def test_calendar_first_day_rejects_a_datetime_subclass() -> None:
    with pytest.raises(TypeError, match="first_day"):
        MarketCalendar(
            name="TOY",
            timezone=NEW_YORK,
            first_day=_DatetimeSubclass(2024, 3, 6),
            last_day=date(2024, 3, 14),
            sessions=(),
        )


def test_build_nyse_calendar_rejects_a_datetime_subclass_day() -> None:
    with pytest.raises(TypeError, match="first_day"):
        build_nyse_calendar(_DatetimeSubclass(2024, 7, 3), date(2024, 7, 3))


def test_calendar_rejects_a_list_of_sessions() -> None:
    with pytest.raises(TypeError, match="sessions"):
        MarketCalendar(
            name="TOY",
            timezone=NEW_YORK,
            first_day=date(2024, 3, 6),
            last_day=date(2024, 3, 14),
            sessions=[wall_session(date(2024, 3, 7), time(9, 30), time(16, 0))],
        )


def test_calendar_rejects_a_non_session_item() -> None:
    with pytest.raises(TypeError, match="Session"):
        MarketCalendar(
            name="TOY",
            timezone=NEW_YORK,
            first_day=date(2024, 3, 6),
            last_day=date(2024, 3, 14),
            sessions=(wall_session(date(2024, 3, 7), time(9, 30), time(16, 0)), "not-a-session"),
        )


def test_calendar_first_day_cannot_equal_date_min() -> None:
    with pytest.raises(ValueError, match="first_day"):
        MarketCalendar(
            name="TOY",
            timezone=NEW_YORK,
            first_day=date.min,
            last_day=date(2024, 3, 14),
            sessions=(),
        )


def test_calendar_last_day_cannot_equal_date_max() -> None:
    with pytest.raises(ValueError, match="first_day"):
        MarketCalendar(
            name="TOY",
            timezone=NEW_YORK,
            first_day=date(2024, 3, 6),
            last_day=date.max,
            sessions=(),
        )


# --- Message bounds on the NYSE calendar (not just the small toy calendar) ----------------------


def test_nyse_calendar_range_error_message_is_bounded() -> None:
    with pytest.raises(CalendarRangeError) as caught:
        NYSE.closed_candles(D1, utc("2027-12-31T23:00"), 10**9)

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 300


def test_nyse_candle_label_error_message_is_bounded() -> None:
    with pytest.raises(CandleLabelError) as caught:
        NYSE.candle_slot(H1, utc("2024-07-03T14:00"))

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 300


# --- candle_slots exactly at coverage_start and coverage_end, on the NYSE calendar ---------------


def test_candle_slots_at_the_nyse_coverage_edges() -> None:
    assert NYSE.candle_slots(D1, NYSE.coverage_start, NYSE.coverage_start) == ()
    assert NYSE.candle_slots(D1, NYSE.coverage_end, NYSE.coverage_end) == ()

    whole = NYSE.candle_slots(D1, NYSE.coverage_start, NYSE.coverage_end)
    assert whole[0].session_day == NYSE.sessions[0].day
    assert whole[-1].session_day == NYSE.sessions[-1].day
    assert len(whole) == len(NYSE.sessions)


def test_candle_slots_with_equal_bounds_outside_the_coverage_raises() -> None:
    """AC3: an out-of-range instant raises ``CalendarRangeError`` even when ``start == end``.

    AC6 states ``start == end`` returns ``()``, but AC3 requires every instant to be inside the
    calendar first; the two combine so that only an *in-range* empty window is a silent ``()``.
    """
    before = NYSE.coverage_start - timedelta(microseconds=1)
    with pytest.raises(CalendarRangeError):
        NYSE.candle_slots(H1, before, before)

    after = NYSE.coverage_end + timedelta(microseconds=1)
    with pytest.raises(CalendarRangeError):
        NYSE.candle_slots(H1, after, after)

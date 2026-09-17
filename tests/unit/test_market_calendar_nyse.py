"""Tests of the NYSE calendar builder and its golden tables (spec 009, T9-T11).

Every expected value is a literal copied from spec 009, Design 5: holidays, half days, DST,
next closes, closed candles, the grid by day and the label kinds. Nothing is recomputed with the
code under test. The NYSE calendar is built once per process through
``tests.fixtures.calendars.nyse_test_calendar`` (2021-01-01 to 2027-12-31); no test reads the
clock or the network.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, utc
from trading_bot.domain.market_calendar import nyse
from trading_bot.domain.market_calendar.nyse import (
    NYSE_CALENDAR_NAME,
    NYSE_FIRST_SUPPORTED_DAY,
    NYSE_LAST_SUPPORTED_DAY,
    build_nyse_calendar,
)
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleLabelError,
    CandleLabelErrorKind,
    CandleSlot,
    MarketCalendar,
    Session,
)
from trading_bot.domain.timeframe import Timeframe

H1 = Timeframe.H1
H4 = Timeframe.H4
D1 = Timeframe.D1
NYSE = nyse_test_calendar()


def year_start(year: int) -> datetime:
    """00:00 ET on January 1 of ``year``, always EST (UTC-05:00)."""
    return utc(f"{year}-01-01T05:00")


def utc_times(day: str, text: str) -> list[datetime]:
    """``utc_times("2024-01-02", "14:30, 15:30")``: UTC instants on ``day``."""
    return [utc(f"{day}T{item.strip()}") for item in text.split(",")]


# --- T9: builder contract (AC11) ---------------------------------------------------------------


def test_nyse_constants() -> None:
    constants = (NYSE_CALENDAR_NAME, NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)

    assert constants == (
        "XNYS",
        date(2000, 1, 1),
        date(2099, 12, 31),
    )
    assert nyse.__all__ == [
        "NYSE_CALENDAR_NAME",
        "NYSE_FIRST_SUPPORTED_DAY",
        "NYSE_LAST_SUPPORTED_DAY",
        "build_nyse_calendar",
    ]


def test_nyse_test_calendar_has_the_nyse_identity_and_the_given_days() -> None:
    assert isinstance(NYSE, MarketCalendar)
    assert NYSE.name == "XNYS"
    assert str(NYSE.timezone) == "America/New_York"
    assert NYSE.first_day == date(2021, 1, 1)
    assert NYSE.last_day == date(2027, 12, 31)
    assert NYSE.coverage_start == utc("2021-01-01T05:00")
    assert NYSE.coverage_end == utc("2028-01-01T05:00")
    assert NYSE.sessions[0].day == date(2021, 1, 4)
    assert NYSE.sessions[-1].day == date(2027, 12, 31)


@pytest.mark.parametrize("argument", ["first_day", "last_day"])
@pytest.mark.parametrize(
    "value",
    [datetime(2024, 7, 3), pd.Timestamp("2024-07-03"), "2024-07-03", None, 20240703],
    ids=["datetime", "timestamp", "str", "none", "int"],
)
def test_build_nyse_calendar_accepts_only_dates(argument: str, value: object) -> None:
    arguments: dict[str, object] = {"first_day": date(2024, 7, 3), "last_day": date(2024, 7, 3)}
    arguments[argument] = value

    with pytest.raises(TypeError, match=argument):
        build_nyse_calendar(**arguments)


@pytest.mark.parametrize(
    ("first_day", "last_day"),
    [
        (date(2024, 7, 4), date(2024, 7, 3)),
        (date(1999, 12, 31), date(2000, 1, 3)),
        (date(2099, 12, 30), date(2100, 1, 1)),
    ],
    ids=["reversed", "before-supported", "after-supported"],
)
def test_build_nyse_calendar_rejects_spans_outside_the_supported_range(
    first_day: date, last_day: date
) -> None:
    with pytest.raises(ValueError, match=r"first_day|last_day"):
        build_nyse_calendar(first_day, last_day)


def test_build_nyse_calendar_of_a_closed_day_has_no_sessions() -> None:
    calendar = build_nyse_calendar(date(2025, 1, 9), date(2025, 1, 9))

    assert calendar.sessions == ()
    assert calendar.first_day == calendar.last_day == date(2025, 1, 9)
    assert calendar.is_open(utc("2025-01-09T16:00")) is False


def test_build_nyse_calendar_of_a_half_day() -> None:
    calendar = build_nyse_calendar(date(2024, 7, 3), date(2024, 7, 3))

    assert calendar.sessions == (
        Session(
            day=date(2024, 7, 3),
            open_time=utc("2024-07-03T13:30"),
            close_time=utc("2024-07-03T17:00"),
        ),
    )


def test_build_nyse_calendar_of_a_weekend_has_no_sessions() -> None:
    calendar = build_nyse_calendar(date(2024, 7, 6), date(2024, 7, 7))

    assert calendar.sessions == ()
    assert calendar.coverage_start == utc("2024-07-06T04:00")
    assert calendar.coverage_end == utc("2024-07-08T04:00")


def test_build_nyse_calendar_at_the_supported_edges() -> None:
    first = build_nyse_calendar(date(2000, 1, 1), date(2000, 1, 3))
    last = build_nyse_calendar(date(2099, 12, 31), date(2099, 12, 31))

    assert [session.day for session in first.sessions] == [date(2000, 1, 3)]
    assert [session.day for session in last.sessions] == [date(2099, 12, 31)]


def test_build_nyse_calendar_is_deterministic() -> None:
    first = build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))
    second = build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))

    assert first == second
    assert hash(first) == hash(second)


def test_build_nyse_calendar_is_range_independent() -> None:
    year = build_nyse_calendar(date(2024, 1, 1), date(2024, 12, 31))

    assert year.sessions == tuple(session for session in NYSE.sessions if session.day.year == 2024)


class _DateSubclass(date):
    pass


def test_build_nyse_calendar_stores_plain_dates() -> None:
    calendar = build_nyse_calendar(_DateSubclass(2024, 7, 3), _DateSubclass(2024, 7, 3))

    assert type(calendar.first_day) is date
    assert type(calendar.last_day) is date


def test_the_builder_never_uses_the_library_registry() -> None:
    """The registry caches instances and defaults its bounds from the wall clock (D17)."""
    source = Path(nyse.__file__).read_text(encoding="utf-8")

    assert "get_calendar" not in source
    assert "exchange_calendar_xnys import XNYSExchangeCalendar" in source


def test_full_supported_range_repr_is_bounded() -> None:
    calendar = build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)
    text = repr(calendar)

    assert text == (
        "MarketCalendar(name='XNYS', first_day=2000-01-01, last_day=2099-12-31, sessions=25116)"
    )
    assert len(text) < 200


# --- T10: holidays and half days (AC12) --------------------------------------------------------

YEAR_TABLES = {
    2024: (
        252,
        "01-01, 01-15, 02-19, 03-29, 05-27, 06-19, 07-04, 09-02, 11-28, 12-25",
        "07-03, 11-29, 12-24",
    ),
    2025: (
        250,
        "01-01, 01-09, 01-20, 02-17, 04-18, 05-26, 06-19, 07-04, 09-01, 11-27, 12-25",
        "07-03, 11-28, 12-24",
    ),
    2026: (
        251,
        "01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25",
        "11-27, 12-24",
    ),
}


def _days(year: int, text: str) -> set[date]:
    return {date.fromisoformat(f"{year}-{item.strip()}") for item in text.split(",")}


@pytest.mark.parametrize("year", sorted(YEAR_TABLES))
def test_year_table_of_holidays_and_half_days(year: int) -> None:
    count, closed_weekdays, half_days = YEAR_TABLES[year]
    weekdays_without_session: set[date] = set()
    early_closes: set[date] = set()
    sessions = 0
    day = date(year, 1, 1)
    while day.year == year:
        session = NYSE.session_bounds(day)
        if session is None:
            if day.weekday() < 5:
                weekdays_without_session.add(day)
        else:
            sessions += 1
            assert day.weekday() < 5
            assert session.open_time.astimezone(NEW_YORK).time() == time(9, 30)
            close = session.close_time.astimezone(NEW_YORK).time()
            assert close in {time(13, 0), time(16, 0)}
            if close == time(13, 0):
                early_closes.add(day)
        day += timedelta(days=1)

    assert sessions == count
    assert weekdays_without_session == _days(year, closed_weekdays)
    assert early_closes == _days(year, half_days)


def test_the_2025_national_day_of_mourning_is_closed() -> None:
    assert NYSE.session_bounds(date(2025, 1, 9)) is None
    assert NYSE.session_bounds(date(2025, 1, 8)) == Session(
        day=date(2025, 1, 8), open_time=utc("2025-01-08T14:30"), close_time=utc("2025-01-08T21:00")
    )
    assert NYSE.session_bounds(date(2025, 1, 10)) == Session(
        day=date(2025, 1, 10),
        open_time=utc("2025-01-10T14:30"),
        close_time=utc("2025-01-10T21:00"),
    )


def test_new_years_day_on_a_saturday_is_not_observed_on_friday() -> None:
    assert NYSE.session_bounds(date(2021, 12, 31)) == Session(
        day=date(2021, 12, 31),
        open_time=utc("2021-12-31T14:30"),
        close_time=utc("2021-12-31T21:00"),
    )


@pytest.mark.parametrize(
    "day",
    [
        date(2021, 12, 24),
        date(2027, 12, 24),
        date(2022, 6, 20),
        date(2027, 6, 18),
        date(2027, 7, 5),
    ],
)
def test_observed_holidays_are_closed(day: date) -> None:
    assert NYSE.session_bounds(day) is None


# --- T11: daylight saving time (AC13) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "offset_hours", "opens", "closes", "daily_label"),
    [
        ("2024-03-08", -5, "14:30", "21:00", "2024-03-08T05:00"),
        ("2024-03-11", -4, "13:30", "20:00", "2024-03-11T04:00"),
        ("2024-11-01", -4, "13:30", "20:00", "2024-11-01T04:00"),
        ("2024-11-04", -5, "14:30", "21:00", "2024-11-04T05:00"),
        ("2025-03-07", -5, "14:30", "21:00", "2025-03-07T05:00"),
        ("2025-03-10", -4, "13:30", "20:00", "2025-03-10T04:00"),
        ("2025-10-31", -4, "13:30", "20:00", "2025-10-31T04:00"),
        ("2025-11-03", -5, "14:30", "21:00", "2025-11-03T05:00"),
    ],
)
def test_sessions_around_each_dst_change(
    day: str, offset_hours: int, opens: str, closes: str, daily_label: str
) -> None:
    session = NYSE.session_bounds(date.fromisoformat(day))

    assert session is not None
    assert session.open_time == utc(f"{day}T{opens}")
    assert session.close_time == utc(f"{day}T{closes}")
    assert session.open_time.astimezone(NEW_YORK).utcoffset() == timedelta(hours=offset_hours)
    daily = NYSE.candle_slot(D1, utc(daily_label))
    assert daily == CandleSlot(
        timeframe=D1,
        session_day=date.fromisoformat(day),
        label=utc(daily_label),
        open_time=utc(f"{day}T{opens}"),
        close_time=utc(f"{day}T{closes}"),
    )


@pytest.mark.parametrize(
    ("day", "opens", "closes"),
    [
        ("2024-07-03", "13:30", "17:00"),
        ("2025-07-03", "13:30", "17:00"),
        ("2024-11-29", "14:30", "18:00"),
        ("2024-12-24", "14:30", "18:00"),
        ("2025-11-28", "14:30", "18:00"),
        ("2025-12-24", "14:30", "18:00"),
        ("2026-11-27", "14:30", "18:00"),
        ("2026-12-24", "14:30", "18:00"),
    ],
)
def test_half_days_in_utc(day: str, opens: str, closes: str) -> None:
    assert NYSE.session_bounds(date.fromisoformat(day)) == Session(
        day=date.fromisoformat(day),
        open_time=utc(f"{day}T{opens}"),
        close_time=utc(f"{day}T{closes}"),
    )


def test_the_wall_clock_grid_is_unchanged_across_a_dst_change() -> None:
    friday = NYSE.candle_slots(H1, utc("2024-03-08T05:00"), utc("2024-03-09T05:00"))
    monday = NYSE.candle_slots(H1, utc("2024-03-11T04:00"), utc("2024-03-12T04:00"))
    wall = [time(9, 30), time(10, 30), time(11, 30), time(12, 30), time(13, 30), time(14, 30)]
    wall.append(time(15, 30))

    assert [candle.label.astimezone(NEW_YORK).time() for candle in friday] == wall
    assert [candle.label.astimezone(NEW_YORK).time() for candle in monday] == wall
    assert [candle.label for candle in friday] == utc_times(
        "2024-03-08", "14:30, 15:30, 16:30, 17:30, 18:30, 19:30, 20:30"
    )
    assert [candle.label for candle in monday] == utc_times(
        "2024-03-11", "13:30, 14:30, 15:30, 16:30, 17:30, 18:30, 19:30"
    )


@pytest.mark.parametrize("fold", [0, 1])
def test_ambiguous_wall_time_in_the_fall_back_fold(fold: int) -> None:
    ambiguous = datetime(2024, 11, 3, 1, 30, tzinfo=NEW_YORK, fold=fold)

    assert NYSE.is_open(ambiguous) is False
    assert NYSE.next_candle_close(H1, ambiguous) == utc("2024-11-04T15:30")
    assert NYSE.closed_candles(D1, ambiguous, 1)[-1].session_day == date(2024, 11, 1)


@pytest.mark.parametrize("fold", [0, 1])
def test_nonexistent_wall_time_in_the_spring_gap_is_closed(fold: int) -> None:
    assert NYSE.is_open(datetime(2024, 3, 10, 2, 30, tzinfo=NEW_YORK, fold=fold)) is False


def test_calendar_close_is_not_the_nominal_close() -> None:
    daily = NYSE.candle_slot(D1, utc("2024-03-08T05:00"))

    assert Timeframe.D1.nominal_close(daily.label) == utc("2024-03-09T05:00")
    assert daily.close_time == utc("2024-03-08T21:00")


# --- T11: is_open, session_bounds and coverage (AC4, AC5) --------------------------------------


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2024-07-03T13:29:59.999999", False),
        ("2024-07-03T13:30", True),
        ("2024-07-03T16:59:59.999999", True),
        ("2024-07-03T17:00", False),
        ("2024-07-04T04:00", False),
        ("2024-07-04T13:30", False),
        ("2024-07-04T15:00", False),
        ("2024-07-04T19:59:59.999999", False),
        ("2024-07-05T03:59:59.999999", False),
        ("2024-07-06T04:00", False),
        ("2024-07-06T14:30", False),
        ("2024-07-06T18:00", False),
        ("2024-07-07T03:59:59.999999", False),
    ],
)
def test_is_open_around_the_2024_independence_day(instant: str, expected: bool) -> None:
    assert NYSE.is_open(utc(instant)) is expected


@pytest.mark.parametrize("day", [date(2020, 12, 31), date(2028, 1, 1)])
def test_session_bounds_outside_the_nyse_test_span_raises(day: date) -> None:
    with pytest.raises(CalendarRangeError) as caught:
        NYSE.session_bounds(day)

    assert (caught.value.calendar, caught.value.first_day, caught.value.last_day) == (
        "XNYS",
        date(2021, 1, 1),
        date(2027, 12, 31),
    )


# --- T11: next_candle_close and closed_candles (AC8, AC9) --------------------------------------


@pytest.mark.parametrize(
    ("now", "h1", "h4", "d1"),
    [
        ("2024-01-02T14:30", "2024-01-02T15:30", "2024-01-02T18:30", "2024-01-02T21:00"),
        ("2024-01-02T15:30", "2024-01-02T16:30", "2024-01-02T18:30", "2024-01-02T21:00"),
        ("2024-01-06T12:00", "2024-01-08T15:30", "2024-01-08T18:30", "2024-01-08T21:00"),
        (
            "2024-03-08T20:59:59.999999",
            "2024-03-08T21:00",
            "2024-03-08T21:00",
            "2024-03-08T21:00",
        ),
        ("2024-03-08T21:00", "2024-03-11T14:30", "2024-03-11T17:30", "2024-03-11T20:00"),
        ("2024-03-28T20:00", "2024-04-01T14:30", "2024-04-01T17:30", "2024-04-01T20:00"),
        (
            "2024-07-03T16:59:59.999999",
            "2024-07-03T17:00",
            "2024-07-03T17:00",
            "2024-07-03T17:00",
        ),
        ("2024-07-03T17:00", "2024-07-05T14:30", "2024-07-05T17:30", "2024-07-05T20:00"),
        (
            "2024-11-01T19:59:59.999999",
            "2024-11-01T20:00",
            "2024-11-01T20:00",
            "2024-11-01T20:00",
        ),
        ("2024-12-24T18:00", "2024-12-26T15:30", "2024-12-26T18:30", "2024-12-26T21:00"),
        ("2025-01-08T21:00", "2025-01-10T15:30", "2025-01-10T18:30", "2025-01-10T21:00"),
    ],
)
def test_next_candle_close_golden_table(now: str, h1: str, h4: str, d1: str) -> None:
    assert NYSE.next_candle_close(H1, utc(now)) == utc(h1)
    assert NYSE.next_candle_close(H4, utc(now)) == utc(h4)
    assert NYSE.next_candle_close(D1, utc(now)) == utc(d1)


@pytest.mark.parametrize(
    ("timeframe", "now", "count", "expected"),
    [
        (
            H1,
            "2024-07-05T14:00",
            3,
            [
                ("2024-07-03T14:30", "2024-07-03T15:30"),
                ("2024-07-03T15:30", "2024-07-03T16:30"),
                ("2024-07-03T16:30", "2024-07-03T17:00"),
            ],
        ),
        (H1, "2024-07-05T14:30", 1, [("2024-07-05T13:30", "2024-07-05T14:30")]),
        (H4, "2024-07-05T14:00", 1, [("2024-07-03T13:30", "2024-07-03T17:00")]),
        (
            D1,
            "2024-07-05T14:00",
            2,
            [("2024-07-02T04:00", "2024-07-02T20:00"), ("2024-07-03T04:00", "2024-07-03T17:00")],
        ),
        (D1, "2024-03-11T19:59:59.999999", 1, [("2024-03-08T05:00", "2024-03-08T21:00")]),
        (D1, "2024-03-11T20:00", 1, [("2024-03-11T04:00", "2024-03-11T20:00")]),
    ],
)
def test_closed_candles_golden_rows(
    timeframe: Timeframe, now: str, count: int, expected: list[tuple[str, str]]
) -> None:
    result = NYSE.closed_candles(timeframe, utc(now), count)

    assert [(candle.label, candle.close_time) for candle in result] == [
        (utc(label), utc(close)) for label, close in expected
    ]


# --- T11: grid by day and label kinds (AC6, AC7) -----------------------------------------------

GRID_BY_DAY = {
    "2024-01-02": (
        "14:30-15:30, 15:30-16:30, 16:30-17:30, 17:30-18:30, 18:30-19:30, 19:30-20:30, 20:30-21:00",
        "14:30-18:30, 18:30-21:00",
        ("05:00", "14:30", "21:00"),
    ),
    "2024-03-08": (
        "14:30-15:30, 15:30-16:30, 16:30-17:30, 17:30-18:30, 18:30-19:30, 19:30-20:30, 20:30-21:00",
        "14:30-18:30, 18:30-21:00",
        ("05:00", "14:30", "21:00"),
    ),
    "2024-03-11": (
        "13:30-14:30, 14:30-15:30, 15:30-16:30, 16:30-17:30, 17:30-18:30, 18:30-19:30, 19:30-20:00",
        "13:30-17:30, 17:30-20:00",
        ("04:00", "13:30", "20:00"),
    ),
    "2024-07-03": (
        "13:30-14:30, 14:30-15:30, 15:30-16:30, 16:30-17:00",
        "13:30-17:00",
        ("04:00", "13:30", "17:00"),
    ),
    "2024-11-29": (
        "14:30-15:30, 15:30-16:30, 16:30-17:30, 17:30-18:00",
        "14:30-18:00",
        ("05:00", "14:30", "18:00"),
    ),
}


def _intraday(timeframe: Timeframe, day: str, text: str) -> tuple[CandleSlot, ...]:
    slots = []
    for item in text.split(","):
        opens, closes = item.strip().split("-")
        slots.append(
            CandleSlot(
                timeframe=timeframe,
                session_day=date.fromisoformat(day),
                label=utc(f"{day}T{opens}"),
                open_time=utc(f"{day}T{opens}"),
                close_time=utc(f"{day}T{closes}"),
            )
        )
    return tuple(slots)


@pytest.mark.parametrize("day", sorted(GRID_BY_DAY))
def test_grid_by_day_golden_table(day: str) -> None:
    hourly, four_hourly, (label, opens, closes) = GRID_BY_DAY[day]
    day_start = utc(f"{day}T{label}")
    next_day_start = day_start + timedelta(days=1)  # none of these days precedes a DST change

    assert NYSE.candle_slots(H1, day_start, next_day_start) == _intraday(H1, day, hourly)
    assert NYSE.candle_slots(H4, day_start, next_day_start) == _intraday(H4, day, four_hourly)
    assert NYSE.candle_slots(D1, day_start, next_day_start) == (
        CandleSlot(
            timeframe=D1,
            session_day=date.fromisoformat(day),
            label=day_start,
            open_time=utc(f"{day}T{opens}"),
            close_time=utc(f"{day}T{closes}"),
        ),
    )


@pytest.mark.parametrize(("timeframe", "count"), [(H1, 1755), (H4, 501), (D1, 252)])
def test_slot_counts_over_the_labels_of_2024(timeframe: Timeframe, count: int) -> None:
    slots = NYSE.candle_slots(timeframe, utc("2024-01-01T05:00"), utc("2025-01-01T05:00"))

    assert len(slots) == count
    assert all(earlier.label < later.label for earlier, later in pairwise(slots))


@pytest.mark.parametrize(
    ("timeframe", "label", "kind"),
    [
        (H1, "2024-07-03T14:00", CandleLabelErrorKind.OFF_GRID),
        (H4, "2024-01-02T16:30", CandleLabelErrorKind.OFF_GRID),
        (H1, "2024-01-02T13:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-01-02T21:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-07-03T17:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-07-04T14:30", CandleLabelErrorKind.NOT_A_SESSION),
        (D1, "2024-07-03T13:30", CandleLabelErrorKind.OFF_GRID),
        (D1, "2024-07-03T00:00", CandleLabelErrorKind.OFF_GRID),
        (D1, "2024-07-04T04:00", CandleLabelErrorKind.NOT_A_SESSION),
        (D1, "2024-07-06T04:00", CandleLabelErrorKind.NOT_A_SESSION),
    ],
)
def test_label_kinds_golden_table(
    timeframe: Timeframe, label: str, kind: CandleLabelErrorKind
) -> None:
    with pytest.raises(CandleLabelError) as caught:
        NYSE.candle_slot(timeframe, utc(label))

    assert caught.value.kind is kind


def test_candle_slot_round_trips_every_2024_slot() -> None:
    for timeframe in Timeframe:
        for candle in NYSE.candle_slots(timeframe, year_start(2024), year_start(2025)):
            assert NYSE.candle_slot(timeframe, candle.label) == candle


def test_nyse_values_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        NYSE.name = "OTHER"
    assert NYSE.sessions[0].open_time.tzinfo is UTC

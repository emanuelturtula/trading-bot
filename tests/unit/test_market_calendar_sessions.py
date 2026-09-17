"""Tests of the calendar model and its queries on the hand-built toy calendar (spec 009, T1-T8).

The toy calendar (``tests.fixtures.calendars.toy_calendar``) covers 2024-03-06 to 2024-03-14 in
New York time: a regular day, a half day, the DST weekend, a weekday holiday and an ad hoc
late-open day. Every expected value is a literal; nothing is recomputed with the code under
test (spec 009, Test plan).
"""

from __future__ import annotations

import dataclasses
import pickle
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from itertools import pairwise
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.fixtures.calendars import NEW_YORK, toy_calendar, utc, wall_session
from trading_bot.domain.market_calendar import sessions as sessions_module
from trading_bot.domain.market_calendar.sessions import (
    CalendarError,
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
TOY = toy_calendar()
NANOSECOND_TIMESTAMP = pd.Timestamp("2024-03-07 14:30:00.000000001", tz="UTC")


def calendar_with(**overrides: object) -> MarketCalendar:
    """A calendar built from the toy arguments with some of them replaced."""
    arguments: dict[str, object] = {
        "name": "TOY",
        "timezone": NEW_YORK,
        "first_day": date(2024, 3, 6),
        "last_day": date(2024, 3, 14),
        "sessions": TOY.sessions,
    }
    arguments.update(overrides)
    return MarketCalendar(**arguments)


def slot(timeframe: Timeframe, label: str, close: str, *, opens: str | None = None) -> CandleSlot:
    """The expected slot: the open defaults to the label and the session day to its UTC date."""
    label_time = utc(label)
    return CandleSlot(
        timeframe=timeframe,
        session_day=label_time.date(),
        label=label_time,
        open_time=utc(opens) if opens is not None else label_time,
        close_time=utc(close),
    )


# --- T1: Session (AC1) -------------------------------------------------------------------------


def test_session_normalizes_both_instants_to_stdlib_utc() -> None:
    session = Session(
        day=date(2024, 3, 7),
        open_time=datetime(2024, 3, 7, 9, 30, tzinfo=NEW_YORK),
        close_time=pd.Timestamp("2024-03-07 16:00", tz="America/New_York"),
    )

    assert session.day == date(2024, 3, 7)
    assert session.open_time == datetime(2024, 3, 7, 14, 30, tzinfo=UTC)
    assert session.close_time == datetime(2024, 3, 7, 21, 0, tzinfo=UTC)
    assert type(session.open_time) is datetime
    assert type(session.close_time) is datetime
    assert session.open_time.tzinfo is UTC
    assert session.close_time.tzinfo is UTC


def test_session_is_a_frozen_slotted_keyword_only_dataclass() -> None:
    parameters = dataclasses.fields(Session)

    assert [field.name for field in parameters] == ["day", "open_time", "close_time"]
    assert all(field.kw_only for field in parameters)
    assert not hasattr(TOY.sessions[0], "__dict__")
    with pytest.raises(TypeError):
        Session(date(2024, 3, 7), utc("2024-03-07T14:30"), utc("2024-03-07T21:00"))


class _DateSubclass(date):
    pass


def test_session_day_of_a_date_subclass_is_stored_as_a_plain_date() -> None:
    session = Session(
        day=_DateSubclass(2024, 3, 7),
        open_time=utc("2024-03-07T14:30"),
        close_time=utc("2024-03-07T21:00"),
    )

    assert type(session.day) is date
    assert session == TOY.sessions[0]


@pytest.mark.parametrize(
    "day",
    [
        datetime(2024, 3, 7),
        datetime(2024, 3, 7, tzinfo=UTC),
        pd.Timestamp("2024-03-07"),
        "2024-03-07",
        None,
    ],
    ids=["naive-datetime", "aware-datetime", "timestamp", "str", "none"],
)
def test_session_day_must_be_a_date_that_is_not_a_datetime(day: object) -> None:
    with pytest.raises(TypeError, match="day"):
        Session(
            day=day,
            open_time=utc("2024-03-07T14:30"),
            close_time=utc("2024-03-07T21:00"),
        )


@pytest.mark.parametrize("field_name", ["open_time", "close_time"])
@pytest.mark.parametrize(
    ("value", "error"),
    [
        (datetime(2024, 3, 7, 15, 0), ValueError),
        (pd.NaT, ValueError),
        (pd.Timestamp("2024-03-07 15:00:00.000000001", tz="UTC"), ValueError),
        ("2024-03-07T15:00:00+00:00", TypeError),
        (date(2024, 3, 7), TypeError),
        (1_709_823_600, TypeError),
    ],
    ids=["naive", "nat", "nanoseconds", "str", "date", "int"],
)
def test_session_instants_follow_the_to_utc_rules(
    field_name: str, value: object, error: type[Exception]
) -> None:
    arguments: dict[str, object] = {
        "day": date(2024, 3, 7),
        "open_time": utc("2024-03-07T14:30"),
        "close_time": utc("2024-03-07T21:00"),
    }
    arguments[field_name] = value

    with pytest.raises(error):
        Session(**arguments)


@pytest.mark.parametrize(
    ("opens", "closes"),
    [("2024-03-07T21:00", "2024-03-07T21:00"), ("2024-03-07T21:00", "2024-03-07T14:30")],
    ids=["equal", "reversed"],
)
def test_session_must_open_before_it_closes(opens: str, closes: str) -> None:
    with pytest.raises(ValueError, match="2024-03-07"):
        Session(day=date(2024, 3, 7), open_time=utc(opens), close_time=utc(closes))


# --- T2: MarketCalendar construction (AC2) -----------------------------------------------------


def test_toy_calendar_keeps_its_arguments() -> None:
    assert TOY.name == "TOY"
    assert TOY.timezone is NEW_YORK
    assert TOY.first_day == date(2024, 3, 6)
    assert TOY.last_day == date(2024, 3, 14)
    assert TOY.sessions == (
        Session(
            day=date(2024, 3, 7),
            open_time=utc("2024-03-07T14:30"),
            close_time=utc("2024-03-07T21:00"),
        ),
        Session(
            day=date(2024, 3, 8),
            open_time=utc("2024-03-08T14:30"),
            close_time=utc("2024-03-08T18:00"),
        ),
        Session(
            day=date(2024, 3, 11),
            open_time=utc("2024-03-11T13:30"),
            close_time=utc("2024-03-11T20:00"),
        ),
        Session(
            day=date(2024, 3, 13),
            open_time=utc("2024-03-13T14:00"),
            close_time=utc("2024-03-13T19:00"),
        ),
    )


def test_market_calendar_is_a_frozen_slotted_keyword_only_dataclass() -> None:
    init_fields = [field for field in dataclasses.fields(MarketCalendar) if field.init]

    assert [field.name for field in init_fields] == [
        "name",
        "timezone",
        "first_day",
        "last_day",
        "sessions",
    ]
    assert all(field.kw_only for field in init_fields)
    assert not hasattr(TOY, "__dict__")


def test_coverage_spans_whole_exchange_days_across_the_dst_change() -> None:
    assert TOY.coverage_start == datetime(2024, 3, 6, 5, 0, tzinfo=UTC)
    assert TOY.coverage_end == datetime(2024, 3, 15, 4, 0, tzinfo=UTC)
    assert type(TOY.coverage_start) is datetime
    assert TOY.coverage_start.tzinfo is UTC
    assert TOY.coverage_end.tzinfo is UTC


def test_coverage_of_a_fixed_offset_calendar() -> None:
    calendar = MarketCalendar(
        name="FIXED",
        timezone=timezone(timedelta(hours=9)),
        first_day=date(2024, 1, 1),
        last_day=date(2024, 1, 1),
        sessions=(),
    )

    assert calendar.coverage_start == datetime(2023, 12, 31, 15, 0, tzinfo=UTC)
    assert calendar.coverage_end == datetime(2024, 1, 1, 15, 0, tzinfo=UTC)


def test_an_empty_session_tuple_is_valid() -> None:
    calendar = calendar_with(sessions=())

    assert calendar.sessions == ()
    assert calendar.session_bounds(date(2024, 3, 7)) is None


@pytest.mark.parametrize("name", ["A", "A" * 16, "XNYS", "X.N-Y_S", "!~"])
def test_calendar_name_accepts_short_printable_ascii_tokens(name: str) -> None:
    assert calendar_with(name=name).name == name


class _Name(str):
    __slots__ = ()


def test_calendar_name_of_a_str_subclass_is_stored_as_a_plain_str() -> None:
    calendar = calendar_with(name=_Name("TOY"))

    assert type(calendar.name) is str
    assert calendar == TOY


@pytest.mark.parametrize("name", ["", "A" * 17, "X NYS", "XNYS\n", " XNYS", "É", "X\x00"], ids=repr)
def test_calendar_name_rejects_other_text(name: str) -> None:
    with pytest.raises(ValueError, match="name") as caught:
        calendar_with(name=name)

    assert name not in str(caught.value) or name == ""


@pytest.mark.parametrize("name", [None, 1, b"XNYS"], ids=repr)
def test_calendar_name_must_be_a_str(name: object) -> None:
    with pytest.raises(TypeError, match="name"):
        calendar_with(name=name)


@pytest.mark.parametrize("tz", ["America/New_York", None, UTC.utcoffset(None)], ids=repr)
def test_calendar_timezone_must_be_a_tzinfo(tz: object) -> None:
    with pytest.raises(TypeError, match="timezone"):
        calendar_with(timezone=tz)


@pytest.mark.parametrize("field_name", ["first_day", "last_day"])
@pytest.mark.parametrize(
    "value",
    [datetime(2024, 3, 6), pd.Timestamp("2024-03-06"), "2024-03-06", None],
    ids=["datetime", "timestamp", "str", "none"],
)
def test_calendar_days_must_be_dates_that_are_not_datetimes(field_name: str, value: object) -> None:
    with pytest.raises(TypeError, match=field_name):
        calendar_with(**{field_name: value})


@pytest.mark.parametrize(
    ("first_day", "last_day"),
    [
        (date(2024, 3, 15), date(2024, 3, 14)),
        (date.min, date(2024, 3, 14)),
        (date(2024, 3, 6), date.max),
    ],
    ids=["reversed", "date-min", "date-max"],
)
def test_calendar_days_must_be_ordered_and_inside_the_date_range(
    first_day: date, last_day: date
) -> None:
    with pytest.raises(ValueError, match="first_day"):
        calendar_with(first_day=first_day, last_day=last_day, sessions=())


def test_calendar_days_may_be_one_day_next_to_the_date_limits_in_extreme_offsets() -> None:
    low = calendar_with(
        timezone=timezone(timedelta(hours=23, minutes=59)),
        first_day=date(1, 1, 2),
        last_day=date(1, 1, 2),
        sessions=(),
    )
    high = calendar_with(
        timezone=timezone(-timedelta(hours=23, minutes=59)),
        first_day=date(9999, 12, 30),
        last_day=date(9999, 12, 30),
        sessions=(),
    )

    assert low.coverage_start == datetime(1, 1, 1, 0, 1, tzinfo=UTC)
    assert high.coverage_end == datetime(9999, 12, 31, 23, 59, tzinfo=UTC)


def test_calendar_first_day_may_equal_last_day() -> None:
    calendar = calendar_with(first_day=date(2024, 3, 7), last_day=date(2024, 3, 7), sessions=())

    assert calendar.coverage_start == datetime(2024, 3, 7, 5, 0, tzinfo=UTC)
    assert calendar.coverage_end == datetime(2024, 3, 8, 5, 0, tzinfo=UTC)


def test_calendar_sessions_must_be_a_tuple() -> None:
    with pytest.raises(TypeError, match="sessions"):
        calendar_with(sessions=list(TOY.sessions))


@pytest.mark.parametrize(
    "item",
    [None, (date(2024, 3, 7), utc("2024-03-07T14:30"), utc("2024-03-07T21:00")), "session"],
    ids=["none", "tuple", "str"],
)
def test_calendar_sessions_must_all_be_sessions(item: object) -> None:
    with pytest.raises(TypeError, match="Session"):
        calendar_with(sessions=(*TOY.sessions[:2], item))


@pytest.mark.parametrize(
    "sessions",
    [
        (TOY.sessions[0], TOY.sessions[0]),
        (TOY.sessions[1], TOY.sessions[0]),
        (TOY.sessions[0], TOY.sessions[2], TOY.sessions[1]),
    ],
    ids=["duplicate", "unsorted-pair", "unsorted-triple"],
)
def test_calendar_session_days_must_be_strictly_increasing(sessions: tuple[Session, ...]) -> None:
    with pytest.raises(ValueError, match="increasing"):
        calendar_with(sessions=sessions)


@pytest.mark.parametrize(
    "session",
    [
        wall_session(date(2024, 3, 5), time(9, 30), time(16, 0)),
        wall_session(date(2024, 3, 15), time(9, 30), time(16, 0)),
    ],
    ids=["before-first-day", "after-last-day"],
)
def test_calendar_session_days_must_be_inside_the_span(session: Session) -> None:
    with pytest.raises(ValueError, match="outside"):
        calendar_with(sessions=(session,))


@pytest.mark.parametrize(
    "session",
    [
        Session(
            day=date(2024, 3, 7),
            open_time=datetime(2024, 3, 7, 20, 0, tzinfo=NEW_YORK),
            close_time=datetime(2024, 3, 8, 2, 0, tzinfo=NEW_YORK),
        ),
        Session(
            day=date(2024, 3, 7),
            open_time=datetime(2024, 3, 6, 23, 0, tzinfo=NEW_YORK),
            close_time=datetime(2024, 3, 7, 10, 0, tzinfo=NEW_YORK),
        ),
        Session(
            day=date(2024, 3, 7),
            open_time=utc("2024-03-07T02:00"),
            close_time=utc("2024-03-07T10:00"),
        ),
        Session(
            day=date(2024, 3, 7),
            open_time=datetime(2024, 3, 7, 9, 30, tzinfo=NEW_YORK),
            close_time=datetime(2024, 3, 8, 0, 0, tzinfo=NEW_YORK),
        ),
    ],
    ids=["closes-next-day", "opens-previous-day", "utc-day-not-exchange-day", "closes-at-midnight"],
)
def test_calendar_sessions_must_open_and_close_on_their_exchange_day(session: Session) -> None:
    with pytest.raises(ValueError, match="2024-03-07"):
        calendar_with(sessions=(session,))


# --- The toy grid, written out (Design 4.2) ----------------------------------------------------

TOY_H1 = (
    slot(H1, "2024-03-07T14:30", "2024-03-07T15:30"),
    slot(H1, "2024-03-07T15:30", "2024-03-07T16:30"),
    slot(H1, "2024-03-07T16:30", "2024-03-07T17:30"),
    slot(H1, "2024-03-07T17:30", "2024-03-07T18:30"),
    slot(H1, "2024-03-07T18:30", "2024-03-07T19:30"),
    slot(H1, "2024-03-07T19:30", "2024-03-07T20:30"),
    slot(H1, "2024-03-07T20:30", "2024-03-07T21:00"),
    slot(H1, "2024-03-08T14:30", "2024-03-08T15:30"),
    slot(H1, "2024-03-08T15:30", "2024-03-08T16:30"),
    slot(H1, "2024-03-08T16:30", "2024-03-08T17:30"),
    slot(H1, "2024-03-08T17:30", "2024-03-08T18:00"),
    slot(H1, "2024-03-11T13:30", "2024-03-11T14:30"),
    slot(H1, "2024-03-11T14:30", "2024-03-11T15:30"),
    slot(H1, "2024-03-11T15:30", "2024-03-11T16:30"),
    slot(H1, "2024-03-11T16:30", "2024-03-11T17:30"),
    slot(H1, "2024-03-11T17:30", "2024-03-11T18:30"),
    slot(H1, "2024-03-11T18:30", "2024-03-11T19:30"),
    slot(H1, "2024-03-11T19:30", "2024-03-11T20:00"),
    slot(H1, "2024-03-13T14:00", "2024-03-13T15:00"),
    slot(H1, "2024-03-13T15:00", "2024-03-13T16:00"),
    slot(H1, "2024-03-13T16:00", "2024-03-13T17:00"),
    slot(H1, "2024-03-13T17:00", "2024-03-13T18:00"),
    slot(H1, "2024-03-13T18:00", "2024-03-13T19:00"),
)
TOY_H4 = (
    slot(H4, "2024-03-07T14:30", "2024-03-07T18:30"),
    slot(H4, "2024-03-07T18:30", "2024-03-07T21:00"),
    slot(H4, "2024-03-08T14:30", "2024-03-08T18:00"),
    slot(H4, "2024-03-11T13:30", "2024-03-11T17:30"),
    slot(H4, "2024-03-11T17:30", "2024-03-11T20:00"),
    slot(H4, "2024-03-13T14:00", "2024-03-13T18:00"),
    slot(H4, "2024-03-13T18:00", "2024-03-13T19:00"),
)
TOY_D1 = (
    slot(D1, "2024-03-07T05:00", "2024-03-07T21:00", opens="2024-03-07T14:30"),
    slot(D1, "2024-03-08T05:00", "2024-03-08T18:00", opens="2024-03-08T14:30"),
    slot(D1, "2024-03-11T04:00", "2024-03-11T20:00", opens="2024-03-11T13:30"),
    slot(D1, "2024-03-13T04:00", "2024-03-13T19:00", opens="2024-03-13T14:00"),
)
TOY_GRID = {H1: TOY_H1, H4: TOY_H4, D1: TOY_D1}


def all_boundaries() -> list[datetime]:
    """Every slot label, open and close of the toy grid, one microsecond around each, and the
    coverage edges: the instants where an off-by-one in a query would show."""
    step = timedelta(microseconds=1)
    instants = {TOY.coverage_start, TOY.coverage_end - step}
    for grid in TOY_GRID.values():
        for candle in grid:
            for instant in (candle.label, candle.open_time, candle.close_time):
                instants.update({instant - step, instant, instant + step})
    return sorted(t for t in instants if TOY.coverage_start <= t < TOY.coverage_end)


def outcome(call: Callable[..., object], *arguments: object) -> object:
    """The result of ``call(*arguments)``, or the type and message of the calendar error."""
    try:
        return call(*arguments)
    except CalendarError as error:
        return (type(error), str(error))


# --- T3: argument contract and coverage (AC3) --------------------------------------------------

TIMEFRAME_QUERIES: dict[str, Callable[[object], object]] = {
    "next_candle_close": lambda tf: TOY.next_candle_close(tf, utc("2024-03-07T15:00")),
    "candle_slot": lambda tf: TOY.candle_slot(tf, utc("2024-03-07T14:30")),
    "candle_slots": lambda tf: TOY.candle_slots(
        tf, utc("2024-03-07T00:00"), utc("2024-03-08T00:00")
    ),
    "closed_candles": lambda tf: TOY.closed_candles(tf, utc("2024-03-08T00:00"), 1),
}


@pytest.mark.parametrize("query", TIMEFRAME_QUERIES.values(), ids=TIMEFRAME_QUERIES.keys())
@pytest.mark.parametrize("timeframe", ["1h", None, 1], ids=repr)
def test_queries_accept_only_timeframe_members(
    query: Callable[[object], object], timeframe: object
) -> None:
    with pytest.raises(TypeError, match="Timeframe"):
        query(timeframe)


INSTANT_QUERIES: dict[str, Callable[[object], object]] = {
    "is_open": lambda t: TOY.is_open(t),
    "next_candle_close": lambda t: TOY.next_candle_close(H1, t),
    "candle_slot": lambda t: TOY.candle_slot(H1, t),
    "candle_slots-start": lambda t: TOY.candle_slots(H1, t, TOY.coverage_end),
    "candle_slots-end": lambda t: TOY.candle_slots(H1, TOY.coverage_start, t),
    "closed_candles": lambda t: TOY.closed_candles(H1, t, 1),
}


@pytest.mark.parametrize("query", INSTANT_QUERIES.values(), ids=INSTANT_QUERIES.keys())
@pytest.mark.parametrize(
    ("value", "error"),
    [
        (datetime(2024, 3, 7, 15, 0), ValueError),
        (pd.NaT, ValueError),
        (pd.Timestamp("2024-03-07 15:00:00.000000001", tz="UTC"), ValueError),
        ("2024-03-07T15:00:00+00:00", TypeError),
        (date(2024, 3, 7), TypeError),
        (1_709_823_600.0, TypeError),
    ],
    ids=["naive", "nat", "nanoseconds", "str", "date", "float"],
)
def test_query_instants_follow_the_to_utc_rules(
    query: Callable[[object], object], value: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        query(value)


@pytest.mark.parametrize(
    "day",
    [datetime(2024, 3, 7), pd.Timestamp("2024-03-07", tz="UTC"), "2024-03-07", None],
    ids=["datetime", "timestamp", "str", "none"],
)
def test_session_bounds_accepts_only_dates_that_are_not_datetimes(day: object) -> None:
    with pytest.raises(TypeError, match="day"):
        TOY.session_bounds(day)


def test_is_open_answers_exactly_inside_the_coverage() -> None:
    assert TOY.is_open(utc("2024-03-06T05:00")) is False
    assert TOY.is_open(utc("2024-03-15T03:59:59.999999")) is False
    with pytest.raises(CalendarRangeError):
        TOY.is_open(utc("2024-03-06T04:59:59.999999"))
    with pytest.raises(CalendarRangeError):
        TOY.is_open(utc("2024-03-15T04:00"))


BEFORE_COVERAGE_QUERIES = [
    "is_open",
    "next_candle_close",
    "candle_slot",
    "closed_candles",
    "candle_slots-start",
]
AT_COVERAGE_END_QUERIES = ["is_open", "next_candle_close", "candle_slot", "closed_candles"]


@pytest.mark.parametrize(
    "query",
    [INSTANT_QUERIES[name] for name in BEFORE_COVERAGE_QUERIES],
    ids=BEFORE_COVERAGE_QUERIES,
)
def test_instants_before_the_coverage_raise_calendar_range_error(
    query: Callable[[object], object],
) -> None:
    with pytest.raises(CalendarRangeError):
        query(utc("2024-03-06T04:59:59.999999"))


@pytest.mark.parametrize(
    "query",
    [INSTANT_QUERIES[name] for name in AT_COVERAGE_END_QUERIES],
    ids=AT_COVERAGE_END_QUERIES,
)
def test_instants_at_the_coverage_end_raise_calendar_range_error(
    query: Callable[[object], object],
) -> None:
    with pytest.raises(CalendarRangeError):
        query(utc("2024-03-15T04:00"))


@pytest.mark.parametrize("day", [date(2024, 3, 5), date(2024, 3, 15), date(1, 1, 1)])
def test_days_outside_the_span_raise_calendar_range_error(day: date) -> None:
    with pytest.raises(CalendarRangeError) as caught:
        TOY.session_bounds(day)

    assert caught.value.calendar == "TOY"
    assert caught.value.first_day == date(2024, 3, 6)
    assert caught.value.last_day == date(2024, 3, 14)


def test_calendar_range_error_carries_the_calendar_and_its_days() -> None:
    with pytest.raises(CalendarRangeError) as caught:
        TOY.next_candle_close(D1, utc("2024-03-15T04:00"))

    assert caught.value.calendar == "TOY"
    assert caught.value.first_day == date(2024, 3, 6)
    assert caught.value.last_day == date(2024, 3, 14)


def test_calendar_errors_form_a_value_error_hierarchy() -> None:
    assert issubclass(CalendarError, ValueError)
    assert issubclass(CalendarRangeError, CalendarError)
    assert issubclass(CandleLabelError, CalendarError)
    assert not issubclass(CandleLabelError, CalendarRangeError)
    assert not issubclass(CalendarRangeError, CandleLabelError)
    assert [kind.value for kind in CandleLabelErrorKind] == [
        "not_a_session",
        "outside_session",
        "off_grid",
    ]


def _representations(instant: datetime) -> dict[str, datetime]:
    return {
        "stdlib-utc": instant,
        "fixed-offset": instant.astimezone(timezone(timedelta(hours=-5, minutes=-30))),
        "zoneinfo": instant.astimezone(NEW_YORK),
        "zoneinfo-other": instant.astimezone(ZoneInfo("Asia/Tokyo")),
        "timestamp-utc": pd.Timestamp(instant),
        "timestamp-new-york": pd.Timestamp(instant).tz_convert("America/New_York"),
    }


@pytest.mark.parametrize(
    "instant",
    [
        "2024-03-06T05:00",
        "2024-03-07T14:30",
        "2024-03-07T15:30",
        "2024-03-08T17:59:59.999999",
        "2024-03-09T12:00",
        "2024-03-11T13:30",
        "2024-03-13T19:00",
        "2024-03-15T03:59:59.999999",
    ],
)
def test_the_same_instant_in_any_representation_gives_equal_results(instant: str) -> None:
    def answers(t: datetime) -> list[object]:
        results: list[object] = [TOY.is_open(t)]
        for timeframe in Timeframe:
            results.append(outcome(TOY.next_candle_close, timeframe, t))
            results.append(outcome(TOY.candle_slot, timeframe, t))
            results.append(outcome(TOY.closed_candles, timeframe, t, 2))
            results.append(outcome(TOY.candle_slots, timeframe, TOY.coverage_start, t))
            results.append(outcome(TOY.candle_slots, timeframe, t, TOY.coverage_end))
        return results

    expected = answers(utc(instant))

    for name, value in _representations(utc(instant)).items():
        assert answers(value) == expected, name


# --- T4: session_bounds and is_open (AC4, AC5) -------------------------------------------------


@pytest.mark.parametrize(
    ("day", "index"),
    [(date(2024, 3, 7), 0), (date(2024, 3, 8), 1), (date(2024, 3, 11), 2), (date(2024, 3, 13), 3)],
)
def test_session_bounds_returns_the_session_of_the_day(day: date, index: int) -> None:
    assert TOY.session_bounds(day) is TOY.sessions[index]


@pytest.mark.parametrize(
    "day",
    [date(2024, 3, 6), date(2024, 3, 9), date(2024, 3, 10), date(2024, 3, 12), date(2024, 3, 14)],
)
def test_session_bounds_is_none_on_closed_days_inside_the_span(day: date) -> None:
    assert TOY.session_bounds(day) is None


def test_session_bounds_accepts_a_date_subclass() -> None:
    assert TOY.session_bounds(_DateSubclass(2024, 3, 8)) is TOY.sessions[1]


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2024-03-08T14:29:59.999999", False),
        ("2024-03-08T14:30", True),
        ("2024-03-08T17:59:59.999999", True),
        ("2024-03-08T18:00", False),
        ("2024-03-07T21:00", False),
        ("2024-03-07T20:59:59.999999", True),
        ("2024-03-09T15:00", False),
        ("2024-03-10T15:00", False),
        ("2024-03-11T13:29:59.999999", False),
        ("2024-03-11T13:30", True),
        ("2024-03-12T15:00", False),
        ("2024-03-13T13:59:59.999999", False),
        ("2024-03-13T14:00", True),
        ("2024-03-13T18:59:59.999999", True),
        ("2024-03-13T19:00", False),
        ("2024-03-14T15:00", False),
        ("2024-03-06T15:00", False),
    ],
)
def test_is_open_uses_half_open_sessions(instant: str, expected: bool) -> None:
    assert TOY.is_open(utc(instant)) is expected


# --- T5: candle grid (AC6) ---------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_candle_slots_over_the_whole_coverage_is_the_toy_grid(timeframe: Timeframe) -> None:
    result = TOY.candle_slots(timeframe, TOY.coverage_start, TOY.coverage_end)

    assert type(result) is tuple
    assert result == TOY_GRID[timeframe]


@pytest.mark.parametrize("timeframe", [H1, H4])
def test_intraday_slots_tile_each_session_exactly(timeframe: Timeframe) -> None:
    result = TOY.candle_slots(timeframe, TOY.coverage_start, TOY.coverage_end)

    for session in TOY.sessions:
        day_slots = [candle for candle in result if candle.session_day == session.day]
        assert day_slots[0].open_time == session.open_time
        assert day_slots[-1].close_time == session.close_time
        for earlier, later in pairwise(day_slots):
            assert earlier.close_time == later.open_time
        for candle in day_slots[:-1]:
            assert candle.close_time - candle.open_time == timeframe.duration
        assert day_slots[-1].close_time - day_slots[-1].open_time <= timeframe.duration
        assert all(candle.label == candle.open_time for candle in day_slots)


def test_late_open_day_grid_anchors_at_its_open() -> None:
    h1 = TOY.candle_slots(H1, utc("2024-03-13T04:00"), utc("2024-03-14T04:00"))
    h4 = TOY.candle_slots(H4, utc("2024-03-13T04:00"), utc("2024-03-14T04:00"))

    assert [candle.label for candle in h1] == [
        utc("2024-03-13T14:00"),
        utc("2024-03-13T15:00"),
        utc("2024-03-13T16:00"),
        utc("2024-03-13T17:00"),
        utc("2024-03-13T18:00"),
    ]
    assert h4 == (
        slot(H4, "2024-03-13T14:00", "2024-03-13T18:00"),
        slot(H4, "2024-03-13T18:00", "2024-03-13T19:00"),
    )


@pytest.mark.parametrize(
    ("timeframe", "start", "end", "expected"),
    [
        (H1, "2024-03-07T15:00", "2024-03-07T17:30", TOY_H1[1:3]),
        (H1, "2024-03-07T15:30", "2024-03-07T16:30", TOY_H1[1:2]),
        (H1, "2024-03-07T15:30", "2024-03-07T15:30:00.000001", TOY_H1[1:2]),
        (H1, "2024-03-07T15:30:00.000001", "2024-03-07T16:30:00.000001", TOY_H1[2:3]),
        (H1, "2024-03-07T20:30:00.000001", "2024-03-08T14:30", ()),
        (H1, "2024-03-08T17:00", "2024-03-11T14:00", TOY_H1[10:12]),
        (H4, "2024-03-08T16:00", "2024-03-11T17:30", TOY_H4[3:4]),
        (H4, "2024-03-07T14:30", "2024-03-13T18:00", TOY_H4[:6]),
        (D1, "2024-03-07T14:00", "2024-03-07T22:00", ()),
        (D1, "2024-03-07T05:00", "2024-03-07T05:00:00.000001", TOY_D1[:1]),
        (D1, "2024-03-07T05:00:00.000001", "2024-03-11T04:00", TOY_D1[1:2]),
        (D1, "2024-03-07T05:00:00.000001", "2024-03-11T04:00:00.000001", TOY_D1[1:3]),
        (D1, "2024-03-12T00:00", "2024-03-15T04:00", TOY_D1[3:]),
    ],
)
def test_candle_slots_selects_by_label(
    timeframe: Timeframe, start: str, end: str, expected: tuple[CandleSlot, ...]
) -> None:
    assert TOY.candle_slots(timeframe, utc(start), utc(end)) == expected


@pytest.mark.parametrize("timeframe", list(Timeframe))
@pytest.mark.parametrize(
    "instant", ["2024-03-06T05:00", "2024-03-07T14:30", "2024-03-13T04:00", "2024-03-15T04:00"]
)
def test_candle_slots_with_equal_bounds_is_empty(timeframe: Timeframe, instant: str) -> None:
    assert TOY.candle_slots(timeframe, utc(instant), utc(instant)) == ()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2024-03-07T14:30:00.000001", "2024-03-07T14:30"),
        ("2024-03-15T04:00:00.000001", "2024-03-06T04:59:59.999999"),
        ("2030-01-01T00:00", "2000-01-01T00:00"),
    ],
    ids=["inside", "both-outside", "far-outside"],
)
def test_candle_slots_rejects_start_after_end_before_checking_the_range(
    start: str, end: str
) -> None:
    with pytest.raises(ValueError, match="start") as caught:
        TOY.candle_slots(H1, utc(start), utc(end))

    assert not isinstance(caught.value, CalendarError)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2024-03-06T04:59:59.999999", "2024-03-07T00:00"),
        ("2024-03-07T00:00", "2024-03-15T04:00:00.000001"),
        ("2024-03-06T04:59:59.999999", "2024-03-06T04:59:59.999999"),
        ("2024-03-15T04:00:00.000001", "2024-03-15T04:00:00.000001"),
    ],
    ids=["start-before", "end-after", "equal-before", "equal-after"],
)
def test_candle_slots_bounds_must_be_inside_the_closed_coverage(start: str, end: str) -> None:
    with pytest.raises(CalendarRangeError):
        TOY.candle_slots(D1, utc(start), utc(end))


# --- T6: candle_slot (AC7) ---------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_candle_slot_round_trips_every_slot(timeframe: Timeframe) -> None:
    for candle in TOY.candle_slots(timeframe, TOY.coverage_start, TOY.coverage_end):
        assert TOY.candle_slot(candle.timeframe, candle.label) == candle
        assert TOY.candle_slot(timeframe, pd.Timestamp(candle.label).tz_convert(NEW_YORK)) == (
            candle
        )


@pytest.mark.parametrize(
    ("timeframe", "label", "kind"),
    [
        (H1, "2024-03-07T15:00", CandleLabelErrorKind.OFF_GRID),
        (H1, "2024-03-07T14:30:00.000001", CandleLabelErrorKind.OFF_GRID),
        (H4, "2024-03-07T15:30", CandleLabelErrorKind.OFF_GRID),
        (H1, "2024-03-13T14:30", CandleLabelErrorKind.OFF_GRID),
        (H1, "2024-03-07T14:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-03-07T21:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H4, "2024-03-07T22:30", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-03-08T18:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-03-13T13:30", CandleLabelErrorKind.OUTSIDE_SESSION),
        (H1, "2024-03-12T15:00", CandleLabelErrorKind.NOT_A_SESSION),
        (H1, "2024-03-09T15:00", CandleLabelErrorKind.NOT_A_SESSION),
        (H4, "2024-03-06T14:30", CandleLabelErrorKind.NOT_A_SESSION),
        (H1, "2024-03-08T03:00", CandleLabelErrorKind.OUTSIDE_SESSION),
        (D1, "2024-03-07T14:30", CandleLabelErrorKind.OFF_GRID),
        (D1, "2024-03-08T00:00", CandleLabelErrorKind.OFF_GRID),
        (D1, "2024-03-07T00:00", CandleLabelErrorKind.NOT_A_SESSION),
        (D1, "2024-03-12T04:00", CandleLabelErrorKind.NOT_A_SESSION),
        (D1, "2024-03-09T05:00", CandleLabelErrorKind.NOT_A_SESSION),
        (D1, "2024-03-14T04:00", CandleLabelErrorKind.NOT_A_SESSION),
    ],
)
def test_candle_slot_classifies_other_labels(
    timeframe: Timeframe, label: str, kind: CandleLabelErrorKind
) -> None:
    with pytest.raises(CandleLabelError) as caught:
        TOY.candle_slot(timeframe, utc(label).astimezone(NEW_YORK))

    error = caught.value
    assert error.kind is kind
    assert error.timeframe is timeframe
    assert error.label == utc(label)
    assert type(error.label) is datetime
    assert error.label.tzinfo is UTC
    assert (
        str(error) == f"{utc(label).isoformat()} is not a {timeframe} candle label in TOY ({kind})"
    )


@pytest.mark.parametrize("timeframe", list(Timeframe))
@pytest.mark.parametrize("label", ["2024-03-06T04:59:59.999999", "2024-03-15T04:00"])
def test_candle_slot_outside_the_coverage_raises_calendar_range_error(
    timeframe: Timeframe, label: str
) -> None:
    with pytest.raises(CalendarRangeError) as caught:
        TOY.candle_slot(timeframe, utc(label))

    assert not isinstance(caught.value, CandleLabelError)


# --- T7: next_candle_close (AC8) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "h1", "h4", "d1"),
    [
        ("2024-03-06T05:00", "2024-03-07T15:30", "2024-03-07T18:30", "2024-03-07T21:00"),
        ("2024-03-07T14:30", "2024-03-07T15:30", "2024-03-07T18:30", "2024-03-07T21:00"),
        ("2024-03-07T15:30", "2024-03-07T16:30", "2024-03-07T18:30", "2024-03-07T21:00"),
        ("2024-03-07T18:30", "2024-03-07T19:30", "2024-03-07T21:00", "2024-03-07T21:00"),
        (
            "2024-03-07T20:59:59.999999",
            "2024-03-07T21:00",
            "2024-03-07T21:00",
            "2024-03-07T21:00",
        ),
        ("2024-03-07T21:00", "2024-03-08T15:30", "2024-03-08T18:00", "2024-03-08T18:00"),
        ("2024-03-08T17:30", "2024-03-08T18:00", "2024-03-08T18:00", "2024-03-08T18:00"),
        ("2024-03-08T18:00", "2024-03-11T14:30", "2024-03-11T17:30", "2024-03-11T20:00"),
        ("2024-03-09T12:00", "2024-03-11T14:30", "2024-03-11T17:30", "2024-03-11T20:00"),
        ("2024-03-10T07:30", "2024-03-11T14:30", "2024-03-11T17:30", "2024-03-11T20:00"),
        ("2024-03-11T20:00", "2024-03-13T15:00", "2024-03-13T18:00", "2024-03-13T19:00"),
        ("2024-03-12T15:00", "2024-03-13T15:00", "2024-03-13T18:00", "2024-03-13T19:00"),
        ("2024-03-13T13:59:59.999999", "2024-03-13T15:00", "2024-03-13T18:00", "2024-03-13T19:00"),
        ("2024-03-13T14:00", "2024-03-13T15:00", "2024-03-13T18:00", "2024-03-13T19:00"),
        ("2024-03-13T18:30", "2024-03-13T19:00", "2024-03-13T19:00", "2024-03-13T19:00"),
    ],
)
def test_next_candle_close_is_strictly_after_now(now: str, h1: str, h4: str, d1: str) -> None:
    results = {tf: TOY.next_candle_close(tf, utc(now)) for tf in Timeframe}

    assert results == {H1: utc(h1), H4: utc(h4), D1: utc(d1)}
    assert all(type(result) is datetime and result.tzinfo is UTC for result in results.values())


@pytest.mark.parametrize("timeframe", list(Timeframe))
@pytest.mark.parametrize(
    "now", ["2024-03-13T19:00", "2024-03-14T12:00", "2024-03-15T03:59:59.999999"]
)
def test_next_candle_close_raises_when_no_slot_closes_later_in_the_calendar(
    timeframe: Timeframe, now: str
) -> None:
    with pytest.raises(CalendarRangeError) as caught:
        TOY.next_candle_close(timeframe, utc(now))

    assert caught.value.calendar == "TOY"


# --- T8: closed_candles (AC9) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "now", "count", "expected"),
    [
        (D1, "2024-03-07T21:00", 1, TOY_D1[:1]),
        (D1, "2024-03-13T18:59:59.999999", 1, TOY_D1[2:3]),
        (D1, "2024-03-15T03:59:59.999999", 4, TOY_D1),
        (H1, "2024-03-07T15:30", 1, TOY_H1[:1]),
        (H1, "2024-03-11T14:00", 3, TOY_H1[8:11]),
        (H1, "2024-03-11T15:30", 3, TOY_H1[10:13]),
        (H1, "2024-03-13T19:00", 23, TOY_H1),
        (H4, "2024-03-13T19:00", 7, TOY_H4),
        (H4, "2024-03-11T17:29:59.999999", 2, TOY_H4[1:3]),
        (H1, "2024-03-06T05:00", 0, ()),
        (D1, "2024-03-15T03:59:59.999999", 0, ()),
    ],
)
def test_closed_candles_returns_the_last_closed_slots_oldest_first(
    timeframe: Timeframe, now: str, count: int, expected: tuple[CandleSlot, ...]
) -> None:
    result = TOY.closed_candles(timeframe, utc(now), count)

    assert type(result) is tuple
    assert result == expected


@pytest.mark.parametrize(
    ("timeframe", "now", "count"),
    [
        (D1, "2024-03-07T21:00", 2),
        (D1, "2024-03-07T20:59:59.999999", 1),
        (H1, "2024-03-07T15:29:59.999999", 1),
        (H1, "2024-03-13T19:00", 24),
        (H4, "2024-03-13T19:00", 8),
        (H1, "2024-03-06T05:00", 1),
        (H1, "2024-03-15T03:59:59.999999", 10**9),
        (H4, "2024-03-15T03:59:59.999999", 10**9),
        (D1, "2024-03-15T03:59:59.999999", 10**9),
        (H1, "2024-03-15T03:59:59.999999", 10**400),
    ],
)
def test_closed_candles_raises_without_enough_history(
    timeframe: Timeframe, now: str, count: int
) -> None:
    with pytest.raises(CalendarRangeError) as caught:
        TOY.closed_candles(timeframe, utc(now), count)

    assert "\n" not in str(caught.value)
    assert len(str(caught.value)) < 300


def test_closed_candles_with_zero_count_still_checks_the_coverage() -> None:
    with pytest.raises(CalendarRangeError):
        TOY.closed_candles(H1, utc("2024-03-15T04:00"), 0)


@pytest.mark.parametrize("count", [-1, -(10**400)])
def test_closed_candles_rejects_a_negative_count(count: int) -> None:
    with pytest.raises(ValueError, match="count") as caught:
        TOY.closed_candles(H1, utc("2024-03-13T19:00"), count)

    assert not isinstance(caught.value, CalendarError)
    assert len(str(caught.value)) < 300


@pytest.mark.parametrize("count", [True, False, 1.0, "1", None], ids=repr)
def test_closed_candles_rejects_a_count_that_is_not_an_int(count: object) -> None:
    with pytest.raises(TypeError, match="count"):
        TOY.closed_candles(H1, utc("2024-03-13T19:00"), count)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_closed_candles_matches_the_written_grid_at_every_boundary(timeframe: Timeframe) -> None:
    for now in all_boundaries():
        closed = [candle for candle in TOY_GRID[timeframe] if candle.close_time <= now]
        previous: tuple[CandleSlot, ...] = ()
        for count in range(len(closed) + 1):
            result = TOY.closed_candles(timeframe, now, count)
            assert result == tuple(closed[len(closed) - count :])
            assert result[1:] == previous  # count - 1 candles are the suffix of count candles
            previous = result
        with pytest.raises(CalendarRangeError):
            TOY.closed_candles(timeframe, now, len(closed) + 1)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_closed_candles_is_consistent_with_next_candle_close(timeframe: Timeframe) -> None:
    last_close = utc("2024-03-13T19:00")
    checked = 0
    for now in all_boundaries():
        if now >= last_close:
            continue
        try:
            last = TOY.closed_candles(timeframe, now, 1)[-1]
        except CalendarRangeError:
            continue
        following = TOY.next_candle_close(timeframe, now)
        assert last.close_time <= now < following
        assert TOY.next_candle_close(timeframe, last.close_time) == following
        checked += 1

    assert checked > 20


# --- T8: value semantics and messages (AC10) ---------------------------------------------------


@pytest.mark.parametrize(
    "instance",
    [TOY.sessions[0], TOY_H1[0], TOY],
    ids=["Session", "CandleSlot", "MarketCalendar"],
)
def test_every_field_is_frozen(instance: object) -> None:
    for field in dataclasses.fields(instance):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field.name, None)


def test_values_are_equal_by_value_and_hashable() -> None:
    other = toy_calendar()
    rebuilt_slot = slot(H1, "2024-03-07T14:30", "2024-03-07T15:30")

    assert other == TOY
    assert other is not TOY
    assert hash(other) == hash(TOY)
    assert other.sessions[0] == TOY.sessions[0]
    assert hash(other.sessions[0]) == hash(TOY.sessions[0])
    assert rebuilt_slot == TOY_H1[0]
    assert hash(rebuilt_slot) == hash(TOY_H1[0])
    assert len({TOY, other}) == 1
    assert len(set(TOY_H1) | set(TOY_H4) | set(TOY_D1)) == 34
    assert calendar_with(name="OTHER") != TOY
    assert calendar_with(sessions=TOY.sessions[:3]) != TOY
    assert calendar_with(timezone=ZoneInfo("America/Chicago"), sessions=()) != calendar_with(
        sessions=()
    )


def test_calendar_repr_is_bounded_and_does_not_list_sessions() -> None:
    text = repr(TOY)

    assert text == (
        "MarketCalendar(name='TOY', first_day=2024-03-06, last_day=2024-03-14, sessions=4)"
    )
    assert "Session(" not in text


class _Weird:
    pass


_Weird.__name__ = "Weird\nName with spaces"

FAILING_CALLS: dict[str, Callable[[], object]] = {
    "range-instant": lambda: TOY.is_open(utc("2024-03-15T04:00")),
    "range-day": lambda: TOY.session_bounds(date(2024, 3, 15)),
    "range-next": lambda: TOY.next_candle_close(H4, utc("2024-03-13T19:00")),
    "range-closed": lambda: TOY.closed_candles(D1, utc("2024-03-07T21:00"), 10**400),
    "range-slots": lambda: TOY.candle_slots(H1, TOY.coverage_start, utc("2030-01-01T00:00")),
    "label": lambda: TOY.candle_slot(D1, utc("2024-03-08T00:00")),
    "start-after-end": lambda: TOY.candle_slots(
        H1, utc("2024-03-08T00:00"), utc("2024-03-07T00:00")
    ),
    "negative-count": lambda: TOY.closed_candles(H1, utc("2024-03-08T00:00"), -(10**400)),
    "count-type": lambda: TOY.closed_candles(H1, utc("2024-03-08T00:00"), _Weird()),
    "timeframe-type": lambda: TOY.candle_slot(_Weird(), utc("2024-03-08T00:00")),
    "instant-type": lambda: TOY.is_open(_Weird()),
    "day-type": lambda: TOY.session_bounds(_Weird()),
    "session-order": lambda: Session(
        day=date(2024, 3, 7), open_time=utc("2024-03-07T21:00"), close_time=utc("2024-03-07T14:30")
    ),
    "session-instant-type": lambda: Session(
        day=date(2024, 3, 7),
        open_time=_Weird(),
        close_time=utc("2024-03-07T14:30"),
    ),
    "calendar-name": lambda: calendar_with(name="bad\nname" * 40),
    "calendar-name-type": lambda: calendar_with(name=_Weird()),
    "calendar-timezone": lambda: calendar_with(timezone=_Weird()),
    "calendar-days": lambda: calendar_with(first_day=date(2024, 3, 15)),
    "calendar-sessions-type": lambda: calendar_with(sessions=[_Weird()]),
    "calendar-session-item": lambda: calendar_with(sessions=(_Weird(),)),
    "calendar-unsorted": lambda: calendar_with(sessions=(TOY.sessions[1], TOY.sessions[0])),
    "calendar-outside": lambda: calendar_with(first_day=date(2024, 3, 8)),
    "calendar-overnight": lambda: calendar_with(
        sessions=(
            Session(
                day=date(2024, 3, 7),
                open_time=utc("2024-03-07T23:00"),
                close_time=utc("2024-03-08T06:00"),
            ),
        )
    ),
}


@pytest.mark.parametrize("call", FAILING_CALLS.values(), ids=FAILING_CALLS.keys())
def test_error_messages_are_single_bounded_lines(call: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)) as caught:
        call()

    message = str(caught.value)
    assert "\n" not in message
    assert 0 < len(message) < 300
    assert "Weird" not in message
    assert "bad" not in message


def test_calendar_errors_survive_pickling() -> None:
    with pytest.raises(CalendarRangeError) as range_caught:
        TOY.session_bounds(date(2024, 3, 15))
    with pytest.raises(CandleLabelError) as label_caught:
        TOY.candle_slot(H1, utc("2024-03-07T15:00"))

    range_error = pickle.loads(pickle.dumps(range_caught.value))  # noqa: S301 - own objects
    label_error = pickle.loads(pickle.dumps(label_caught.value))  # noqa: S301 - own objects

    assert str(range_error) == str(range_caught.value)
    assert (range_error.calendar, range_error.first_day, range_error.last_day) == (
        "TOY",
        date(2024, 3, 6),
        date(2024, 3, 14),
    )
    assert str(label_error) == str(label_caught.value)
    assert (label_error.kind, label_error.timeframe, label_error.label) == (
        CandleLabelErrorKind.OFF_GRID,
        H1,
        utc("2024-03-07T15:00"),
    )


def test_queries_on_a_calendar_without_sessions() -> None:
    empty = calendar_with(sessions=())

    assert empty.is_open(utc("2024-03-07T15:00")) is False
    assert empty.candle_slots(H1, empty.coverage_start, empty.coverage_end) == ()
    assert empty.closed_candles(D1, utc("2024-03-07T15:00"), 0) == ()
    with pytest.raises(CalendarRangeError):
        empty.next_candle_close(H1, empty.coverage_start)
    with pytest.raises(CalendarRangeError):
        empty.closed_candles(D1, utc("2024-03-14T23:00"), 1)
    with pytest.raises(CandleLabelError) as caught:
        empty.candle_slot(H1, utc("2024-03-07T14:30"))
    assert caught.value.kind is CandleLabelErrorKind.NOT_A_SESSION


def test_module_exports_exactly_the_public_api() -> None:
    assert sessions_module.__all__ == [
        "CalendarError",
        "CalendarRangeError",
        "CandleLabelError",
        "CandleLabelErrorKind",
        "CandleSlot",
        "MarketCalendar",
        "Session",
    ]

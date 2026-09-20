"""The schedule: ``next_fire``, ``CandleCloseTrigger`` and the slot predicates (T2, T3).

Every expected instant below is a **literal** taken from the golden table of spec 016, Design
7.2, which was computed while the spec was written: nothing here is regenerated with the code
under test, so an arithmetic change that is wrong in both places cannot pass.

No clock is read: ``after`` and ``now`` are always literals, and the calendar is the shared
``nyse_test_calendar()``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from tests.fixtures.calendars import NEW_YORK, nyse_test_calendar, utc
from trading_bot.data.yahoo.provider import is_unpublished_hour
from trading_bot.domain.market_calendar.sessions import (
    CalendarRangeError,
    CandleSlot,
    MarketCalendar,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.scheduler.slots import skip_unpublished_hours
from trading_bot.scheduler.trigger import (
    CandleCloseTrigger,
    SlotPredicate,
    next_fire,
    run_every_slot,
)

LOGGER_NAME = "trading_bot.scheduler"
DELAY = timedelta(seconds=120)
RESOLUTION = timedelta(microseconds=1)

# 2024-07-01 (Monday, EDT, regular): 13:30-20:00Z. 2024-07-03: half day, 13:30-17:00Z.
# 2024-07-04: holiday. 2024-03-10 and 2024-11-03: the two DST changes.
GOLDEN_FIRE_TIMES = [
    # (after, 1h, 4h, 1d)
    ("2024-07-01T13:00", "2024-07-01T14:32", "2024-07-01T17:32", "2024-07-01T20:02"),
    ("2024-07-01T19:45", "2024-07-01T20:02", "2024-07-01T20:02", "2024-07-01T20:02"),
    ("2024-07-01T20:02", "2024-07-01T20:02", "2024-07-01T20:02", "2024-07-01T20:02"),
    ("2024-07-01T20:02:00.000001", "2024-07-02T14:32", "2024-07-02T17:32", "2024-07-02T20:02"),
    ("2024-07-03T15:00", "2024-07-03T15:32", "2024-07-03T17:02", "2024-07-03T17:02"),
    ("2024-07-03T16:35", "2024-07-05T14:32", "2024-07-03T17:02", "2024-07-03T17:02"),
    ("2024-07-03T17:05", "2024-07-05T14:32", "2024-07-05T17:32", "2024-07-05T20:02"),
    ("2024-03-08T21:05", "2024-03-11T14:32", "2024-03-11T17:32", "2024-03-11T20:02"),
    ("2024-11-01T20:05", "2024-11-04T15:32", "2024-11-04T18:32", "2024-11-04T21:02"),
]


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    return nyse_test_calendar()


@pytest.fixture(scope="module")
def skip_unpublished(calendar: MarketCalendar) -> SlotPredicate:
    return skip_unpublished_hours(calendar)


def fire_time(
    calendar: MarketCalendar,
    timeframe: Timeframe,
    after: str,
    *,
    delay: timedelta = DELAY,
    should_run: SlotPredicate = run_every_slot,
) -> datetime:
    return next_fire(calendar, timeframe, after=utc(after), delay=delay, should_run=should_run)[1]


# --- T2: the golden table (AC1, AC2) --------------------------------------------------------


@pytest.mark.parametrize(("after", "hourly", "four_hourly", "daily"), GOLDEN_FIRE_TIMES)
def test_the_golden_fire_times_are_reproduced(
    calendar: MarketCalendar,
    skip_unpublished: SlotPredicate,
    after: str,
    hourly: str,
    four_hourly: str,
    daily: str,
) -> None:
    """Spec 016, Design 7.2, with ``delay=120s`` and ``should_run=skip_unpublished_hours``."""
    expected = {Timeframe.H1: hourly, Timeframe.H4: four_hourly, Timeframe.D1: daily}

    for timeframe, instant in expected.items():
        slot, fire = next_fire(
            calendar, timeframe, after=utc(after), delay=DELAY, should_run=skip_unpublished
        )

        assert fire == utc(instant), f"{timeframe.value} after {after}"
        assert slot.close_time == fire - DELAY
        assert slot.timeframe is timeframe


def test_a_fire_time_is_at_or_after_the_argument_to_the_microsecond(
    calendar: MarketCalendar,
) -> None:
    """ "At or after": the boundary is the fire instant itself, not the microsecond after it."""
    exact = utc("2024-07-01T20:02")

    assert fire_time(calendar, Timeframe.H1, "2024-07-01T20:01:59.999999") == exact
    assert fire_time(calendar, Timeframe.H1, "2024-07-01T20:02") == exact
    assert fire_time(calendar, Timeframe.H1, "2024-07-01T20:02:00.000001") == utc(
        "2024-07-02T14:32"
    )


@pytest.mark.parametrize(
    ("delay", "first", "last"),
    [
        (timedelta(0), "2024-07-01T14:30", "2024-07-01T20:00"),
        (timedelta(seconds=120), "2024-07-01T14:32", "2024-07-01T20:02"),
        (timedelta(seconds=900), "2024-07-01T14:45", "2024-07-01T20:15"),
    ],
    ids=["0s", "120s", "900s"],
)
def test_the_delay_shifts_the_first_and_last_fire_of_a_session(
    calendar: MarketCalendar, delay: timedelta, first: str, last: str
) -> None:
    assert fire_time(calendar, Timeframe.H1, "2024-07-01T00:00", delay=delay) == utc(first)
    assert fire_time(calendar, Timeframe.H1, "2024-07-01T19:46", delay=delay) == utc(last)


def test_the_largest_delay_still_orders_the_thirty_minute_gap_of_a_session(
    calendar: MarketCalendar,
) -> None:
    """Why ``MAX_CLOSE_DELAY`` is fifteen minutes: the last two ``1h`` closes are 30 min apart."""
    assert list(
        fire_sequence(calendar, Timeframe.H1, "2024-07-01T19:00", 2, delay=timedelta(seconds=900))
    ) == [utc("2024-07-01T19:45"), utc("2024-07-01T20:15")]


def test_the_fires_of_one_regular_session_are_the_whole_grid(calendar: MarketCalendar) -> None:
    """The truncated 19:30-20:00Z slot of a regular session is fired for (AC4)."""
    assert list(fire_sequence(calendar, Timeframe.H1, "2024-07-01T00:00", 7)) == [
        utc(instant)
        for instant in (
            "2024-07-01T14:32",
            "2024-07-01T15:32",
            "2024-07-01T16:32",
            "2024-07-01T17:32",
            "2024-07-01T18:32",
            "2024-07-01T19:32",
            "2024-07-01T20:02",
        )
    ]
    assert list(fire_sequence(calendar, Timeframe.H4, "2024-07-01T00:00", 2)) == [
        utc("2024-07-01T17:32"),
        utc("2024-07-01T20:02"),
    ]


def test_the_daily_candle_fires_after_the_session_close_never_at_midnight(
    calendar: MarketCalendar,
) -> None:
    """User decision U6: ``1d`` fires at the close, not at midnight and not at the next open."""
    fires = list(fire_sequence(calendar, Timeframe.D1, "2024-07-01T00:00", 3))

    assert fires == [utc("2024-07-01T20:02"), utc("2024-07-02T20:02"), utc("2024-07-03T17:02")]


def test_a_daily_slot_keeps_its_label_while_the_fire_follows_the_close(
    calendar: MarketCalendar,
) -> None:
    slot, fire = next_fire(calendar, Timeframe.D1, after=utc("2024-07-01T00:00"), delay=DELAY)

    assert slot.label == utc("2024-07-01T04:00")  # 00:00 New York of the session date
    assert slot.close_time == utc("2024-07-01T20:00")
    assert fire == utc("2024-07-01T20:02")


# --- T2: the predicate (AC4) ----------------------------------------------------------------


def test_the_unpublished_half_day_hour_is_never_fired_for(
    calendar: MarketCalendar, skip_unpublished: SlotPredicate
) -> None:
    """2024-07-03 is a half day: its 16:30-17:00Z slot is the one Yahoo never publishes."""
    with_predicate = list(
        fire_sequence(calendar, Timeframe.H1, "2024-07-03T00:00", 4, should_run=skip_unpublished)
    )
    without_predicate = list(fire_sequence(calendar, Timeframe.H1, "2024-07-03T00:00", 4))

    assert with_predicate == [
        utc("2024-07-03T14:32"),
        utc("2024-07-03T15:32"),
        utc("2024-07-03T16:32"),
        utc("2024-07-05T14:32"),  # 2024-07-04 is a holiday
    ]
    assert without_predicate[3] == utc("2024-07-03T17:02")


def test_the_predicate_skips_only_the_unpublished_hours(
    calendar: MarketCalendar, skip_unpublished: SlotPredicate
) -> None:
    """Every other slot of the grid is accepted, the truncated regular one included."""
    rejected = [
        slot
        for timeframe in Timeframe
        for slot in calendar.candle_slots(
            timeframe, utc("2024-01-01T05:00"), utc("2025-01-01T05:00")
        )
        if not skip_unpublished(slot)
    ]

    assert rejected != []
    assert all(slot.timeframe is Timeframe.H1 for slot in rejected)
    assert all(is_unpublished_hour(slot, calendar=calendar) for slot in rejected)
    assert all(slot.close_time - slot.open_time < timedelta(hours=1) for slot in rejected)


def test_the_predicate_answers_for_four_hourly_and_daily_slots_without_raising(
    calendar: MarketCalendar, skip_unpublished: SlotPredicate
) -> None:
    """``is_unpublished_hour`` raises for other timeframes, so it must not be called for them."""
    for timeframe in (Timeframe.H4, Timeframe.D1):
        slots = calendar.candle_slots(timeframe, utc("2024-07-01T04:00"), utc("2024-07-08T04:00"))

        assert slots != ()
        assert all(skip_unpublished(slot) for slot in slots)


def test_the_default_predicate_accepts_every_slot(calendar: MarketCalendar) -> None:
    slots = calendar.candle_slots(Timeframe.H1, utc("2024-07-01T04:00"), utc("2024-07-08T04:00"))

    assert slots != ()
    assert all(run_every_slot(slot) for slot in slots)


# --- T2: arguments, coverage and a predicate that rejects everything (AC5) ------------------


@pytest.mark.parametrize("after", ["2020-06-01T12:00", "2028-06-01T12:00"], ids=["before", "after"])
def test_an_instant_outside_the_calendar_raises(calendar: MarketCalendar, after: str) -> None:
    with pytest.raises(CalendarRangeError):
        next_fire(calendar, Timeframe.H1, after=utc(after), delay=DELAY)


def test_an_instant_the_calendar_cannot_answer_for_raises(calendar: MarketCalendar) -> None:
    """Inside the coverage, but no slot of the calendar closes after it."""
    with pytest.raises(CalendarRangeError):
        next_fire(calendar, Timeframe.D1, after=utc("2027-12-31T23:00"), delay=DELAY)


def test_a_predicate_that_rejects_everything_fails_loudly(calendar: MarketCalendar) -> None:
    """Better a ``ValueError`` than a walk through a century of sessions (Design 7.1)."""
    with pytest.raises(ValueError, match="rejected 64 consecutive"):
        next_fire(
            calendar,
            Timeframe.H1,
            after=utc("2024-07-01T00:00"),
            delay=DELAY,
            should_run=lambda slot: False,
        )


def test_a_predicate_that_rejects_sixty_three_slots_still_answers(
    calendar: MarketCalendar,
) -> None:
    """The bound is on **consecutive** rejections: 63 of them are not a failure."""
    seen: list[CandleSlot] = []

    def reject_the_first_sixty_three(slot: CandleSlot) -> bool:
        seen.append(slot)
        return len(seen) > 63

    slot, fire = next_fire(
        calendar,
        Timeframe.H1,
        after=utc("2024-07-01T00:00"),
        delay=DELAY,
        should_run=reject_the_first_sixty_three,
    )

    assert len(seen) == 64
    assert fire == slot.close_time + DELAY


@pytest.mark.parametrize(
    ("timeframe", "after", "delay"),
    [
        ("1h", utc("2024-07-01T13:00"), DELAY),
        (Timeframe.H1, "2024-07-01T13:00", DELAY),
        (Timeframe.H1, utc("2024-07-01T13:00"), 120),
    ],
    ids=["timeframe", "after", "delay"],
)
def test_an_argument_of_the_wrong_type_raises(
    calendar: MarketCalendar, timeframe: object, after: object, delay: object
) -> None:
    with pytest.raises(TypeError):
        next_fire(calendar, timeframe, after=after, delay=delay)  # type: ignore[arg-type]


def test_a_naive_instant_raises(calendar: MarketCalendar) -> None:
    with pytest.raises(ValueError, match="aware"):
        next_fire(calendar, Timeframe.H1, after=datetime(2024, 7, 1, 13, 0), delay=DELAY)


def test_a_calendar_of_the_wrong_type_raises() -> None:
    with pytest.raises(TypeError, match="calendar"):
        next_fire(
            "NYSE",  # type: ignore[arg-type]
            Timeframe.H1,
            after=utc("2024-07-01T13:00"),
            delay=DELAY,
        )
    with pytest.raises(TypeError, match="calendar"):
        skip_unpublished_hours("NYSE")  # type: ignore[arg-type]


# --- T3: the schedule advances over a whole year (AC1, AC3) ---------------------------------


@pytest.mark.parametrize("timeframe", list(Timeframe), ids=lambda value: value.value)
@pytest.mark.parametrize(
    "delay",
    [timedelta(0), timedelta(seconds=120), timedelta(seconds=900)],
    ids=["0s", "120s", "900s"],
)
def test_a_year_of_fires_visits_every_slot_once_in_order(
    calendar: MarketCalendar, timeframe: Timeframe, delay: timedelta
) -> None:
    """CLAUDE.md rule 4, as AC3 states it: no fire precedes the close it is about."""
    predicate = skip_unpublished_hours(calendar)
    start, end = utc("2024-01-01T05:00"), utc("2025-01-01T05:00")
    expected = [slot for slot in calendar.candle_slots(timeframe, start, end) if predicate(slot)]

    visited: list[CandleSlot] = []
    fires: list[datetime] = []
    after = start
    while True:
        slot, fire = next_fire(calendar, timeframe, after=after, delay=delay, should_run=predicate)
        if slot.label >= end:
            break
        visited.append(slot)
        fires.append(fire)
        after = fire + RESOLUTION

    assert visited == expected
    assert fires == sorted(fires)
    assert len(set(fires)) == len(fires)
    for slot, fire in zip(visited, fires, strict=True):
        assert slot.close_time + delay == fire
        assert calendar.closed_candles(timeframe, fire, 1)[-1] == slot
        assert calendar.session_bounds(slot.session_day) is not None


# --- T3: the trigger (AC1, AC5) -------------------------------------------------------------


def trigger(
    calendar: MarketCalendar,
    timeframe: Timeframe = Timeframe.H1,
    *,
    delay: timedelta = DELAY,
    should_run: SlotPredicate = run_every_slot,
) -> CandleCloseTrigger:
    return CandleCloseTrigger(
        calendar=calendar, timeframe=timeframe, delay=delay, should_run=should_run
    )


def test_the_trigger_agrees_with_next_fire(calendar: MarketCalendar) -> None:
    now = utc("2024-07-01T13:00")

    assert trigger(calendar).get_next_fire_time(None, now) == utc("2024-07-01T14:32")
    assert trigger(calendar, Timeframe.D1).get_next_fire_time(None, now) == utc("2024-07-01T20:02")


def test_the_trigger_advances_when_apscheduler_asks_twice_with_the_same_now(
    calendar: MarketCalendar,
) -> None:
    """``previous_fire_time`` is what makes the sequence move; ``now`` may not have."""
    candle_close = trigger(calendar)
    now = utc("2024-07-01T14:32")

    first = candle_close.get_next_fire_time(None, now)
    second = candle_close.get_next_fire_time(first, now)
    third = candle_close.get_next_fire_time(second, now)

    assert first == utc("2024-07-01T14:32")
    assert second == utc("2024-07-01T15:32")
    assert third == utc("2024-07-01T16:32")


def test_the_trigger_uses_the_later_of_now_and_the_previous_fire(
    calendar: MarketCalendar,
) -> None:
    """A fire that ran late must not replay the closes that went by meanwhile."""
    candle_close = trigger(calendar)

    assert candle_close.get_next_fire_time(utc("2024-07-01T14:32"), utc("2024-07-01T18:00")) == utc(
        "2024-07-01T18:32"
    )


def test_the_trigger_returns_none_and_logs_one_error_outside_the_calendar(
    calendar: MarketCalendar, caplog: pytest.LogCaptureFixture
) -> None:
    """AC5: raising inside APScheduler's loop would take the scheduler down with it."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        result = trigger(calendar).get_next_fire_time(None, utc("2028-06-01T12:00"))

    assert result is None
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and record.name == LOGGER_NAME
    ]
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "1h trigger: no next fire after 2028-06-01T12:00:00+00:00 (CalendarRangeError)"
    )


def test_the_trigger_returns_none_and_logs_one_error_when_the_predicate_rejects_everything(
    calendar: MarketCalendar, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        result = trigger(calendar, should_run=lambda slot: False).get_next_fire_time(
            None, utc("2024-07-01T13:00")
        )

    assert result is None
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and record.name == LOGGER_NAME
    ]
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "1h trigger: no next fire after 2024-07-01T13:00:00+00:00 (ValueError)"
    )


def test_the_trigger_names_itself_for_the_job_repr(calendar: MarketCalendar) -> None:
    assert str(trigger(calendar)) == "candle close 1h + 120s"
    assert str(trigger(calendar, Timeframe.D1, delay=timedelta(0))) == "candle close 1d + 0s"


def test_the_trigger_normalizes_the_instants_it_is_given(calendar: MarketCalendar) -> None:
    """APScheduler runs on ``timezone=UTC``, but the trigger never assumes the zone."""
    new_york = utc("2024-07-01T13:00").astimezone(NEW_YORK)

    assert trigger(calendar).get_next_fire_time(None, new_york) == utc("2024-07-01T14:32")


def test_the_trigger_rejects_arguments_of_the_wrong_type(calendar: MarketCalendar) -> None:
    with pytest.raises(TypeError, match="calendar"):
        CandleCloseTrigger(
            calendar="NYSE",  # type: ignore[arg-type]
            timeframe=Timeframe.H1,
            delay=DELAY,
        )
    with pytest.raises(TypeError):
        CandleCloseTrigger(
            calendar=calendar,
            timeframe="1h",  # type: ignore[arg-type]
            delay=DELAY,
        )
    with pytest.raises(TypeError):
        CandleCloseTrigger(
            calendar=calendar,
            timeframe=Timeframe.H1,
            delay=120,  # type: ignore[arg-type]
        )


def fire_sequence(
    calendar: MarketCalendar,
    timeframe: Timeframe,
    after: str,
    count: int,
    *,
    delay: timedelta = DELAY,
    should_run: SlotPredicate = run_every_slot,
) -> list[datetime]:
    """``count`` consecutive fire times, each one fed back as ``after + 1 microsecond``."""
    instant = utc(after)
    fires: list[datetime] = []
    for _ in range(count):
        _, fire = next_fire(calendar, timeframe, after=instant, delay=delay, should_run=should_run)
        fires.append(fire)
        instant = fire + RESOLUTION
    return fires

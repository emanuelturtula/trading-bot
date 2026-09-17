"""Property tests of the calendar queries on the NYSE test calendar (spec 009, T13).

Hypothesis draws instants across ``tests.fixtures.calendars.nyse_test_calendar()`` (2021-01-01 to
2027-12-31) at microsecond resolution and checks invariants that must hold for *every* instant,
not only the hand-picked literals of ``test_market_calendar_nyse.py``: the AC9 consistency
between ``closed_candles`` and ``next_candle_close``, monotonicity of ``next_candle_close``, the
``closed_candles`` suffix property, ``is_open`` agreeing with the ``1h`` slot grid (an independent
code path from ``is_open`` itself), every NYSE session tiled exactly by its intraday slots, and
representation independence (stdlib UTC, a fixed offset, ``ZoneInfo`` and ``pd.Timestamp``).

Drawn instants stay a safe margin away from the calendar's coverage edges, so every timeframe
already has closed candles and still has a future close: the properties assert directly instead
of masking a real bug behind a swallowed ``CalendarRangeError``. The literal edge cases (coverage
boundaries, DST folds and gaps, half-day boundaries) are covered by ``test_market_calendar_nyse.py``
and ``test_market_calendar_adversarial.py``. No network, no wall clock: the NYSE test calendar is
built once per process (``tests.fixtures.calendars.nyse_test_calendar``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.calendars import (
    NEW_YORK,
    NYSE_TEST_FIRST_DAY,
    NYSE_TEST_LAST_DAY,
    nyse_test_calendar,
)
from trading_bot.domain.market_calendar.sessions import CalendarError
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
TIMEFRAMES = list(Timeframe)
_INTRADAY_TIMEFRAMES = [Timeframe.H1, Timeframe.H4]

# Comfortably inside coverage: by 2021-02-01 every timeframe already has several closed candles,
# and 2027-11-01 is well before the calendar's last close, so the properties below never need to
# tolerate a ``CalendarRangeError`` from insufficient history or an exhausted future.
_SAFE_LOWER = datetime(2021, 2, 1, tzinfo=UTC)
_SAFE_UPPER = datetime(2027, 11, 1, tzinfo=UTC)
_SETTINGS = {"max_examples": 30, "deadline": None}


def _safe_instants() -> st.SearchStrategy[datetime]:
    """Instants inside the NYSE test calendar, at microsecond resolution, away from its edges."""
    return st.datetimes(
        min_value=_SAFE_LOWER.replace(tzinfo=None), max_value=_SAFE_UPPER.replace(tzinfo=None)
    ).map(lambda naive: naive.replace(tzinfo=UTC))


def _outcome(call: object, *arguments: object) -> object:
    """The result of ``call(*arguments)``, or the type and message of a calendar error."""
    try:
        return call(*arguments)  # type: ignore[operator]
    except CalendarError as error:
        return (type(error), str(error))


# --- AC9 consistency and the candle_slot round trip ---------------------------------------------


@settings(**_SETTINGS)
@given(now=_safe_instants(), timeframe=st.sampled_from(TIMEFRAMES))
def test_closed_candles_agrees_with_next_candle_close(now: datetime, timeframe: Timeframe) -> None:
    last = NYSE.closed_candles(timeframe, now, 1)[-1]
    following = NYSE.next_candle_close(timeframe, now)

    assert last.close_time <= now < following
    assert NYSE.next_candle_close(timeframe, last.close_time) == following
    assert NYSE.candle_slot(timeframe, last.label) == last


# --- next_candle_close: strictly after now and non-decreasing -----------------------------------


@settings(**_SETTINGS)
@given(pair=st.tuples(_safe_instants(), _safe_instants()), timeframe=st.sampled_from(TIMEFRAMES))
def test_next_candle_close_is_non_decreasing_and_strictly_after_now(
    pair: tuple[datetime, datetime], timeframe: Timeframe
) -> None:
    earlier, later = sorted(pair)

    first_close = NYSE.next_candle_close(timeframe, earlier)
    second_close = NYSE.next_candle_close(timeframe, later)

    assert first_close > earlier
    assert second_close > later
    assert first_close <= second_close


# --- closed_candles: the suffix property ---------------------------------------------------------


@settings(**_SETTINGS)
@given(
    now=_safe_instants(),
    timeframe=st.sampled_from(TIMEFRAMES),
    count=st.integers(min_value=0, max_value=20),
)
def test_closed_candles_of_count_is_the_suffix_of_count_plus_one(
    now: datetime, timeframe: Timeframe, count: int
) -> None:
    larger = NYSE.closed_candles(timeframe, now, count + 1)
    smaller = NYSE.closed_candles(timeframe, now, count)

    assert smaller == larger[1:]


# --- is_open agrees with the 1h slot grid (an independent code path) ----------------------------


@settings(**_SETTINGS)
@given(now=_safe_instants())
def test_is_open_agrees_with_some_hourly_slot_containing_it(now: datetime) -> None:
    window = NYSE.candle_slots(
        Timeframe.H1, now - Timeframe.H1.duration, now + Timeframe.H1.duration
    )
    contains_now = any(slot.open_time <= now < slot.close_time for slot in window)

    assert NYSE.is_open(now) is contains_now


# --- Every NYSE session is tiled exactly by its intraday slots ----------------------------------


@settings(**_SETTINGS)
@given(
    day_offset=st.integers(min_value=0, max_value=(NYSE_TEST_LAST_DAY - NYSE_TEST_FIRST_DAY).days),
    timeframe=st.sampled_from(_INTRADAY_TIMEFRAMES),
)
def test_every_nyse_session_is_tiled_exactly_by_its_intraday_slots(
    day_offset: int, timeframe: Timeframe
) -> None:
    day = NYSE_TEST_FIRST_DAY + timedelta(days=day_offset)
    session = NYSE.session_bounds(day)
    if session is None:
        return

    day_slots = NYSE.candle_slots(timeframe, session.open_time, session.close_time)

    assert day_slots[0].open_time == session.open_time
    assert day_slots[-1].close_time == session.close_time
    for earlier, later in pairwise(day_slots):
        assert earlier.close_time == later.open_time
    for candle in day_slots[:-1]:
        assert candle.close_time - candle.open_time == timeframe.duration
    assert day_slots[-1].close_time - day_slots[-1].open_time <= timeframe.duration


# --- Representation independence over drawn instants (AC3) --------------------------------------


@settings(**_SETTINGS)
@given(now=_safe_instants(), timeframe=st.sampled_from(TIMEFRAMES))
def test_the_same_instant_in_any_representation_agrees_on_nyse(
    now: datetime, timeframe: Timeframe
) -> None:
    def answers(instant: datetime) -> tuple[object, ...]:
        return (
            NYSE.is_open(instant),
            _outcome(NYSE.next_candle_close, timeframe, instant),
            _outcome(NYSE.candle_slot, timeframe, instant),
            _outcome(NYSE.closed_candles, timeframe, instant, 2),
        )

    expected = answers(now)
    fixed_offset = now.astimezone(timezone(timedelta(hours=-5, minutes=-30)))
    zoneinfo_new_york = now.astimezone(NEW_YORK)
    timestamp = pd.Timestamp(now)

    assert answers(fixed_offset) == expected
    assert answers(zoneinfo_new_york) == expected
    assert answers(timestamp) == expected

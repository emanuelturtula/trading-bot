"""Property tests of ``next_fire`` over drawn instants, delays, timeframes and predicates.

Spec 016, T12 (AC1, AC3, AC4). The golden literals of ``test_scheduler_trigger.py`` (T2, T3) are
never regenerated here: hypothesis draws instants that the golden table never names and checks
invariants that must hold for *every* one of them, exactly as ``test_market_calendar_properties.py``
does for the calendar itself. Drawn instants stay a safe margin away from the calendar's coverage
edges (the same margin that module uses), so ``next_fire`` never needs to tolerate a
``CalendarRangeError`` from an exhausted future: that boundary is a literal case (AC5), covered by
``test_scheduler_trigger.py``.

No network, no wall clock: the NYSE test calendar is built once per process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.calendars import nyse_test_calendar
from trading_bot.domain.market_calendar.sessions import CandleSlot
from trading_bot.domain.timeframe import Timeframe
from trading_bot.scheduler.slots import skip_unpublished_hours
from trading_bot.scheduler.trigger import SlotPredicate, next_fire, run_every_slot

NYSE = nyse_test_calendar()
TIMEFRAMES = list(Timeframe)
PREDICATES: list[SlotPredicate] = [run_every_slot, skip_unpublished_hours(NYSE)]

# The same comfortable margin ``test_market_calendar_properties.py`` uses: by this lower bound
# every timeframe already has closed candles, and the upper bound is well before the calendar's
# last close, even after subtracting the largest delay this spec allows (900 s).
_SAFE_LOWER = datetime(2021, 2, 1, tzinfo=UTC)
_SAFE_UPPER = datetime(2027, 11, 1, tzinfo=UTC)
_SETTINGS = {"max_examples": 30, "deadline": None}


def _safe_instants() -> st.SearchStrategy[datetime]:
    return st.datetimes(
        min_value=_SAFE_LOWER.replace(tzinfo=None), max_value=_SAFE_UPPER.replace(tzinfo=None)
    ).map(lambda naive: naive.replace(tzinfo=UTC))


def _delays() -> st.SearchStrategy[timedelta]:
    """Whole seconds in ``[0, 900]``, the range ``SchedulerPolicy.close_delay`` allows."""
    return st.integers(min_value=0, max_value=900).map(lambda seconds: timedelta(seconds=seconds))


_ARGUMENTS = {
    "after": _safe_instants(),
    "delay": _delays(),
    "timeframe": st.sampled_from(TIMEFRAMES),
    "predicate": st.sampled_from(PREDICATES),
}


# --- Every fire is at or after its argument, and matches the slot it names ------------------


@settings(**_SETTINGS)
@given(**_ARGUMENTS)
def test_a_fire_is_at_or_after_its_argument_and_names_a_real_closed_slot(
    after: datetime, delay: timedelta, timeframe: Timeframe, predicate: SlotPredicate
) -> None:
    slot, fire = next_fire(NYSE, timeframe, after=after, delay=delay, should_run=predicate)

    assert fire >= after
    assert slot.close_time + delay == fire
    assert predicate(slot)
    # No fire is ever earlier than the real close of the candle it is about (CLAUDE.md rule 4),
    # and the candle the run would derive at the firing instant is already this exact slot.
    assert NYSE.closed_candles(timeframe, fire, 1)[-1] == slot
    # No fire falls on a weekend or a holiday: every slot belongs to a real trading session.
    assert NYSE.session_bounds(slot.session_day) is not None


# --- Idempotent when fed its own result -------------------------------------------------------


@settings(**_SETTINGS)
@given(**_ARGUMENTS)
def test_next_fire_is_idempotent_on_its_own_result(
    after: datetime, delay: timedelta, timeframe: Timeframe, predicate: SlotPredicate
) -> None:
    slot, fire = next_fire(NYSE, timeframe, after=after, delay=delay, should_run=predicate)

    again = next_fire(NYSE, timeframe, after=fire, delay=delay, should_run=predicate)

    assert again == (slot, fire)


# --- Forward from any earlier point, truncated, agrees with the same result -------------------


@settings(**_SETTINGS)
@given(**_ARGUMENTS, fraction=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_the_result_computed_from_any_point_up_to_the_fire_time_agrees(
    after: datetime,
    delay: timedelta,
    timeframe: Timeframe,
    predicate: SlotPredicate,
    fraction: float,
) -> None:
    """No slot the grid holds becomes eligible strictly between ``after`` and its own fire time."""
    slot, fire = next_fire(NYSE, timeframe, after=after, delay=delay, should_run=predicate)
    probe = after + (fire - after) * fraction

    probed: tuple[CandleSlot, datetime] = next_fire(
        NYSE, timeframe, after=probe, delay=delay, should_run=predicate
    )

    assert probed == (slot, fire)

"""The cooldown gate carries the mandatory look-ahead test (spec 015, T3, AC7; CLAUDE.md rule 4).

``cooldown_decision`` answers about the last label of a frame, so it cannot be checked on its
own: it is paired with ``cooldown_decisions``, the series-valued reference, exactly as
``evaluate`` is paired with ``evaluate_each`` (spec 003). Two deliberate cheats that peek at the
last label are checked first, so a green result on the real functions is not vacuous.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.candles import ALL_SCENARIOS, Scenario, synthetic_candles
from tests.fixtures.session_candles import session_candles
from tests.lookahead import (
    LookaheadError,
    assert_no_lookahead,
    assert_no_lookahead_point_in_time,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.cooldown import CooldownDecision, cooldown_decisions

COOLDOWN_BARS = (0, 1, 3)


def decisions_by_candle(
    *, timeframe: Timeframe, cooldown_bars: int, previous_close: datetime | None
) -> Callable[[pd.DataFrame], object]:
    """The reference: one decision per candle, as an object-dtype Series on the frame labels."""

    def decisions_by_candle(candles: pd.DataFrame) -> object:
        decisions = cooldown_decisions(
            pd.DatetimeIndex(candles.index),
            timeframe=timeframe,
            cooldown_bars=cooldown_bars,
            previous_close=previous_close,
        )
        return pd.Series(list(decisions), index=candles.index, dtype=object, name="decision")

    return decisions_by_candle


def decision_at_last_candle(
    *, timeframe: Timeframe, cooldown_bars: int, previous_close: datetime | None
) -> Callable[[pd.DataFrame], object]:
    """The point-in-time function: one decision about ``candles.index[-1]``."""

    def decision_at_last_candle(candles: pd.DataFrame) -> object:
        return cooldown_decisions(
            pd.DatetimeIndex(candles.index),
            timeframe=timeframe,
            cooldown_bars=cooldown_bars,
            previous_close=previous_close,
        )[-1]

    return decision_at_last_candle


def previous_closes(candles: pd.DataFrame, timeframe: Timeframe) -> tuple[datetime | None, ...]:
    """Previous closes that exercise every branch: none, before, inside and after the frame."""
    index = pd.DatetimeIndex(candles.index)
    middle = len(index) // 2
    return (
        None,
        timeframe.nominal_close(index[0].to_pydatetime()),
        timeframe.nominal_close(index[middle].to_pydatetime()),
        timeframe.nominal_close(index[-1].to_pydatetime()),
    )


# --- The real functions on every scenario ---------------------------------------------------


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[str(item) for item in ALL_SCENARIOS])
def test_the_series_form_never_looks_ahead(scenario: Scenario) -> None:
    candles = synthetic_candles(90, scenario=scenario, timeframe="1d")

    for previous_close in previous_closes(candles, Timeframe.D1):
        for cooldown_bars in COOLDOWN_BARS:
            report = assert_no_lookahead(
                decisions_by_candle(
                    timeframe=Timeframe.D1,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                candles,
            )

            assert report.non_missing_values > 0


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[str(item) for item in ALL_SCENARIOS])
def test_the_point_in_time_form_matches_the_reference(scenario: Scenario) -> None:
    candles = synthetic_candles(90, scenario=scenario, timeframe="1d")

    for previous_close in previous_closes(candles, Timeframe.D1):
        for cooldown_bars in COOLDOWN_BARS:
            report = assert_no_lookahead_point_in_time(
                decision_at_last_candle(
                    timeframe=Timeframe.D1,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                decisions_by_candle(
                    timeframe=Timeframe.D1,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                candles,
            )

            assert report.non_missing_values > 0


@pytest.mark.parametrize("timeframe", [Timeframe.H1, Timeframe.H4, Timeframe.D1])
def test_calendar_aligned_frames_never_look_ahead(timeframe: Timeframe) -> None:
    """The gaps of the real grid (nights, weekends, the 2024-07-04 holiday) are the hard case."""
    candles = session_candles(
        nyse_test_calendar(), timeframe, utc("2024-06-24T00:00"), utc("2024-07-12T00:00")
    )

    for previous_close in previous_closes(candles, timeframe):
        for cooldown_bars in (0, 2):
            assert_no_lookahead(
                decisions_by_candle(
                    timeframe=timeframe,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                candles,
            )
            assert_no_lookahead_point_in_time(
                decision_at_last_candle(
                    timeframe=timeframe,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                decisions_by_candle(
                    timeframe=timeframe,
                    cooldown_bars=cooldown_bars,
                    previous_close=previous_close,
                ),
                candles,
            )


def test_a_decision_never_changes_when_later_candles_are_appended() -> None:
    """The obligation of CLAUDE.md rule 4, spelled out on one frame."""
    candles = session_candles(
        nyse_test_calendar(), Timeframe.D1, utc("2024-06-24T00:00"), utc("2024-07-12T00:00")
    )
    index = pd.DatetimeIndex(candles.index)
    previous_close = Timeframe.D1.nominal_close(index[2].to_pydatetime())
    whole = cooldown_decisions(
        index, timeframe=Timeframe.D1, cooldown_bars=2, previous_close=previous_close
    )

    for length in range(1, len(index) + 1):
        prefix = cooldown_decisions(
            index[:length],
            timeframe=Timeframe.D1,
            cooldown_bars=2,
            previous_close=previous_close,
        )

        assert prefix == whole[:length]


# --- The cheats the harness must catch -------------------------------------------------------


def peeking_decisions(candles: pd.DataFrame) -> object:
    """A cheat: the previous close is read from the **last** candle of the frame."""
    index = pd.DatetimeIndex(candles.index)
    decisions = cooldown_decisions(
        index,
        timeframe=Timeframe.D1,
        cooldown_bars=2,
        previous_close=Timeframe.D1.nominal_close(index[-1].to_pydatetime()),
    )
    return pd.Series(list(decisions), index=candles.index, dtype=object, name="decision")


def decision_at_the_first_candle(candles: pd.DataFrame) -> object:
    """A cheat: the answer is about the wrong candle, which the reference disagrees with."""
    return cooldown_decisions(
        pd.DatetimeIndex(candles.index),
        timeframe=Timeframe.D1,
        cooldown_bars=2,
        previous_close=Timeframe.D1.nominal_close(
            pd.DatetimeIndex(candles.index)[0].to_pydatetime()
        ),
    )[0]


def test_the_harness_catches_a_series_form_that_peeks_at_the_last_label() -> None:
    candles = synthetic_candles(40, timeframe="1d")

    with pytest.raises(LookaheadError) as error:
        assert_no_lookahead(peeking_decisions, candles)

    assert error.value.violation.kind == "value_mismatch"


def test_the_harness_catches_a_point_in_time_form_about_the_wrong_candle() -> None:
    candles = synthetic_candles(40, timeframe="1d")
    index = pd.DatetimeIndex(candles.index)

    with pytest.raises(LookaheadError) as error:
        assert_no_lookahead_point_in_time(
            decision_at_the_first_candle,
            decisions_by_candle(
                timeframe=Timeframe.D1,
                cooldown_bars=2,
                previous_close=Timeframe.D1.nominal_close(index[0].to_pydatetime()),
            ),
            candles,
        )

    assert error.value.violation.kind == "point_in_time_mismatch"
    assert CooldownDecision.ALLOWED in set(CooldownDecision)

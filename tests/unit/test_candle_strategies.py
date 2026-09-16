"""Tests of the Hypothesis candle strategy and profile (spec 003, AC14-AC15)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings

from tests.fixtures.candles import FEATURE_MIN_CANDLES, Scenario
from tests.fixtures.strategies import candle_frames
from tests.lookahead import LookaheadError, assert_no_lookahead
from tests.unit.test_synthetic_candles import (
    assert_has_extreme_features,
    assert_has_flat_run,
    assert_valid_candles,
)

# --- T14: strategy (AC14) --------------------------------------------------------------


@given(candles=candle_frames())
def test_drawn_frames_are_valid(candles: pd.DataFrame) -> None:
    assert 2 <= len(candles) <= 120
    # Every timeframe is aligned to the hourly grid; stricter grids are checked below.
    assert_valid_candles(candles, "1h")


@given(candles=candle_frames(min_size=3, max_size=7))
def test_drawn_frames_respect_size_bounds(candles: pd.DataFrame) -> None:
    assert 3 <= len(candles) <= 7


@given(candles=candle_frames(timeframes=["4h"]))
def test_timeframes_are_restricted_to_four_hours(candles: pd.DataFrame) -> None:
    assert_valid_candles(candles, "4h")


@given(candles=candle_frames(timeframes=["1d"]))
def test_timeframes_are_restricted_to_days(candles: pd.DataFrame) -> None:
    assert_valid_candles(candles, "1d")


@given(candles=candle_frames(scenarios=[Scenario.RANDOM_WALK], timeframes=["1h"]))
def test_scenarios_are_restricted_to_random_walks(candles: pd.DataFrame) -> None:
    steps = candles.index.to_series().diff().iloc[1:]
    assert (steps == pd.Timedelta(hours=1)).all()
    assert np.array_equal(candles["open"].to_numpy()[1:], candles["close"].to_numpy()[:-1])


@given(candles=candle_frames(scenarios=[Scenario.GAPS]))
def test_scenarios_are_restricted_to_gaps(candles: pd.DataFrame) -> None:
    assert (candles.index.dayofweek < 5).all()


@given(candles=candle_frames(min_size=FEATURE_MIN_CANDLES, scenarios=[Scenario.FLAT_RUNS]))
def test_scenarios_are_restricted_to_flat_runs(candles: pd.DataFrame) -> None:
    assert_has_flat_run(candles)


@given(candles=candle_frames(min_size=FEATURE_MIN_CANDLES, scenarios=[Scenario.EXTREME]))
def test_scenarios_are_restricted_to_extreme_values(candles: pd.DataFrame) -> None:
    assert_has_extreme_features(candles)


@given(candles=candle_frames(max_volatility=0.0, scenarios=[Scenario.RANDOM_WALK]))
def test_zero_volatility_draws_are_flat(candles: pd.DataFrame) -> None:
    prices = candles[["open", "high", "low", "close"]].to_numpy()
    assert (prices == prices[0, 0]).all()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"min_size": 0}, id="min-size"),
        pytest.param({"min_size": 10, "max_size": 9}, id="max-below-min"),
        pytest.param({"scenarios": []}, id="no-scenarios"),
        pytest.param({"scenarios": ["sideways"]}, id="unknown-scenario"),
        pytest.param({"timeframes": []}, id="no-timeframes"),
        pytest.param({"timeframes": ["15m"]}, id="unknown-timeframe"),
        pytest.param({"max_volatility": -0.01}, id="negative-volatility"),
        pytest.param({"max_volatility": math.nan}, id="nan-volatility"),
    ],
)
def test_invalid_arguments_fail_when_the_strategy_is_built(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"min_size|max_size|scenario|timeframe|max_volatility"):
        candle_frames(**arguments)  # type: ignore[arg-type]


def sma_20(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean()


def cheat_shift_forward(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].shift(-1)


@given(candles=candle_frames(min_size=40))
def test_honest_rolling_mean_passes_on_drawn_frames(candles: pd.DataFrame) -> None:
    report = assert_no_lookahead(sma_20, candles, max_cuts=10)

    assert report.non_missing_values > 0


@given(candles=candle_frames())
def test_forward_shift_is_always_caught_on_drawn_frames(candles: pd.DataFrame) -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_shift_forward, candles, max_cuts=10)

    assert caught.value.violation.kind == "value_mismatch"


# --- T15: Hypothesis profile (AC15) ----------------------------------------------------


def test_trading_bot_profile_is_loaded() -> None:
    assert settings.default.max_examples == 50
    assert settings.default.deadline is None

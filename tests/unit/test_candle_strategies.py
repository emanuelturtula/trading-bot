"""Tests of the Hypothesis candle strategy and profile (spec 003 AC14-AC15, spec 004 AC5, AC17)."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings

from tests.fixtures.candle_assertions import (
    assert_has_extreme_volume_features,
    assert_has_flat_run,
    assert_valid_candles,
)
from tests.fixtures.candles import FEATURE_MIN_CANDLES, Scenario, synthetic_candles
from tests.fixtures.strategies import candle_frames
from tests.lookahead import LookaheadError, assert_no_lookahead
from trading_bot.domain.candles import validate_candles
from trading_bot.domain.timeframe import Timeframe

TESTS_ROOT = Path(__file__).resolve().parents[1]

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


@given(candles=candle_frames(timeframes=[Timeframe.H4, Timeframe.D1]))
def test_timeframe_members_restrict_timeframes(candles: pd.DataFrame) -> None:
    assert_valid_candles(candles, Timeframe.H4)


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
    # Only parameter-independent features: the -90%/+900% candles can vanish near the price
    # clipping bounds, so the fixed-seed tests at default parameters assert those.
    assert_has_extreme_volume_features(candles)


def test_extreme_volume_features_hold_near_the_price_floor() -> None:
    candles = synthetic_candles(
        112, seed=3370146904, scenario=Scenario.EXTREME, start_price=1e-3, volatility=0.25
    )

    assert_has_extreme_volume_features(candles)


# --- Candle contract on drawn frames (spec 004, AC5) ---------------------------------------


@given(candles=candle_frames())
def test_validate_candles_accepts_every_draw_unchanged(candles: pd.DataFrame) -> None:
    before = candles.copy(deep=True)

    assert validate_candles(candles) is candles
    pd.testing.assert_frame_equal(candles, before, check_exact=True)
    assert candles.index.dtype == before.index.dtype


@given(candles=candle_frames())
def test_validate_candles_accepts_every_prefix_of_a_draw(candles: pd.DataFrame) -> None:
    before = candles.copy(deep=True)

    for n in range(1, len(candles) + 1):
        prefix = candles.iloc[:n]
        assert validate_candles(prefix) is prefix

    pd.testing.assert_frame_equal(candles, before, check_exact=True)


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
        pytest.param({"timeframes": ["1H"]}, id="timeframe-in-another-case"),
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


# --- Fixture hand-off (spec 004, AC17b) ------------------------------------------------------


def test_no_test_module_imports_from_other_test_modules() -> None:
    offenders = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            offenders += [
                f"{path.relative_to(TESTS_ROOT)}: {module}"
                for module in modules
                if module == "tests.unit" or module.startswith("tests.unit.")
            ]

    assert offenders == []

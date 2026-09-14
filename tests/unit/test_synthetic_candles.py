"""Tests of the seeded synthetic OHLCV generator (spec 003, AC10-AC13).

The generator's random streams are not stable across numpy versions, so these tests assert
invariants and scenario features, never golden values.
"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candle_assertions import (
    assert_has_extreme_features,
    assert_has_extreme_volume_features,
    assert_has_flat_run,
    assert_has_gap_features,
    assert_valid_candles,
)
from tests.fixtures.candles import (
    ALL_SCENARIOS,
    DEFAULT_START,
    FEATURE_MIN_CANDLES,
    MAX_PRICE,
    MIN_PRICE,
    TIMEFRAMES,
    Scenario,
    TimeframeCode,
    synthetic_candles,
)
from trading_bot.domain.timeframe import Timeframe

FEATURE_SEEDS = (0, 1, 7, 2024)


def duration(timeframe: Timeframe | TimeframeCode) -> pd.Timedelta:
    return pd.Timedelta(Timeframe(timeframe).duration)


# --- T10: validity (AC10) --------------------------------------------------------------


@pytest.mark.parametrize("volatility", [0.02, 0.0])
@pytest.mark.parametrize("n", [1, 2, 60, 500])
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_generated_frames_are_valid(
    scenario: Scenario, timeframe: TimeframeCode, n: int, volatility: float
) -> None:
    candles = synthetic_candles(
        n, seed=11, scenario=scenario, timeframe=timeframe, volatility=volatility
    )

    assert len(candles) == n
    assert_valid_candles(candles, timeframe)


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_timeframe_members_are_accepted_like_their_codes(
    timeframe: Timeframe, scenario: Scenario
) -> None:
    candles = synthetic_candles(FEATURE_MIN_CANDLES, seed=3, scenario=scenario, timeframe=timeframe)

    assert_valid_candles(candles, timeframe)
    assert_valid_candles(candles, timeframe.value)
    same = synthetic_candles(
        FEATURE_MIN_CANDLES, seed=3, scenario=scenario, timeframe=timeframe.value
    )
    pd.testing.assert_frame_equal(candles, same, check_exact=True)


def test_numpy_integer_sizes_are_accepted() -> None:
    assert len(synthetic_candles(np.int64(3))) == 3  # type: ignore[arg-type]


def test_rejects_non_integer_sizes() -> None:
    with pytest.raises(TypeError, match="n must be an int"):
        synthetic_candles(2.5)  # type: ignore[arg-type]


def test_random_walk_without_volatility_is_constant() -> None:
    candles = synthetic_candles(120, seed=5, volatility=0.0, start_price=42.0)

    assert (candles[["open", "high", "low", "close"]] == 42.0).all().all()


def test_extreme_start_prices_stay_valid() -> None:
    for start_price in (MIN_PRICE, MAX_PRICE):
        candles = synthetic_candles(
            300, seed=9, scenario=Scenario.MIXED, start_price=start_price, volatility=0.25
        )
        assert_valid_candles(candles, "1d")


def test_contiguous_scenarios_start_at_the_requested_open_time() -> None:
    start = pd.Timestamp("2024-03-05T08:00:00+00:00")

    candles = synthetic_candles(10, timeframe="4h", start=start)

    assert candles.index[0] == start
    assert candles.index[-1] == start + 9 * duration("4h")


def test_gaps_starting_on_a_weekend_begin_on_monday() -> None:
    candles = synthetic_candles(
        20, scenario=Scenario.GAPS, timeframe="1d", start="2024-01-06T00:00:00+00:00"
    )

    assert candles.index[0].dayofweek == 0


# --- T11: scenario features (AC11) -----------------------------------------------------


@pytest.mark.parametrize("n", [FEATURE_MIN_CANDLES, 250])
@pytest.mark.parametrize("seed", FEATURE_SEEDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_random_walk_is_contiguous_and_opens_at_the_previous_close(
    timeframe: TimeframeCode, seed: int, n: int
) -> None:
    candles = synthetic_candles(n, seed=seed, timeframe=timeframe)

    steps = candles.index.to_series().diff().iloc[1:]
    assert (steps == duration(timeframe)).all()
    assert np.array_equal(candles["open"].to_numpy()[1:], candles["close"].to_numpy()[:-1])


@pytest.mark.parametrize("n", [FEATURE_MIN_CANDLES, 250])
@pytest.mark.parametrize("seed", FEATURE_SEEDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_gaps_scenario_has_gap_features(timeframe: TimeframeCode, seed: int, n: int) -> None:
    candles = synthetic_candles(n, seed=seed, scenario=Scenario.GAPS, timeframe=timeframe)

    assert_has_gap_features(candles, timeframe)


@pytest.mark.parametrize("n", [FEATURE_MIN_CANDLES, 250])
@pytest.mark.parametrize("seed", FEATURE_SEEDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_flat_runs_scenario_has_a_flat_run(timeframe: TimeframeCode, seed: int, n: int) -> None:
    candles = synthetic_candles(n, seed=seed, scenario=Scenario.FLAT_RUNS, timeframe=timeframe)

    assert_has_flat_run(candles)


@pytest.mark.parametrize("n", [FEATURE_MIN_CANDLES, 250])
@pytest.mark.parametrize("seed", FEATURE_SEEDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_extreme_scenario_has_extreme_features(timeframe: TimeframeCode, seed: int, n: int) -> None:
    candles = synthetic_candles(n, seed=seed, scenario=Scenario.EXTREME, timeframe=timeframe)

    assert_has_extreme_features(candles)


@pytest.mark.parametrize("n", [FEATURE_MIN_CANDLES, 250])
@pytest.mark.parametrize("seed", FEATURE_SEEDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_mixed_scenario_has_every_feature(timeframe: TimeframeCode, seed: int, n: int) -> None:
    candles = synthetic_candles(n, seed=seed, scenario=Scenario.MIXED, timeframe=timeframe)

    assert_has_gap_features(candles, timeframe)
    assert_has_flat_run(candles)
    assert_has_extreme_features(candles)


# --- T12: determinism (AC12) -----------------------------------------------------------


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_same_arguments_give_identical_frames(scenario: Scenario) -> None:
    first = synthetic_candles(250, seed=21, scenario=scenario, timeframe="1h")
    second = synthetic_candles(250, seed=21, scenario=scenario, timeframe="1h")

    pd.testing.assert_frame_equal(first, second, check_exact=True)


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_different_seeds_give_different_closes(scenario: Scenario) -> None:
    first = synthetic_candles(250, seed=1, scenario=scenario)
    second = synthetic_candles(250, seed=2, scenario=scenario)

    assert not np.array_equal(first["close"].to_numpy(), second["close"].to_numpy())


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_global_random_state_is_neither_used_nor_changed(scenario: Scenario) -> None:
    random.seed(1)
    np.random.seed(1)  # the legacy global state is what must not matter
    first = synthetic_candles(250, seed=4, scenario=scenario)

    random.seed(2)
    np.random.seed(2)  # the legacy global state is what must not matter
    python_state = random.getstate()
    numpy_state = np.random.get_state()  # reading the legacy global state
    second = synthetic_candles(250, seed=4, scenario=scenario)

    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert random.getstate() == python_state
    after = np.random.get_state()  # reading the legacy global state
    assert after[0] == numpy_state[0]
    assert np.array_equal(after[1], numpy_state[1])
    assert after[2:] == numpy_state[2:]


# --- T13: argument validation (AC13) ---------------------------------------------------


@pytest.mark.parametrize("n", [0, -5])
def test_rejects_non_positive_sizes(n: int) -> None:
    with pytest.raises(ValueError, match="n must be"):
        synthetic_candles(n)


def test_rejects_unknown_timeframes() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        synthetic_candles(10, timeframe="15m")  # type: ignore[arg-type]


def test_rejects_unknown_scenarios() -> None:
    with pytest.raises(ValueError, match="scenario"):
        synthetic_candles(10, scenario="sideways")


def test_accepts_scenario_values_as_strings() -> None:
    candles = synthetic_candles(80, seed=2, scenario="gaps")

    assert_has_gap_features(candles, "1d")


@pytest.mark.parametrize("volatility", [-0.01, float("nan"), float("inf")])
def test_rejects_invalid_volatility(volatility: float) -> None:
    with pytest.raises(ValueError, match="volatility"):
        synthetic_candles(10, volatility=volatility)


@pytest.mark.parametrize("start_price", [0.0, -1.0, MIN_PRICE / 2, MAX_PRICE * 2, float("nan")])
def test_rejects_start_prices_outside_the_bounds(start_price: float) -> None:
    with pytest.raises(ValueError, match="start_price"):
        synthetic_candles(10, start_price=start_price)


@pytest.mark.parametrize(
    ("start", "timeframe"),
    [
        pytest.param("2024-01-01T00:00:00", "1d", id="tz-naive"),
        pytest.param("2024-01-01T01:00:00+01:00", "1h", id="not-utc-offset"),
        pytest.param(pd.Timestamp("2024-01-01", tz="Europe/London"), "1d", id="not-utc-zone"),
        pytest.param("2024-01-01T00:30:00+00:00", "1h", id="off-hour-grid"),
        pytest.param("2024-01-01T02:00:00+00:00", "4h", id="off-4h-grid"),
        pytest.param("2024-01-01T12:00:00+00:00", "1d", id="off-day-grid"),
    ],
)
def test_rejects_starts_that_are_not_utc_grid_open_times(
    start: str | pd.Timestamp, timeframe: TimeframeCode
) -> None:
    with pytest.raises(ValueError, match="start"):
        synthetic_candles(10, start=start, timeframe=timeframe)


def test_default_start_is_a_monday_on_every_grid() -> None:
    start = pd.Timestamp(DEFAULT_START)

    assert start.dayofweek == 0
    assert all(start == start.floor(duration(timeframe)) for timeframe in Timeframe)


# --- Shared assertion helpers fail loudly (AC17b) ----------------------------------------------


def test_assert_valid_candles_reports_contract_violations_as_assertion_errors() -> None:
    candles = synthetic_candles(10, seed=1)
    candles.iloc[3, candles.columns.get_loc("close")] = np.nan

    with pytest.raises(AssertionError, match="missing_value"):
        assert_valid_candles(candles, "1d")


def test_assert_valid_candles_rejects_labels_off_the_grid() -> None:
    candles = synthetic_candles(10, seed=1, timeframe="1h")
    candles = candles.set_axis(candles.index + pd.Timedelta(minutes=30))

    with pytest.raises(AssertionError, match="grid"):
        assert_valid_candles(candles, Timeframe.H1)


@pytest.mark.parametrize("price", [MIN_PRICE / 2, MAX_PRICE * 2])
def test_assert_valid_candles_rejects_prices_outside_the_bounds(price: float) -> None:
    candles = synthetic_candles(10, seed=1, volatility=0.0)
    candles.loc[:, ["open", "high", "low", "close"]] = price

    with pytest.raises(AssertionError, match="bounds"):
        assert_valid_candles(candles, "1d")


def test_assert_valid_candles_rejects_non_datetime_indexes_without_crashing() -> None:
    candles = synthetic_candles(10, seed=1).reset_index(drop=True)

    with pytest.raises(AssertionError, match="index_type"):
        assert_valid_candles(candles, "1d")


def test_feature_assertions_fail_on_a_plain_random_walk() -> None:
    candles = synthetic_candles(FEATURE_MIN_CANDLES, seed=1)

    with pytest.raises(AssertionError, match="weekend"):
        assert_has_gap_features(candles, "1d")
    with pytest.raises(AssertionError, match="flat run"):
        assert_has_flat_run(candles)
    with pytest.raises(AssertionError, match="volume"):
        assert_has_extreme_volume_features(candles)
    with pytest.raises(AssertionError, match="-90%"):
        assert_has_extreme_features(candles)


def test_gap_assertion_requires_a_weekday_drop_and_a_price_jump() -> None:
    gaps = synthetic_candles(FEATURE_MIN_CANDLES, seed=1, scenario=Scenario.GAPS)
    no_jump = synthetic_candles(FEATURE_MIN_CANDLES, seed=1, scenario=Scenario.GAPS, volatility=0.0)
    only_weekend_gaps = gaps.set_axis(
        pd.bdate_range("2024-01-01", periods=FEATURE_MIN_CANDLES, tz="UTC")
    )

    assert_has_gap_features(gaps, "1d")
    with pytest.raises(AssertionError, match="price"):
        assert_has_gap_features(no_jump, "1d")
    with pytest.raises(AssertionError, match="weekday"):
        assert_has_gap_features(only_weekend_gaps, "1d")


def test_extreme_assertion_requires_the_spike_candle() -> None:
    candles = synthetic_candles(FEATURE_MIN_CANDLES, seed=1, scenario=Scenario.EXTREME)
    spikes = candles["close"] >= 10 * candles["open"]
    candles.loc[spikes, "close"] = candles.loc[spikes, "open"]

    with pytest.raises(AssertionError, match="900%"):
        assert_has_extreme_features(candles)

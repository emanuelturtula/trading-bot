"""Stable-warmup window-start invariance (spec 005, T15; AC10).

A value computed on a rolling fetch window whose start moves differs from the long-history value
until the recursive seed has decayed (Design 8). ``stable_warmup`` is the frame length after
which that difference is small: this file checks it by comparing, at several candles ``t``, the
last value of an indicator computed on exactly ``stable_warmup`` candles ending at ``t`` with the
value at ``t`` computed on the whole 3000-candle frame.

``obv`` is handled separately (AC10's last bullet): its level depends on the first candle of the
fetched history for any history length, so only ``value - signal`` is compared this way; the
``value`` output itself is checked for a constant shift between two windows with different
starts, on the same positions, rather than for stability against the whole frame.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from trading_bot.domain.indicators.catalog import REGISTRY

SEEDS = range(5)
SCENARIOS = (Scenario.RANDOM_WALK, Scenario.GAPS)
FRAME_LENGTH = 3000
CASES = ("default", "large")

# AC10's "length=50" parameter set (macd fast=50/slow=100/signal=30; obv signal=50). Test files
# never import from one another (tests/unit/test_candle_strategies.py enforces it), so this
# duplicates the small dict also used, independently, by test_indicator_lookahead_properties.py.
LARGE_PARAMS: dict[str, dict[str, object]] = {
    "sma": {"length": 50},
    "volume_sma": {"length": 50},
    "ema": {"length": 50},
    "bbands": {"length": 50},
    "rsi": {"length": 50},
    "atr": {"length": 50},
    "adx": {"length": 50},
    "macd": {"fast": 50, "slow": 100, "signal": 30},
    "stoch": {"length": 30, "smooth_k": 5, "smooth_d": 5},
    "obv": {"signal": 50},
}

# Indicators whose value is compared within 1e-9 * scale (AC7): they settle immediately
# (``settle = 0``), so a moving window should barely move the result at all.
EXACT_INDICATORS = frozenset({"sma", "bbands", "stoch", "volume_sma"})
# rsi and adx are 0-100 scores: an absolute tolerance, not a relative one, per AC10.
HUNDRED_SCALE_INDICATORS = frozenset({"rsi", "adx"})
NON_OBV_NAMES = tuple(name for name in REGISTRY.names if name != "obv")


def params_for(case: str, name: str) -> dict[str, object]:
    return {} if case == "default" else LARGE_PARAMS[name]


def _scale(name: str, frame: pd.DataFrame) -> float:
    """AC7's ``scale``: bounds the magnitude of the compared output, in its own unit."""
    if name in ("rsi", "stoch", "adx"):
        return 100.0
    if name == "obv":
        return max(1.0, float(frame["volume"].sum()))
    if name == "volume_sma":
        return max(1.0, float(frame["volume"].max()))
    return float(frame["high"].max())


def _tolerance(name: str, close_t: float, scale: float) -> float:
    if name in HUNDRED_SCALE_INDICATORS:
        return 0.1
    if name in EXACT_INDICATORS:
        return 1e-9 * scale
    return 1e-3 * abs(close_t)  # ema, atr, macd: price-unit outputs


def _anchors(n: int, stable_warmup: int) -> list[int]:
    """A handful of candle positions from just after ``stable_warmup`` to the last candle."""
    candidates = {stable_warmup + 20, n // 4, n // 2, (3 * n) // 4, n - 1}
    return sorted(t for t in candidates if stable_warmup - 1 <= t <= n - 1)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("name", NON_OBV_NAMES)
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", SEEDS)
def test_stable_warmup_window_matches_the_whole_frame(
    seed: int, scenario: Scenario, name: str, case: str
) -> None:
    frame = synthetic_candles(FRAME_LENGTH, seed=seed, scenario=scenario)
    resolved = REGISTRY.validate_params(name, params_for(case, name))
    stable_warmup = REGISTRY.stable_warmup(name, resolved)
    full_result = REGISTRY.compute(name, resolved, frame)
    close = frame["close"].to_numpy()
    scale = _scale(name, frame)
    anchors = _anchors(len(frame), stable_warmup)
    assert anchors, f"no anchor fits stable_warmup={stable_warmup} in a {len(frame)}-candle frame"

    compared = 0
    for t in anchors:
        window = frame.iloc[t - stable_warmup + 1 : t + 1]
        window_result = REGISTRY.compute(name, resolved, window)
        for output in full_result:
            windowed_value = window_result[output].iloc[-1]
            full_value = full_result[output].iloc[t]
            if math.isnan(windowed_value) or math.isnan(full_value):
                continue
            tolerance = _tolerance(name, close[t], scale)
            difference = abs(windowed_value - full_value)
            assert difference <= tolerance, (
                f"{name}.{output} at t={t} (seed={seed}, {scenario}, {case}): windowed="
                f"{windowed_value}, whole-frame={full_value}, difference={difference}, "
                f"allowed={tolerance}"
            )
            compared += 1
    assert compared > 0, f"no finite value was ever compared for {name} ({case})"


# --- obv: value - signal is window-start invariant; value shifts by a constant (AC10) --------


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", SEEDS)
def test_obv_value_minus_signal_is_stable_across_windows(
    seed: int, scenario: Scenario, case: str
) -> None:
    frame = synthetic_candles(FRAME_LENGTH, seed=seed, scenario=scenario)
    resolved = REGISTRY.validate_params("obv", params_for(case, "obv"))
    stable_warmup = REGISTRY.stable_warmup("obv", resolved)
    full_result = REGISTRY.compute("obv", resolved, frame)
    scale = _scale("obv", frame)
    anchors = _anchors(len(frame), stable_warmup)
    assert anchors

    compared = 0
    for t in anchors:
        window = frame.iloc[t - stable_warmup + 1 : t + 1]
        window_result = REGISTRY.compute("obv", resolved, window)
        windowed_spread = window_result["value"].iloc[-1] - window_result["signal"].iloc[-1]
        full_spread = full_result["value"].iloc[t] - full_result["signal"].iloc[t]
        if math.isnan(windowed_spread) or math.isnan(full_spread):
            continue
        difference = abs(windowed_spread - full_spread)
        assert difference <= 1e-9 * scale, (
            f"obv value - signal at t={t} (seed={seed}, {scenario}, {case}): windowed="
            f"{windowed_spread}, whole-frame={full_spread}, difference={difference}"
        )
        compared += 1
    assert compared > 0


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("seed", SEEDS)
def test_obv_value_shifts_by_one_constant_when_the_window_starts_later(
    seed: int, scenario: Scenario, case: str
) -> None:
    offset = 500
    frame = synthetic_candles(FRAME_LENGTH, seed=seed, scenario=scenario)
    resolved = REGISTRY.validate_params("obv", params_for(case, "obv"))
    lookback = REGISTRY.lookback("obv", resolved)
    full_result = REGISTRY.compute("obv", resolved, frame)
    later_frame = frame.iloc[offset:]
    later_result = REGISTRY.compute("obv", resolved, later_frame)
    scale = _scale("obv", frame)

    shifts = []
    for position in range(lookback, len(later_frame)):
        later_value = later_result["value"].iloc[position]
        whole_value = full_result["value"].iloc[offset + position]
        if math.isnan(later_value) or math.isnan(whole_value):
            continue
        shifts.append(later_value - whole_value)

    assert shifts, "no finite obv value was ever compared"
    spread = max(shifts) - min(shifts)
    assert spread <= 1e-9 * scale, (
        f"obv value shift is not constant (seed={seed}, {scenario}, {case}): spread={spread}, "
        f"allowed={1e-9 * scale}"
    )

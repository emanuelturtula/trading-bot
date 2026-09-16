"""Hypothesis strategies for candle frames (spec 003, Design 5).

The strategy draws generator **parameters** and delegates to ``synthetic_candles``, so validity
lives in one code path and draws shrink toward ``min_size``, ``RANDOM_WALK``, seed 0 and
``volatility=0.0``. Always import this module as ``tests.fixtures.strategies``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd
from hypothesis import strategies as st

from tests.fixtures.candles import (
    ALL_SCENARIOS,
    DEFAULT_START,
    TIMEFRAMES,
    Scenario,
    TimeframeCode,
    synthetic_candles,
)

MAX_SEED = 2**32 - 1
MAX_START_OFFSET = 5000  # grid steps after DEFAULT_START
MIN_PRICE_EXPONENT = -3.0
MAX_PRICE_EXPONENT = 5.0

_DURATIONS: dict[str, pd.Timedelta] = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
}


def candle_frames(
    *,
    min_size: int = 2,
    max_size: int = 120,
    scenarios: Sequence[Scenario] = ALL_SCENARIOS,
    timeframes: Sequence[TimeframeCode] = TIMEFRAMES,
    max_volatility: float = 0.25,
) -> st.SearchStrategy[pd.DataFrame]:
    """Strategy of valid synthetic candle frames; invalid arguments raise ``ValueError`` now."""
    if min_size < 1:
        raise ValueError(f"min_size must be >= 1, got {min_size}")
    if max_size < min_size:
        raise ValueError(f"max_size must be >= min_size ({min_size}), got {max_size}")
    if not scenarios:
        raise ValueError("scenarios must not be empty")
    scenario_options = tuple(_scenario(scenario) for scenario in scenarios)
    if not timeframes:
        raise ValueError("timeframes must not be empty")
    for timeframe in timeframes:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {TIMEFRAMES}")
    timeframe_options = tuple(timeframes)
    if not (math.isfinite(max_volatility) and max_volatility >= 0.0):
        raise ValueError(f"max_volatility must be a finite number >= 0, got {max_volatility!r}")

    @st.composite
    def frames(draw: st.DrawFn) -> pd.DataFrame:
        n = draw(st.integers(min_value=min_size, max_value=max_size))
        scenario = draw(st.sampled_from(scenario_options))
        timeframe = draw(st.sampled_from(timeframe_options))
        seed = draw(st.integers(min_value=0, max_value=MAX_SEED))
        offset = draw(st.integers(min_value=0, max_value=MAX_START_OFFSET))
        exponent = draw(st.floats(min_value=MIN_PRICE_EXPONENT, max_value=MAX_PRICE_EXPONENT))
        volatility = draw(st.floats(min_value=0.0, max_value=max_volatility))
        return synthetic_candles(
            n,
            seed=seed,
            scenario=scenario,
            timeframe=timeframe,
            start=pd.Timestamp(DEFAULT_START) + offset * _DURATIONS[timeframe],
            start_price=10.0**exponent,
            volatility=volatility,
        )

    return frames()


def _scenario(scenario: Scenario | str) -> Scenario:
    try:
        return Scenario(scenario)
    except ValueError:
        expected = [item.value for item in Scenario]
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {expected}") from None

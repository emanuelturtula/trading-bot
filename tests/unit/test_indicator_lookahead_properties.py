"""Extended look-ahead checks: full sweeps and a Hypothesis property (spec 005, T14; AC11).

``test_indicator_lookahead.py`` (the developer's T10) already checks every indicator on every
``Scenario`` at default parameters with a thinned-down cut selection. This file adds two more
layers: a full sweep (``max_cuts=len(candles)``, no cut skipped) with a large parameter set on a
mixed-scenario frame, and a Hypothesis property drawing the indicator, its parameters and the
frame, so the check does not depend on any one hand-picked case. Comparison stays exact
(``rtol=atol=0``): CLAUDE.md rule 4 allows no tolerance here.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.strategies import candle_frames
from tests.lookahead import assert_no_lookahead
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import ParamKind, ParamSpec
from trading_bot.domain.indicators.spec import IndicatorSpec

# The T9 "large" parameter set of every indicator (spec 005, Test plan T9 row), independent of
# the developer's tests: both come from the same spec table.
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


def computing(
    name: str, params: dict[str, object]
) -> Callable[[pd.DataFrame], dict[str, pd.Series[float]]]:
    """A closure over one ``(name, params)``, as the look-ahead harness expects."""

    def compute(candles: pd.DataFrame) -> dict[str, pd.Series[float]]:
        return REGISTRY.compute(name, params, candles)

    compute.__qualname__ = f"compute_{name}"
    return compute


def test_large_params_cover_every_indicator() -> None:
    assert set(LARGE_PARAMS) == set(REGISTRY.names)


# --- Full sweep on a mixed-scenario frame, no cut skipped (AC11) ----------------------------


@pytest.mark.parametrize("name", REGISTRY.names)
def test_full_sweep_with_a_large_parameter_set_on_a_mixed_scenario(name: str) -> None:
    candles = synthetic_candles(200, seed=97, scenario=Scenario.MIXED)

    report = assert_no_lookahead(
        computing(name, LARGE_PARAMS[name]), candles, max_cuts=len(candles)
    )

    assert report.cuts == tuple(range(1, len(candles)))
    assert report.non_missing_values > 0


# --- Hypothesis property: indicator, parameters and frame all drawn (AC11) ------------------

_CAP = 30  # every drawn integer parameter stays within [minimum, min(maximum, 30)]


def _param_strategy(param: ParamSpec) -> st.SearchStrategy[int | float]:
    if param.kind is ParamKind.INT:
        assert isinstance(param.minimum, int)
        assert isinstance(param.maximum, int)
        return st.integers(min_value=param.minimum, max_value=min(param.maximum, _CAP))
    return st.floats(
        min_value=param.minimum, max_value=param.maximum, allow_nan=False, allow_infinity=False
    )


@st.composite
def _macd_params(draw: st.DrawFn, spec: IndicatorSpec) -> dict[str, object]:
    fast_spec, slow_spec, signal_spec = spec.params
    assert isinstance(fast_spec.minimum, int)
    assert isinstance(slow_spec.maximum, int)
    fast_max = min(fast_spec.maximum, _CAP - 1)
    fast = draw(st.integers(min_value=fast_spec.minimum, max_value=fast_max))
    slow_min = max(int(slow_spec.minimum), fast + 1)
    slow = draw(st.integers(min_value=slow_min, max_value=min(slow_spec.maximum, _CAP)))
    signal = draw(_param_strategy(signal_spec))
    return {"fast": fast, "slow": slow, "signal": signal}


@st.composite
def _indicator_and_params(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    name = draw(st.sampled_from(REGISTRY.names))
    spec = REGISTRY.get(name)
    if name == "macd":
        params = draw(_macd_params(spec))
    else:
        params = {param.name: draw(_param_strategy(param)) for param in spec.params}
    return name, params


@st.composite
def _lookahead_case(draw: st.DrawFn) -> tuple[str, dict[str, object], pd.DataFrame]:
    name, params = draw(_indicator_and_params())
    warmup = REGISTRY.warmup(name, params)
    frame = draw(candle_frames(min_size=warmup + 5, max_size=warmup + 60))
    return name, params, frame


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_lookahead_case())
def test_lookahead_property_over_indicator_params_and_frame(
    case: tuple[str, dict[str, object], pd.DataFrame],
) -> None:
    name, params, frame = case
    resolved = REGISTRY.validate_params(name, params)
    result = REGISTRY.compute(name, resolved, frame)
    assume(any(series.notna().any() for series in result.values()))  # skip all-NaN (flat) draws

    assert_no_lookahead(computing(name, resolved), frame, max_cuts=10)

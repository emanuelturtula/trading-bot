"""Adversarial parameters and frames through the real catalog (spec 005, T17 and T18; AC2, AC4,
AC5, AC8).

``test_indicator_params.py`` (the developer's T1) already drills ``validate_params`` directly
with hostile input; this file re-probes the same kinds of adversarial values through
``REGISTRY`` (the public entry point rule evaluation actually uses) and through real TA-Lib
kernels, plus a handful of frame shapes the developer's tests do not build (empty and
one-candle frames, alternate index units, ``attrs``, extreme prices and volume, and the
stochastic zero-range boundary).
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.candles import synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.errors import IndicatorErrorKind, InvalidParameterError
from trading_bot.domain.indicators.params import ParamKind, ParamSpec

HOSTILE_NAME = ("\n\x1b[31m" + "n" * 60) * 200  # 12 000+ characters, newlines and ANSI escapes
HOSTILE_VALUE = ("\r\n\x1b]0;" + "v" * 60) * 200


def _assert_bounded_message(message: str) -> None:
    assert "\n" not in message
    assert "\r" not in message
    assert "\x1b" not in message
    assert len(message) < 300


# --- T17: adversarial parameters, through the real registry ---------------------------------


def test_hostile_parameter_name_through_the_registry_is_bounded() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params("rsi", {HOSTILE_NAME: 1})

    assert caught.value.kind is IndicatorErrorKind.UNKNOWN_PARAMETER
    assert caught.value.parameter == HOSTILE_NAME[:32]
    _assert_bounded_message(str(caught.value))


def test_hostile_parameter_value_through_the_registry_is_bounded() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params("rsi", {"length": HOSTILE_VALUE})

    assert caught.value.kind is IndicatorErrorKind.WRONG_TYPE
    _assert_bounded_message(str(caught.value))


@pytest.mark.parametrize(
    ("name", "params"), [("rsi", {"length": 10**100}), ("macd", {"fast": 10**100})]
)
def test_huge_integer_is_out_of_range_through_the_registry(
    name: str, params: dict[str, object]
) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params(name, params)

    assert caught.value.kind is IndicatorErrorKind.OUT_OF_RANGE


def test_huge_integer_for_a_float_parameter_through_the_registry() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params("bbands", {"std": 10**400})

    assert caught.value.kind is IndicatorErrorKind.OUT_OF_RANGE


@pytest.mark.parametrize("value", [np.bool_(True), np.float32(2.0), Fraction(1, 2)])
def test_bbands_std_rejects_bool_and_normalizes_numpy_and_fraction_like_values(
    value: object,
) -> None:
    if isinstance(value, np.bool_):
        with pytest.raises(InvalidParameterError) as caught:
            REGISTRY.validate_params("bbands", {"std": value})
        assert caught.value.kind is IndicatorErrorKind.WRONG_TYPE
    else:
        result = REGISTRY.validate_params("bbands", {"std": value})
        assert type(result["std"]) is float


@pytest.mark.parametrize("value", [-0.0, 5e-324])
def test_bbands_std_boundary_floats_are_out_of_range(value: float) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params("bbands", {"std": value})

    assert caught.value.kind is IndicatorErrorKind.OUT_OF_RANGE


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("rsi", {"length": [14]}),
        ("rsi", {"length": (14,)}),
        ("bbands", {"std": {"nested": 1}}),
        ("obv", {"signal": {2, 3}}),
        ("stoch", {"length": [[1, 2]]}),
    ],
)
def test_nested_containers_as_parameter_values_are_rejected(
    name: str, params: dict[str, object]
) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params(name, params)

    assert caught.value.kind is IndicatorErrorKind.WRONG_TYPE


@pytest.mark.parametrize("name", REGISTRY.names)
def test_an_indicator_params_result_can_be_validated_and_computed_again(name: str) -> None:
    frame = synthetic_candles(40, seed=3)
    params = REGISTRY.validate_params(name, {})

    revalidated = REGISTRY.validate_params(name, params)
    result = REGISTRY.compute(name, params, frame)

    assert revalidated == params
    assert list(result) == list(REGISTRY.get(name).output_names)


def test_indicator_params_from_an_incompatible_indicator_is_rejected_not_misapplied() -> None:
    """An ``IndicatorParams`` built for one indicator must not silently work for another whose
    parameters differ: the registry must validate it against the target's own declaration.
    """
    rsi_params = REGISTRY.validate_params("rsi", {"length": 21})

    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params("macd", rsi_params)

    assert caught.value.kind is IndicatorErrorKind.UNKNOWN_PARAMETER
    assert caught.value.parameter == "length"


def test_indicator_params_reused_across_indicators_with_the_same_param_declaration() -> None:
    """``sma`` and ``volume_sma`` declare the same single ``length`` parameter (name, kind,
    default, range): an ``IndicatorParams`` built for one validates and computes for the other,
    reading whichever column the target indicator's own inputs declare.
    """
    frame = synthetic_candles(40, seed=3)
    params = REGISTRY.validate_params("sma", {"length": 10})

    revalidated = REGISTRY.validate_params("volume_sma", params)
    result = REGISTRY.compute("volume_sma", params, frame)

    assert revalidated == params
    np.testing.assert_allclose(
        result["value"].to_numpy()[9:],
        frame["volume"].rolling(10).mean().to_numpy()[9:],
        rtol=0.0,
        atol=1e-9,
    )


def _param_value_strategy(param: ParamSpec) -> st.SearchStrategy[object]:
    if param.kind is ParamKind.INT:
        assert isinstance(param.minimum, int)
        assert isinstance(param.maximum, int)
        return st.integers(min_value=param.minimum, max_value=param.maximum)
    return st.floats(
        min_value=param.minimum, max_value=param.maximum, allow_nan=False, allow_infinity=False
    )


@st.composite
def _valid_params_for_every_indicator(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    name = draw(st.sampled_from(REGISTRY.names))
    spec = REGISTRY.get(name)
    if name == "macd":
        fast_spec, slow_spec, signal_spec = spec.params
        assert isinstance(fast_spec.maximum, int)
        assert isinstance(slow_spec.minimum, int)
        fast = draw(st.integers(min_value=fast_spec.minimum, max_value=fast_spec.maximum - 1))
        slow = draw(
            st.integers(min_value=max(slow_spec.minimum, fast + 1), max_value=slow_spec.maximum)
        )
        signal = draw(_param_value_strategy(signal_spec))
        return name, {"fast": fast, "slow": slow, "signal": signal}
    return name, {param.name: draw(_param_value_strategy(param)) for param in spec.params}


@settings(max_examples=100, deadline=None)
@given(case=_valid_params_for_every_indicator())
def test_validate_params_is_idempotent_over_drawn_valid_parameters(
    case: tuple[str, dict[str, object]],
) -> None:
    name, params = case

    first = REGISTRY.validate_params(name, params)
    second = REGISTRY.validate_params(name, first)

    assert second == first
    assert list(second.items()) == list(first.items())


# --- T18: adversarial frames ------------------------------------------------------------------


@pytest.mark.parametrize("name", REGISTRY.names)
def test_length_zero_and_one_frames_give_all_nan_outputs(name: str) -> None:
    empty = candles_from_prices([])
    one = synthetic_candles(1, seed=1)

    for frame in (empty, one):
        result = REGISTRY.compute(name, {}, frame)
        assert list(result) == list(REGISTRY.get(name).output_names)
        for output, series in result.items():
            assert len(series) == len(frame), output
            assert series.dtype == np.float64
            assert series.isna().all(), output


@pytest.mark.parametrize("name", REGISTRY.names)
def test_frames_of_exactly_lookback_and_warmup_candles(name: str) -> None:
    lookback = REGISTRY.lookback(name, {})
    warmup = REGISTRY.warmup(name, {})
    frame = synthetic_candles(warmup, seed=5)

    at_lookback = REGISTRY.compute(name, {}, frame.iloc[:lookback])
    at_warmup = REGISTRY.compute(name, {}, frame)

    for output, series in at_lookback.items():
        assert series.isna().all(), f"{name}.{output} at exactly lookback candles"
    for output, series in at_warmup.items():
        assert np.isfinite(series.iloc[-1]), f"{name}.{output} at exactly warmup candles"


@pytest.mark.parametrize("unit", ["s", "ms"])
def test_index_unit_and_attrs_survive_computation_untouched(unit: str) -> None:
    frame = synthetic_candles(60, seed=2)
    frame = frame.copy()
    frame.index = frame.index.as_unit(unit)
    frame.attrs["source"] = "adversarial-test"
    before_attrs = dict(frame.attrs)

    result = REGISTRY.compute("rsi", {}, frame)

    assert result["value"].index.dtype == frame.index.dtype
    assert frame.attrs == before_attrs


@pytest.mark.parametrize("start_price", [1e-6, 1e9])
@pytest.mark.parametrize("name", REGISTRY.names)
def test_extreme_prices_give_finite_never_infinite_outputs(name: str, start_price: float) -> None:
    frame = synthetic_candles(120, seed=4, start_price=start_price)
    lookback = REGISTRY.lookback(name, {})

    result = REGISTRY.compute(name, {}, frame)

    for output, series in result.items():
        values = series.to_numpy()
        assert not np.isinf(values).any(), f"{name}.{output}"
        assert np.isfinite(values[lookback:]).all(), f"{name}.{output}"


def test_extreme_volume_gives_finite_obv_and_volume_sma() -> None:
    frame = candles_from_prices([float(c) for c in range(1, 60)], volume=[1e15] * 59)

    obv = REGISTRY.compute("obv", {}, frame)
    volume_sma = REGISTRY.compute("volume_sma", {}, frame)

    assert np.isfinite(obv["value"].to_numpy()[19:]).all()
    assert np.isfinite(obv["signal"].to_numpy()[19:]).all()
    assert np.isfinite(volume_sma["value"].to_numpy()[19:]).all()
    np.testing.assert_allclose(volume_sma["value"].to_numpy()[19:], 1e15, rtol=1e-12)


def test_stoch_length_one_on_flat_candles_is_undefined_throughout() -> None:
    frame = candles_from_prices([5.0] * 20)

    result = REGISTRY.compute("stoch", {"length": 1, "smooth_k": 1, "smooth_d": 1}, frame)

    assert result["k"].isna().all()
    assert result["d"].isna().all()


def test_macd_signal_one_means_signal_equals_macd_and_hist_is_zero() -> None:
    frame = synthetic_candles(80, seed=6)

    result = REGISTRY.compute("macd", {"fast": 5, "slow": 10, "signal": 1}, frame)

    macd_line = result["macd"].to_numpy()
    signal_line = result["signal"].to_numpy()
    hist = result["hist"].to_numpy()
    finite = ~np.isnan(macd_line)
    assert finite.any()
    np.testing.assert_allclose(signal_line[finite], macd_line[finite], rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(hist[finite], 0.0, atol=1e-9)


def test_atr_length_one_equals_the_true_range() -> None:
    high = [11.0, 12.0, 13.0, 12.0, 15.0]
    low = [9.0, 10.0, 11.0, 8.0, 9.0]
    close = [10.0, 11.0, 12.0, 9.0, 14.0]
    frame = candles_from_prices(close, high=high, low=low)
    expected_true_range = [
        np.nan,
        max(12 - 10, abs(12 - 10), abs(10 - 10)),
        max(13 - 11, abs(13 - 11), abs(11 - 11)),
        max(12 - 8, abs(12 - 12), abs(8 - 12)),
        max(15 - 9, abs(15 - 9), abs(9 - 9)),
    ]

    result = REGISTRY.compute("atr", {"length": 1}, frame)

    np.testing.assert_allclose(
        result["value"].to_numpy(), expected_true_range, rtol=0.0, atol=1e-12, equal_nan=True
    )


def test_stoch_masks_a_window_with_relative_range_below_the_talib_floor() -> None:
    # A high-low range of 1e-15 relative to the price level (1.0) sits below TA-Lib's 1e-14
    # scale-relative zero test (spec 7): this window must be undefined, not a tiny finite %K.
    below_floor_high = 1.0 + 5e-16
    below_floor_low = 1.0 - 5e-16
    high = [1.5, 1.4, 1.3, below_floor_high, 2.0, 2.1]
    low = [0.5, 0.6, 0.7, below_floor_low, 1.0, 1.1]
    close = [1.0, 1.0, 1.0, 1.0, 1.5, 1.6]
    frame = candles_from_prices(close, high=high, low=low)

    result = REGISTRY.compute("stoch", {"length": 1, "smooth_k": 1, "smooth_d": 1}, frame)

    assert np.isnan(result["k"].iloc[3])
    assert np.isfinite(result["k"].iloc[0])
    assert np.isfinite(result["k"].iloc[4])


def test_computed_series_share_memory_with_neither_each_other_nor_the_input() -> None:
    frame = synthetic_candles(80, seed=8)
    before = frame.copy(deep=True)

    result = REGISTRY.compute("macd", {}, frame)
    macd_values = result["macd"].to_numpy()
    signal_values = result["signal"].to_numpy()
    hist_values = result["hist"].to_numpy()

    assert not np.shares_memory(macd_values, signal_values)
    assert not np.shares_memory(macd_values, hist_values)
    assert not np.shares_memory(signal_values, hist_values)
    for column in frame.columns:
        assert not np.shares_memory(macd_values, frame[column].to_numpy())

    result["macd"].iloc[-1] = -1.0
    result["hist"].iloc[-1] = -1.0
    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    again = REGISTRY.compute("macd", {}, frame)
    assert again["macd"].iloc[-1] != -1.0
    assert again["hist"].iloc[-1] != -1.0

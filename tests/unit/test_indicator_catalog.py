"""Tests of the default indicator catalog on TA-Lib (spec 005, T6-T9 and T11).

T6 pins the user-visible metadata of Design 6.1 and 10, T7 the golden values of Design 6.4, T8
the undefined values of Design 7, T9 the lookback and warmup formulas of Design 6.1 and 8, and
T11 the unstable-period guard of Design 9.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest
import talib

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from trading_bot.domain.indicators.catalog import CATALOG, REGISTRY
from trading_bot.domain.indicators.errors import (
    IndicatorComputationError,
    IndicatorErrorKind,
    InvalidOutputError,
    InvalidParameterError,
    UnknownIndicatorError,
)
from trading_bot.domain.indicators.params import ParamKind
from trading_bot.domain.indicators.registry import IndicatorRegistry

NAMES = ("sma", "ema", "rsi", "macd", "bbands", "atr", "adx", "stoch", "obv", "volume_sma")
nan = math.nan

# --- T6: catalog metadata (AC1) -------------------------------------------------------------

WINDOW = "Candles in the window."
SMOOTHING = "Smoothing period in candles."

# name -> (label, inputs, params, constraints, outputs, default output, description)
# params: (name, kind, default, minimum, maximum, label, description)
EXPECTED_METADATA: dict[str, tuple[object, ...]] = {
    "sma": (
        "SMA",
        ("close",),
        (("length", ParamKind.INT, 20, 2, 500, "Length", WINDOW),),
        (),
        (("value", "SMA"),),
        "value",
        "Simple moving average of the close over the last length candles.",
    ),
    "ema": (
        "EMA",
        ("close",),
        (("length", ParamKind.INT, 20, 2, 500, "Length", SMOOTHING),),
        (),
        (("value", "EMA"),),
        "value",
        "Exponential moving average of the close with smoothing 2 / (length + 1), seeded with "
        "the simple average of the first length closes.",
    ),
    "rsi": (
        "RSI",
        ("close",),
        (("length", ParamKind.INT, 14, 2, 100, "Length", SMOOTHING),),
        (),
        (("value", "RSI"),),
        "value",
        "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined "
        "while the close has not changed since the first candle.",
    ),
    "macd": (
        "MACD",
        ("close",),
        (
            (
                "fast",
                ParamKind.INT,
                12,
                2,
                100,
                "Fast length",
                "Period of the fast EMA in candles.",
            ),
            (
                "slow",
                ParamKind.INT,
                26,
                3,
                200,
                "Slow length",
                "Period of the slow EMA in candles.",
            ),
            (
                "signal",
                ParamKind.INT,
                9,
                1,
                100,
                "Signal length",
                "Period of the signal line EMA in candles.",
            ),
        ),
        (("fast", "slow"),),
        (("macd", "MACD line"), ("signal", "Signal line"), ("hist", "Histogram")),
        None,
        "Fast EMA minus slow EMA of the close (MACD line), an EMA of that line (signal line) and "
        "their difference (histogram).",
    ),
    "bbands": (
        "Bollinger Bands",
        ("close",),
        (
            ("length", ParamKind.INT, 20, 2, 500, "Length", WINDOW),
            (
                "std",
                ParamKind.FLOAT,
                2.0,
                0.1,
                5.0,
                "Standard deviations",
                "Distance of the bands from the middle band, in population standard deviations.",
            ),
        ),
        (),
        (("lower", "Lower band"), ("middle", "Middle band"), ("upper", "Upper band")),
        None,
        "Simple moving average of the close (middle band) plus and minus std population standard "
        "deviations over the last length candles.",
    ),
    "atr": (
        "ATR",
        ("high", "low", "close"),
        (("length", ParamKind.INT, 14, 1, 100, "Length", SMOOTHING),),
        (),
        (("value", "ATR"),),
        "value",
        "Wilder's average true range over length candles, in price units.",
    ),
    "adx": (
        "ADX",
        ("high", "low", "close"),
        (("length", ParamKind.INT, 14, 2, 100, "Length", SMOOTHING),),
        (),
        (("value", "ADX"),),
        "value",
        "Wilder's average directional index, from 0 to 100: trend strength regardless of "
        "direction. Undefined until the first candle with directional movement.",
    ),
    "stoch": (
        "Stochastic",
        ("high", "low", "close"),
        (
            ("length", ParamKind.INT, 14, 1, 100, "%K length", "Candles in the high-low range."),
            (
                "smooth_k",
                ParamKind.INT,
                3,
                1,
                100,
                "%K smoothing",
                "Candles averaged to smooth %K.",
            ),
            (
                "smooth_d",
                ParamKind.INT,
                3,
                1,
                100,
                "%D smoothing",
                "Candles of %K averaged into %D.",
            ),
        ),
        (),
        (("k", "%K"), ("d", "%D")),
        None,
        "Slow stochastic oscillator from 0 to 100: %K is the close within the high-low range of "
        "the last length candles, averaged over smooth_k candles; %D averages %K over smooth_d "
        "candles. Undefined when that range is zero.",
    ),
    "obv": (
        "OBV",
        ("close", "volume"),
        (
            (
                "signal",
                ParamKind.INT,
                20,
                2,
                500,
                "Signal length",
                "Candles in the simple moving average of OBV.",
            ),
        ),
        (),
        (("value", "OBV"), ("signal", "Signal line")),
        None,
        "On-balance volume: the first candle's volume, plus the volume of each candle that closes "
        "higher and minus the volume of each candle that closes lower, with its simple moving "
        "average over signal candles (signal line). The OBV level depends on the first candle of "
        "the history; its position relative to the signal line does not.",
    ),
    "volume_sma": (
        "Volume SMA",
        ("volume",),
        (("length", ParamKind.INT, 20, 2, 500, "Length", WINDOW),),
        (),
        (("value", "Volume SMA"),),
        "value",
        "Simple moving average of the volume over the last length candles.",
    ),
}


def test_registry_names_are_the_ten_indicators_in_order() -> None:
    assert REGISTRY.names == NAMES
    assert tuple(spec.name for spec in CATALOG) == NAMES
    assert isinstance(REGISTRY, IndicatorRegistry)
    assert type(CATALOG) is tuple


@pytest.mark.parametrize("name", NAMES)
def test_indicator_metadata_matches_the_spec(name: str) -> None:
    label, inputs, params, constraints, outputs, default_output, description = EXPECTED_METADATA[
        name
    ]
    spec = REGISTRY.get(name)

    assert spec.label == label
    assert spec.description == description
    assert spec.inputs == inputs
    assert (
        tuple(
            (p.name, p.kind, p.default, p.minimum, p.maximum, p.label, p.description)
            for p in spec.params
        )
        == params
    )
    assert tuple((c.left, c.right) for c in spec.constraints) == constraints
    assert tuple((o.name, o.label) for o in spec.outputs) == outputs
    assert spec.default_output == default_output
    for param in spec.params:
        assert type(param.default) is type(param.minimum) is type(param.maximum)
        assert type(param.default) is (int if param.kind is ParamKind.INT else float)


def test_catalog_specs_are_the_registry_specs() -> None:
    assert tuple(REGISTRY.get(name) for name in NAMES) == CATALOG
    assert all(REGISTRY.get(spec.name) is spec for spec in CATALOG)


def test_registry_is_immutable() -> None:
    with pytest.raises(AttributeError):
        REGISTRY._specs = ()  # type: ignore[misc]


# Default warmups of Design 6.1: name -> (lookback, warmup, stable_warmup)
DEFAULT_WARMUPS = {
    "sma": (19, 20, 20),
    "volume_sma": (19, 20, 20),
    "bbands": (19, 20, 20),
    "obv": (19, 20, 20),
    "ema": (19, 20, 94),
    "rsi": (14, 15, 113),
    "atr": (14, 15, 113),
    "macd": (33, 34, 164),
    "adx": (27, 28, 168),
    "stoch": (17, 18, 18),
}


@pytest.mark.parametrize("name", NAMES)
def test_default_warmups_match_the_spec_table(name: str) -> None:
    assert (
        REGISTRY.lookback(name, {}),
        REGISTRY.warmup(name, {}),
        REGISTRY.stable_warmup(name, {}),
    ) == DEFAULT_WARMUPS[name]


# --- AC2 and AC3 on the real catalog --------------------------------------------------------


def test_macd_defaults_and_float_normalization() -> None:
    assert list(REGISTRY.validate_params("macd", {}).items()) == [
        ("fast", 12),
        ("slow", 26),
        ("signal", 9),
    ]
    std = REGISTRY.validate_params("bbands", {"std": 2})["std"]
    assert std == 2.0
    assert type(std) is float


@pytest.mark.parametrize(
    ("name", "params", "kind"),
    [
        ("rsi", {"period": 14}, IndicatorErrorKind.UNKNOWN_PARAMETER),
        ("rsi", {"length": 14.0}, IndicatorErrorKind.WRONG_TYPE),
        ("rsi", {"length": True}, IndicatorErrorKind.WRONG_TYPE),
        ("rsi", {"length": 1}, IndicatorErrorKind.OUT_OF_RANGE),
        ("rsi", {"length": 101}, IndicatorErrorKind.OUT_OF_RANGE),
        ("bbands", {"std": 0.0}, IndicatorErrorKind.OUT_OF_RANGE),
        ("bbands", {"std": 5.5}, IndicatorErrorKind.OUT_OF_RANGE),
        ("bbands", {"std": math.nan}, IndicatorErrorKind.OUT_OF_RANGE),
        ("bbands", {"std": math.inf}, IndicatorErrorKind.OUT_OF_RANGE),
        ("macd", {"fast": 26, "slow": 12}, IndicatorErrorKind.CONSTRAINT),
        ("macd", {"fast": 20, "slow": 20}, IndicatorErrorKind.CONSTRAINT),
    ],
)
def test_catalog_parameter_rejections(
    name: str, params: dict[str, object], kind: IndicatorErrorKind
) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        REGISTRY.validate_params(name, params)

    assert caught.value.kind is kind


def test_catalog_messages_follow_the_spec_examples() -> None:
    with pytest.raises(UnknownIndicatorError) as unknown:
        REGISTRY.get("RSI")
    with pytest.raises(InvalidParameterError) as out_of_range:
        REGISTRY.validate_params("rsi", {"length": 1})
    with pytest.raises(InvalidParameterError) as constraint:
        REGISTRY.validate_params("macd", {"fast": 26, "slow": 12})
    with pytest.raises(InvalidOutputError) as missing:
        REGISTRY.resolve_output("macd", None)

    assert str(unknown.value) == (
        "unknown indicator 'RSI'; expected one of "
        "sma, ema, rsi, macd, bbands, atr, adx, stoch, obv, volume_sma"
    )
    assert str(out_of_range.value) == "parameter 'length' of rsi must be in [2, 100], got 1"
    assert str(constraint.value) == (
        "parameters of macd must satisfy fast < slow, got fast=26, slow=12"
    )
    assert str(missing.value) == "macd has several outputs; choose one of macd, signal, hist"


@pytest.mark.parametrize("name", ["RSI", " rsi", "rsi ", "", "pandas_ta"])
def test_catalog_names_are_exact(name: str) -> None:
    with pytest.raises(UnknownIndicatorError) as caught:
        REGISTRY.compute(name, {}, synthetic_candles(30))

    assert caught.value.kind is IndicatorErrorKind.UNKNOWN_INDICATOR


def test_catalog_outputs() -> None:
    assert REGISTRY.resolve_output("rsi", None) == "value"
    assert REGISTRY.resolve_output("macd", "hist") == "hist"
    for name in ("macd", "bbands", "stoch", "obv"):
        with pytest.raises(InvalidOutputError) as caught:
            REGISTRY.resolve_output(name, None)
        assert caught.value.kind is IndicatorErrorKind.MISSING_OUTPUT
    with pytest.raises(InvalidOutputError) as unknown:
        REGISTRY.resolve_output("rsi", "signal")
    assert unknown.value.kind is IndicatorErrorKind.UNKNOWN_OUTPUT


# --- AC13: describe literals ----------------------------------------------------------------

RSI_JSON = """
{
  "name": "rsi",
  "label": "RSI",
  "description": "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined while the close has not changed since the first candle.",
  "inputs": ["close"],
  "params": [
    {"name": "length", "label": "Length", "description": "Smoothing period in candles.", "type": "int", "default": 14, "min": 2, "max": 100}
  ],
  "constraints": [],
  "outputs": [{"name": "value", "label": "RSI"}],
  "default_output": "value",
  "default_warmup": 15,
  "default_stable_warmup": 113
}
"""  # noqa: E501

MACD_JSON = """
{
  "name": "macd",
  "label": "MACD",
  "description": "Fast EMA minus slow EMA of the close (MACD line), an EMA of that line (signal line) and their difference (histogram).",
  "inputs": ["close"],
  "params": [
    {"name": "fast", "label": "Fast length", "description": "Period of the fast EMA in candles.", "type": "int", "default": 12, "min": 2, "max": 100},
    {"name": "slow", "label": "Slow length", "description": "Period of the slow EMA in candles.", "type": "int", "default": 26, "min": 3, "max": 200},
    {"name": "signal", "label": "Signal length", "description": "Period of the signal line EMA in candles.", "type": "int", "default": 9, "min": 1, "max": 100}
  ],
  "constraints": [{"type": "less_than", "left": "fast", "right": "slow"}],
  "outputs": [
    {"name": "macd", "label": "MACD line"},
    {"name": "signal", "label": "Signal line"},
    {"name": "hist", "label": "Histogram"}
  ],
  "default_output": null,
  "default_warmup": 34,
  "default_stable_warmup": 164
}
"""  # noqa: E501


def entry(name: str) -> dict[str, object]:
    (found,) = (item for item in REGISTRY.describe() if item["name"] == name)
    return found


@pytest.mark.parametrize(("name", "literal"), [("rsi", RSI_JSON), ("macd", MACD_JSON)])
def test_describe_literals(name: str, literal: str) -> None:
    expected = json.loads(literal)
    actual = entry(name)

    assert actual == expected
    assert list(actual) == list(expected)
    assert json.dumps(actual) == json.dumps(expected)


def test_describe_bbands_std_and_obv_entry() -> None:
    std = entry("bbands")["params"][1]  # type: ignore[index]
    obv = entry("obv")

    assert std == {
        "name": "std",
        "label": "Standard deviations",
        "description": (
            "Distance of the bands from the middle band, in population standard deviations."
        ),
        "type": "float",
        "default": 2.0,
        "min": 0.1,
        "max": 5.0,
    }
    assert json.dumps([std["default"], std["min"], std["max"]]) == "[2.0, 0.1, 5.0]"  # type: ignore[index]
    assert obv["params"] == [
        {
            "name": "signal",
            "label": "Signal length",
            "description": "Candles in the simple moving average of OBV.",
            "type": "int",
            "default": 20,
            "min": 2,
            "max": 500,
        }
    ]
    assert obv["outputs"] == [
        {"name": "value", "label": "OBV"},
        {"name": "signal", "label": "Signal line"},
    ]
    assert (obv["default_output"], obv["default_warmup"], obv["default_stable_warmup"]) == (
        None,
        20,
        20,
    )


def test_describe_is_json_ready_in_catalog_order() -> None:
    result = REGISTRY.describe()

    assert [item["name"] for item in result] == list(NAMES)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert REGISTRY.describe() == result


# --- T7: golden values (AC6) ----------------------------------------------------------------


def assert_values(actual: pd.Series, expected: Sequence[float]) -> None:
    np.testing.assert_allclose(actual.to_numpy(), np.asarray(expected), rtol=0.0, atol=1e-12)


def test_sma_golden() -> None:
    result = REGISTRY.compute("sma", {"length": 3}, candles_from_prices([1, 2, 3, 4, 5]))

    assert_values(result["value"], [nan, nan, 2, 3, 4])


def test_volume_sma_golden() -> None:
    frame = candles_from_prices([5, 4, 3, 2, 1], volume=[1, 2, 3, 4, 5])

    result = REGISTRY.compute("volume_sma", {"length": 3}, frame)

    assert_values(result["value"], [nan, nan, 2, 3, 4])


def test_ema_golden() -> None:
    result = REGISTRY.compute("ema", {"length": 3}, candles_from_prices([1, 2, 3, 4, 5]))

    assert_values(result["value"], [nan, nan, 2, 3, 4])


def test_rsi_golden() -> None:
    result = REGISTRY.compute("rsi", {"length": 2}, candles_from_prices([1, 2, 1, 2, 3]))

    assert_values(result["value"], [nan, nan, 50, 75, 87.5])


def test_obv_golden() -> None:
    frame = candles_from_prices([1, 2, 2, 1], volume=[10, 20, 30, 40])

    result = REGISTRY.compute("obv", {"signal": 2}, frame)

    assert list(result) == ["value", "signal"]
    assert_values(result["value"], [nan, 30, 30, -10])
    assert_values(result["signal"], [nan, 20, 30, 10])


def test_bbands_golden() -> None:
    result = REGISTRY.compute("bbands", {"length": 2, "std": 1.0}, candles_from_prices([1, 3, 5]))

    assert list(result) == ["lower", "middle", "upper"]
    assert_values(result["lower"], [nan, 1, 3])
    assert_values(result["middle"], [nan, 2, 4])
    assert_values(result["upper"], [nan, 3, 5])


def test_macd_golden() -> None:
    frame = candles_from_prices(list(range(1, 11)))

    result = REGISTRY.compute("macd", {"fast": 3, "slow": 7, "signal": 3}, frame)

    assert list(result) == ["macd", "signal", "hist"]
    assert_values(result["macd"], [nan] * 8 + [2, 2])
    assert_values(result["signal"], [nan] * 8 + [2, 2])
    assert_values(result["hist"], [nan] * 8 + [0, 0])


def test_atr_golden() -> None:
    frame = candles_from_prices([10, 11, 12, 9], high=[11, 12, 13, 12], low=[9, 10, 11, 8])

    result = REGISTRY.compute("atr", {"length": 2}, frame)

    assert_values(result["value"], [nan, nan, 2, 3])


def test_stoch_golden() -> None:
    frame = candles_from_prices([9, 11, 10, 12], high=[10, 12, 11, 13], low=[8, 9, 9, 10])

    result = REGISTRY.compute("stoch", {"length": 2, "smooth_k": 1, "smooth_d": 2}, frame)

    assert list(result) == ["k", "d"]
    assert_values(result["k"], [nan, nan, 100 / 3, 75])
    assert_values(result["d"], [nan, nan, 325 / 6, 325 / 6])


def trend_frame(*, reverse: bool) -> pd.DataFrame:
    close = [float(c) for c in range(2, 14)]
    if reverse:
        close.reverse()
    return candles_from_prices(close, high=[c + 0.5 for c in close], low=[c - 0.5 for c in close])


@pytest.mark.parametrize("reverse", [False, True], ids=["uptrend", "downtrend"])
def test_adx_trend_golden(reverse: bool) -> None:
    result = REGISTRY.compute("adx", {"length": 3}, trend_frame(reverse=reverse))

    assert_values(result["value"], [nan] * 5 + [100] * 7)


def test_rsi_flat_start_golden() -> None:
    result = REGISTRY.compute("rsi", {"length": 2}, candles_from_prices([5, 5, 5, 6]))

    assert_values(result["value"], [nan, nan, nan, 100])


def test_stoch_flat_start_golden() -> None:
    frame = candles_from_prices([5, 5, 5, 6, 6.5], high=[5, 5, 5, 6, 7], low=[5, 5, 5, 5, 6])

    result = REGISTRY.compute("stoch", {"length": 3, "smooth_k": 1, "smooth_d": 2}, frame)

    assert_values(result["k"], [nan, nan, nan, 100, 75])
    assert_values(result["d"], [nan, nan, nan, nan, 87.5])


def test_adx_flat_start_golden() -> None:
    frame = candles_from_prices(
        [10, 10, 10, 10, 11, 12, 13],
        high=[10, 10, 10, 10, 11.5, 12.5, 13.5],
        low=[10, 10, 10, 10, 10.5, 11.5, 12.5],
    )

    result = REGISTRY.compute("adx", {"length": 2}, frame)

    assert_values(result["value"], [nan, nan, nan, nan, 50, 75, 87.5])


# --- T8: undefined values (AC8) -------------------------------------------------------------


def outputs_of(
    name: str, frame: pd.DataFrame, params: dict[str, object] | None = None
) -> dict[str, pd.Series]:
    return REGISTRY.compute(name, params or {}, frame)


def test_every_flat_candle_frame() -> None:
    frame = synthetic_candles(250, scenario=Scenario.RANDOM_WALK, volatility=0.0)
    assert (frame["high"] == frame["low"]).all()
    close = frame["close"].to_numpy()

    for name in ("rsi", "adx", "stoch"):
        for output, series in outputs_of(name, frame).items():
            assert series.isna().all(), f"{name}.{output} must be undefined on flat candles"
    for name in ("sma", "ema", "bbands", "macd", "atr", "obv", "volume_sma"):
        lookback = REGISTRY.lookback(name, {})
        for output, series in outputs_of(name, frame).items():
            values = series.to_numpy()
            assert np.isnan(values[:lookback]).all(), f"{name}.{output}"
            assert np.isfinite(values[lookback:]).all(), f"{name}.{output}"
    bands = outputs_of("bbands", frame)
    for output in ("lower", "middle", "upper"):
        np.testing.assert_array_equal(bands[output].to_numpy()[19:], close[19:])
    for output, series in outputs_of("macd", frame).items():
        assert (series.to_numpy()[33:] == 0.0).all(), output
    assert (outputs_of("atr", frame)["value"].to_numpy()[14:] == 0.0).all()


@pytest.mark.parametrize("seed", range(5))
def test_stoch_is_undefined_exactly_on_flat_windows(seed: int) -> None:
    frame = synthetic_candles(250, seed=seed, scenario=Scenario.FLAT_RUNS)
    high, low = frame["high"].to_numpy(), frame["low"].to_numpy()
    result = outputs_of("stoch", frame, {"length": 5, "smooth_k": 1, "smooth_d": 1})
    lookback = REGISTRY.lookback("stoch", {"length": 5, "smooth_k": 1, "smooth_d": 1})
    assert lookback == 4

    flat_windows = 0
    for i in range(lookback, len(frame)):
        window = np.concatenate((high[i - 4 : i + 1], low[i - 4 : i + 1]))
        flat = bool((window == window[0]).all())
        flat_windows += flat
        for output in ("k", "d"):
            value = result[output].iloc[i]
            assert math.isnan(value) == flat, f"{output} at {i}: {value}, flat window: {flat}"
    assert flat_windows > 0


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("scenario", [Scenario.RANDOM_WALK, Scenario.GAPS, Scenario.EXTREME])
@pytest.mark.parametrize("name", NAMES)
def test_outputs_are_finite_from_lookback_on_non_flat_frames(
    name: str, scenario: Scenario, seed: int
) -> None:
    frame = synthetic_candles(250, seed=seed, scenario=scenario)
    lookback = REGISTRY.lookback(name, {})

    for output, series in outputs_of(name, frame).items():
        values = series.to_numpy()
        assert np.isnan(values[:lookback]).all(), f"{name}.{output}"
        assert np.isfinite(values[lookback:]).all(), f"{name}.{output}"


def test_rsi_becomes_defined_at_the_first_change_after_a_flat_start() -> None:
    frame = candles_from_prices([7] * 30 + [8, 7.5] + [7.5] * 5)

    values = outputs_of("rsi", frame, {"length": 3})["value"].to_numpy()

    assert np.isnan(values[:30]).all()
    assert np.isfinite(values[30:]).all()
    assert values[30] == 100.0


def test_adx_becomes_defined_at_the_first_directional_movement() -> None:
    close = [10.0] * 20 + [10.0, 10.0, 10.0]
    high = [10.0] * 20 + [10.5, 10.5, 10.5]
    low = [10.0] * 20 + [10.0, 10.0, 10.0]

    values = outputs_of("adx", candles_from_prices(close, high=high, low=low), {"length": 3})[
        "value"
    ].to_numpy()

    assert np.isnan(values[:20]).all()
    assert np.isfinite(values[20:]).all()


def test_stoch_k_and_d_masks_follow_their_windows() -> None:
    # One flat raw window at position 4 (the range over [2..4] is zero), then movement again.
    high = [9.0, 8.0, 5.0, 5.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    low = [4.0, 3.0, 5.0, 5.0, 5.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    close = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    params = {"length": 3, "smooth_k": 2, "smooth_d": 2}

    result = outputs_of("stoch", candles_from_prices(close, high=high, low=low), params)

    # lookback = 3 + 2 + 2 - 3 = 4; raw is undefined only at 4, so k is NaN at 4 and 5 and d
    # at 4, 5 and 6.
    k, d = result["k"].to_numpy(), result["d"].to_numpy()
    assert np.isnan(k[:6]).all()
    assert np.isfinite(k[6:]).all()
    assert np.isnan(d[:7]).all()
    assert np.isfinite(d[7:]).all()


# --- T9: lookback and warmup (AC9) ----------------------------------------------------------


def ema_settle(period: int) -> int:
    return math.ceil(3.5 * (period + 1))


# name -> (params, lookback, settle) for the default, minimum and large parameter sets
PARAMETER_SETS: dict[str, list[tuple[dict[str, object], int, int]]] = {
    "sma": [({}, 19, 0), ({"length": 2}, 1, 0), ({"length": 50}, 49, 0)],
    "volume_sma": [({}, 19, 0), ({"length": 2}, 1, 0), ({"length": 50}, 49, 0)],
    "ema": [
        ({}, 19, ema_settle(20)),
        ({"length": 2}, 1, ema_settle(2)),
        ({"length": 50}, 49, ema_settle(50)),
    ],
    "bbands": [({}, 19, 0), ({"length": 2, "std": 0.1}, 1, 0), ({"length": 50}, 49, 0)],
    "rsi": [({}, 14, 98), ({"length": 2}, 2, 14), ({"length": 50}, 50, 350)],
    "atr": [({}, 14, 98), ({"length": 1}, 1, 7), ({"length": 50}, 50, 350)],
    "adx": [({}, 27, 140), ({"length": 2}, 3, 20), ({"length": 50}, 99, 500)],
    "macd": [
        ({}, 33, ema_settle(26) + ema_settle(9)),
        ({"fast": 2, "slow": 3, "signal": 1}, 2, ema_settle(3) + ema_settle(1)),
        ({"fast": 50, "slow": 100, "signal": 30}, 128, ema_settle(100) + ema_settle(30)),
    ],
    "stoch": [
        ({}, 17, 0),
        ({"length": 1, "smooth_k": 1, "smooth_d": 1}, 0, 0),
        ({"length": 30, "smooth_k": 5, "smooth_d": 5}, 37, 0),
    ],
    "obv": [({}, 19, 0), ({"signal": 2}, 1, 0), ({"signal": 50}, 49, 0)],
}

CASES = [
    pytest.param(name, params, lookback, settle, id=f"{name}-{index}")
    for name, sets in PARAMETER_SETS.items()
    for index, (params, lookback, settle) in enumerate(sets)
]


def test_parameter_sets_cover_every_indicator() -> None:
    assert set(PARAMETER_SETS) == set(NAMES)


def test_ema_settle_is_integer_arithmetic() -> None:
    assert [ema_settle(p) for p in (1, 2, 9, 20, 26, 100, 500)] == [
        (7 * (p + 1) + 1) // 2 for p in (1, 2, 9, 20, 26, 100, 500)
    ]


@pytest.mark.parametrize(("name", "params", "lookback", "settle"), CASES)
def test_lookback_warmup_and_stable_warmup_formulas(
    name: str, params: dict[str, object], lookback: int, settle: int
) -> None:
    assert REGISTRY.lookback(name, params) == lookback
    assert REGISTRY.warmup(name, params) == lookback + 1
    assert REGISTRY.stable_warmup(name, params) == lookback + 1 + settle


NON_FLAT_FRAME = synthetic_candles(400, seed=11, scenario=Scenario.RANDOM_WALK)


@pytest.mark.parametrize(("name", "params", "lookback", "settle"), CASES)
def test_leading_nan_rows_equal_lookback(
    name: str, params: dict[str, object], lookback: int, settle: int
) -> None:
    for output, series in REGISTRY.compute(name, params, NON_FLAT_FRAME).items():
        values = series.to_numpy()
        leading = int(np.argmax(~np.isnan(values)))
        assert leading == lookback, f"{name}.{output}"
        assert np.isfinite(values[lookback:]).all(), f"{name}.{output}"


@pytest.mark.parametrize(("name", "params", "lookback", "settle"), CASES)
def test_warmup_is_the_shortest_frame_with_a_last_value(
    name: str, params: dict[str, object], lookback: int, settle: int
) -> None:
    warmup = REGISTRY.warmup(name, params)
    short = REGISTRY.compute(name, params, NON_FLAT_FRAME.iloc[: warmup - 1])
    enough = REGISTRY.compute(name, params, NON_FLAT_FRAME.iloc[:warmup])

    assert all(series.isna().all() for series in short.values())
    assert all(math.isfinite(series.iloc[-1]) for series in enough.values())


@pytest.mark.parametrize("name", NAMES)
def test_warmup_helpers_validate_params(name: str) -> None:
    for helper in (REGISTRY.lookback, REGISTRY.warmup, REGISTRY.stable_warmup):
        with pytest.raises(InvalidParameterError):
            helper(name, {"unknown": 1})


# --- T11: TA-Lib unstable-period guard (AC12) -----------------------------------------------


@contextmanager
def unstable_period(function: str, period: int) -> Iterator[None]:
    talib.set_unstable_period(function, period)
    try:
        yield
    finally:
        talib.set_unstable_period(function, 0)


GUARDED = [("EMA", "ema"), ("EMA", "macd"), ("RSI", "rsi"), ("ATR", "atr"), ("ADX", "adx")]


@pytest.mark.parametrize(("function", "name"), GUARDED)
def test_non_default_unstable_period_raises(function: str, name: str) -> None:
    frame = synthetic_candles(120, seed=5)
    expected = REGISTRY.compute(name, {}, frame)

    with unstable_period(function, 3), pytest.raises(IndicatorComputationError) as caught:
        REGISTRY.compute(name, {}, frame)

    assert str(caught.value) == (
        f"TA-Lib unstable period for {function} is 3; the indicator registry requires 0"
    )
    assert talib.get_unstable_period(function) == 0
    again = REGISTRY.compute(name, {}, frame)
    for output in expected:
        assert expected[output].to_numpy().tobytes() == again[output].to_numpy().tobytes()


def test_unstable_period_of_one_function_does_not_block_the_others() -> None:
    frame = synthetic_candles(120, seed=5)

    with unstable_period("ADX", 2):
        for name in ("sma", "bbands", "stoch", "obv", "volume_sma", "ema", "rsi", "atr", "macd"):
            result = REGISTRY.compute(name, {}, frame)
            assert list(result) == list(REGISTRY.get(name).output_names)


def test_short_frames_do_not_reach_the_guard() -> None:
    with unstable_period("RSI", 4):
        result = REGISTRY.compute("rsi", {}, synthetic_candles(14))

    assert result["value"].isna().all()


def test_computing_every_indicator_leaves_talib_settings_at_their_defaults() -> None:
    frame = synthetic_candles(300, seed=2, scenario=Scenario.MIXED)

    for name in NAMES:
        REGISTRY.compute(name, {}, frame)

    assert [talib.get_unstable_period(f) for f in ("EMA", "RSI", "ATR", "ADX")] == [0, 0, 0, 0]
    assert talib.get_compatibility() == 0

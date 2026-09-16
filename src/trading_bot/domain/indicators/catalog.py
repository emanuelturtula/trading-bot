"""The default indicator catalog and registry (spec 005, Design 6).

``REGISTRY`` is an immutable module constant built from ``CATALOG``, the ten whitelisted
indicators of the rule model. Parameter names, defaults, ranges, labels and descriptions are
user-visible (dashboard forms and stored rule JSON): changing them requires migrating stored
rules. Importing this module loads TA-Lib through ``talib_kernels``.
"""

from __future__ import annotations

from typing import Final

from trading_bot.domain.indicators import talib_kernels
from trading_bot.domain.indicators.params import IndicatorParams, LessThan, ParamKind, ParamSpec
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.indicators.spec import IndicatorSpec, OutputSpec

__all__ = [
    "ADX",
    "ATR",
    "BBANDS",
    "CATALOG",
    "EMA",
    "MACD",
    "OBV",
    "REGISTRY",
    "RSI",
    "SMA",
    "STOCH",
    "VOLUME_SMA",
]

_WINDOW: Final = "Candles in the window."
_SMOOTHING: Final = "Smoothing period in candles."


def _int_param(
    name: str, default: int, minimum: int, maximum: int, label: str, description: str
) -> ParamSpec:
    return ParamSpec(
        name=name,
        kind=ParamKind.INT,
        default=default,
        minimum=minimum,
        maximum=maximum,
        label=label,
        description=description,
    )


def _value(label: str) -> tuple[OutputSpec, ...]:
    return (OutputSpec(name="value", label=label),)


def _length_minus_one(params: IndicatorParams) -> int:
    return params.integer("length") - 1


def _length(params: IndicatorParams) -> int:
    return params.integer("length")


def _no_settle(params: IndicatorParams) -> int:
    return 0


def _wilder_settle(params: IndicatorParams) -> int:
    return 7 * params.integer("length")


def _ema_settle(period: int) -> int:
    """``ceil(3.5 * (period + 1))`` in integer arithmetic: the seed weighs about ``e^-7``."""
    return (7 * (period + 1) + 1) // 2


SMA: Final = IndicatorSpec(
    name="sma",
    label="SMA",
    description="Simple moving average of the close over the last length candles.",
    inputs=("close",),
    params=(_int_param("length", 20, 2, 500, "Length", _WINDOW),),
    constraints=(),
    outputs=_value("SMA"),
    lookback=_length_minus_one,
    settle=_no_settle,
    kernel=talib_kernels.sma,
)

EMA: Final = IndicatorSpec(
    name="ema",
    label="EMA",
    description=(
        "Exponential moving average of the close with smoothing 2 / (length + 1), seeded with "
        "the simple average of the first length closes."
    ),
    inputs=("close",),
    params=(_int_param("length", 20, 2, 500, "Length", _SMOOTHING),),
    constraints=(),
    outputs=_value("EMA"),
    lookback=_length_minus_one,
    settle=lambda params: _ema_settle(params.integer("length")),
    kernel=talib_kernels.ema,
)

RSI: Final = IndicatorSpec(
    name="rsi",
    label="RSI",
    description=(
        "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined "
        "while the close has not changed since the first candle."
    ),
    inputs=("close",),
    params=(_int_param("length", 14, 2, 100, "Length", _SMOOTHING),),
    constraints=(),
    outputs=_value("RSI"),
    lookback=_length,
    settle=_wilder_settle,
    kernel=talib_kernels.rsi,
)

MACD: Final = IndicatorSpec(
    name="macd",
    label="MACD",
    description=(
        "Fast EMA minus slow EMA of the close (MACD line), an EMA of that line (signal line) and "
        "their difference (histogram)."
    ),
    inputs=("close",),
    params=(
        _int_param("fast", 12, 2, 100, "Fast length", "Period of the fast EMA in candles."),
        _int_param("slow", 26, 3, 200, "Slow length", "Period of the slow EMA in candles."),
        _int_param(
            "signal", 9, 1, 100, "Signal length", "Period of the signal line EMA in candles."
        ),
    ),
    constraints=(LessThan(left="fast", right="slow"),),
    outputs=(
        OutputSpec(name="macd", label="MACD line"),
        OutputSpec(name="signal", label="Signal line"),
        OutputSpec(name="hist", label="Histogram"),
    ),
    lookback=lambda params: params.integer("slow") + params.integer("signal") - 2,
    settle=lambda params: (
        _ema_settle(params.integer("slow")) + _ema_settle(params.integer("signal"))
    ),
    kernel=talib_kernels.macd,
)

BBANDS: Final = IndicatorSpec(
    name="bbands",
    label="Bollinger Bands",
    description=(
        "Simple moving average of the close (middle band) plus and minus std population standard "
        "deviations over the last length candles."
    ),
    inputs=("close",),
    params=(
        _int_param("length", 20, 2, 500, "Length", _WINDOW),
        ParamSpec(
            name="std",
            kind=ParamKind.FLOAT,
            default=2.0,
            minimum=0.1,
            maximum=5.0,
            label="Standard deviations",
            description=(
                "Distance of the bands from the middle band, in population standard deviations."
            ),
        ),
    ),
    constraints=(),
    outputs=(
        OutputSpec(name="lower", label="Lower band"),
        OutputSpec(name="middle", label="Middle band"),
        OutputSpec(name="upper", label="Upper band"),
    ),
    lookback=_length_minus_one,
    settle=_no_settle,
    kernel=talib_kernels.bbands,
)

ATR: Final = IndicatorSpec(
    name="atr",
    label="ATR",
    description="Wilder's average true range over length candles, in price units.",
    inputs=("high", "low", "close"),
    params=(_int_param("length", 14, 1, 100, "Length", _SMOOTHING),),
    constraints=(),
    outputs=_value("ATR"),
    lookback=_length,
    settle=_wilder_settle,
    kernel=talib_kernels.atr,
)

ADX: Final = IndicatorSpec(
    name="adx",
    label="ADX",
    description=(
        "Wilder's average directional index, from 0 to 100: trend strength regardless of "
        "direction. Undefined until the first candle with directional movement."
    ),
    inputs=("high", "low", "close"),
    params=(_int_param("length", 14, 2, 100, "Length", _SMOOTHING),),
    constraints=(),
    outputs=_value("ADX"),
    lookback=lambda params: 2 * params.integer("length") - 1,
    settle=lambda params: 10 * params.integer("length"),
    kernel=talib_kernels.adx,
)

STOCH: Final = IndicatorSpec(
    name="stoch",
    label="Stochastic",
    description=(
        "Slow stochastic oscillator from 0 to 100: %K is the close within the high-low range of "
        "the last length candles, averaged over smooth_k candles; %D averages %K over smooth_d "
        "candles. Undefined when that range is zero."
    ),
    inputs=("high", "low", "close"),
    params=(
        _int_param("length", 14, 1, 100, "%K length", "Candles in the high-low range."),
        _int_param("smooth_k", 3, 1, 100, "%K smoothing", "Candles averaged to smooth %K."),
        _int_param("smooth_d", 3, 1, 100, "%D smoothing", "Candles of %K averaged into %D."),
    ),
    constraints=(),
    outputs=(OutputSpec(name="k", label="%K"), OutputSpec(name="d", label="%D")),
    lookback=lambda params: (
        params.integer("length") + params.integer("smooth_k") + params.integer("smooth_d") - 3
    ),
    settle=_no_settle,
    kernel=talib_kernels.stoch,
)

OBV: Final = IndicatorSpec(
    name="obv",
    label="OBV",
    description=(
        "On-balance volume: the first candle's volume, plus the volume of each candle that closes "
        "higher and minus the volume of each candle that closes lower, with its simple moving "
        "average over signal candles (signal line). The OBV level depends on the first candle of "
        "the history; its position relative to the signal line does not."
    ),
    inputs=("close", "volume"),
    params=(
        _int_param(
            "signal", 20, 2, 500, "Signal length", "Candles in the simple moving average of OBV."
        ),
    ),
    constraints=(),
    outputs=(OutputSpec(name="value", label="OBV"), OutputSpec(name="signal", label="Signal line")),
    lookback=lambda params: params.integer("signal") - 1,
    settle=_no_settle,
    kernel=talib_kernels.obv,
)

VOLUME_SMA: Final = IndicatorSpec(
    name="volume_sma",
    label="Volume SMA",
    description="Simple moving average of the volume over the last length candles.",
    inputs=("volume",),
    params=(_int_param("length", 20, 2, 500, "Length", _WINDOW),),
    constraints=(),
    outputs=_value("Volume SMA"),
    lookback=_length_minus_one,
    settle=_no_settle,
    kernel=talib_kernels.volume_sma,
)

CATALOG: Final[tuple[IndicatorSpec, ...]] = (
    SMA,
    EMA,
    RSI,
    MACD,
    BBANDS,
    ATR,
    ADX,
    STOCH,
    OBV,
    VOLUME_SMA,
)

REGISTRY: Final = IndicatorRegistry(CATALOG)

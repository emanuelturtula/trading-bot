"""TA-Lib kernels of the default indicator catalog (spec 005, Design 6, 7 and 9).

This is the only module that imports TA-Lib, so replacing TA-Lib means rewriting this file.
Each kernel reads only its input arrays and parameters, passes every TA-Lib parameter by keyword
with explicit moving-average types, and returns newly allocated arrays.

Where a formula divides by zero (``rsi``, ``adx`` and ``stoch`` on flat data), TA-Lib returns
``0``; the kernels replace those values with NaN using masks computed from the inputs only, each
position from candles at or before it.

TA-Lib global settings are never changed here. The kernels of indicators with an unstable
period (``ema``, ``macd``, ``rsi``, ``atr``, ``adx``) check it is ``0`` on every call and raise
``IndicatorComputationError`` otherwise, so a foreign change fails loudly instead of silently
shifting values.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import numpy.typing as npt
import talib
from talib._ta_lib import MA_Type

from trading_bot.domain.indicators.errors import IndicatorComputationError
from trading_bot.domain.indicators.params import IndicatorParams
from trading_bot.domain.indicators.spec import FloatArray, InputArrays

__all__ = ["adx", "atr", "bbands", "ema", "macd", "obv", "rsi", "sma", "stoch", "volume_sma"]

type _BoolArray = npt.NDArray[np.bool_]

_STOCH_ZERO_RANGE: Final = 1e-14  # TA-Lib's scale-relative zero test for a high-low range


def sma(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: simple moving average of the close."""
    return (talib.SMA(real=inputs["close"], timeperiod=params.integer("length")),)


def volume_sma(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: simple moving average of the volume."""
    return (talib.SMA(real=inputs["volume"], timeperiod=params.integer("length")),)


def ema(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: exponential moving average of the close, seeded with a simple average."""
    _require_default_unstable_period("EMA")
    return (talib.EMA(real=inputs["close"], timeperiod=params.integer("length")),)


def rsi(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: Wilder's RSI; NaN while every close so far equals the first one."""
    _require_default_unstable_period("RSI")
    close = inputs["close"]
    value = talib.RSI(real=close, timeperiod=params.integer("length"))
    # Average gain plus average loss is zero exactly while no close has changed yet.
    value[np.logical_and.accumulate(close == close[0])] = np.nan
    return (value,)


def macd(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``macd``, ``signal`` and ``hist`` (MACD uses the EMA unstable period)."""
    _require_default_unstable_period("EMA")
    line, signal, hist = talib.MACD(
        real=inputs["close"],
        fastperiod=params.integer("fast"),
        slowperiod=params.integer("slow"),
        signalperiod=params.integer("signal"),
    )
    return (line, signal, hist)


def bbands(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``lower``, ``middle`` and ``upper`` Bollinger Bands with population deviations."""
    deviations = params.real("std")
    upper, middle, lower = talib.BBANDS(
        real=inputs["close"],
        timeperiod=params.integer("length"),
        nbdevup=deviations,
        nbdevdn=deviations,
        matype=MA_Type.SMA,
    )
    return (lower, middle, upper)


def atr(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: Wilder's average true range."""
    _require_default_unstable_period("ATR")
    value = talib.ATR(
        high=inputs["high"],
        low=inputs["low"],
        close=inputs["close"],
        timeperiod=params.integer("length"),
    )
    return (value,)


def adx(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value``: Wilder's ADX; NaN until the first candle with directional movement."""
    _require_default_unstable_period("ADX")
    high, low = inputs["high"], inputs["low"]
    value = talib.ADX(
        high=high, low=low, close=inputs["close"], timeperiod=params.integer("length")
    )
    # No DX is defined until some candle has +DM > 0 or -DM > 0 (Wilder's directional movement).
    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    moved = ((up > 0.0) & (up > down)) | ((down > 0.0) & (down > up))
    undefined = np.ones(len(high), dtype=np.bool_)
    undefined[1:] = ~np.logical_or.accumulate(moved)
    value[undefined] = np.nan
    return (value,)


def stoch(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``k`` and ``d`` of the slow stochastic; NaN where a high-low range in the window is zero."""
    high, low = inputs["high"], inputs["low"]
    length = params.integer("length")
    smooth_k = params.integer("smooth_k")
    smooth_d = params.integer("smooth_d")
    k, d = talib.STOCH(
        high=high,
        low=low,
        close=inputs["close"],
        fastk_period=length,
        slowk_period=smooth_k,
        slowk_matype=MA_Type.SMA,
        slowd_period=smooth_d,
        slowd_matype=MA_Type.SMA,
    )
    # TA-Lib substitutes 0 for the raw %K of a window whose range fails its scale-relative zero
    # test; %K averages smooth_k raw values and %D averages smooth_d %K values.
    k_undefined = _rolling_any(_zero_range(high, low, length), smooth_k)
    k[k_undefined] = np.nan
    d[_rolling_any(k_undefined, smooth_d)] = np.nan
    return (k, d)


def obv(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """``value`` (on-balance volume) and ``signal`` (its simple moving average)."""
    period = params.integer("signal")
    # The array arguments of OBV are positional: the runtime names them (real, volume) while the
    # package stubs name them (close, volume), so no keyword satisfies both.
    level = talib.OBV(inputs["close"], inputs["volume"])
    signal = talib.SMA(real=level, timeperiod=period)
    value = level.copy()
    value[: period - 1] = np.nan
    return (value, signal)


def _zero_range(high: FloatArray, low: FloatArray, length: int) -> _BoolArray:
    """``True`` at ``j`` when the range of ``[j - length + 1 .. j]`` is zero for TA-Lib's STOCH.

    The test is ``highest - lowest <= 1e-14 * (highest + lowest)``. Positions before the first
    full window are ``False``: no output reads them.
    """
    zero = np.zeros(len(high), dtype=np.bool_)
    highest = np.lib.stride_tricks.sliding_window_view(high, length).max(axis=1)
    lowest = np.lib.stride_tricks.sliding_window_view(low, length).min(axis=1)
    zero[length - 1 :] = highest - lowest <= _STOCH_ZERO_RANGE * (highest + lowest)
    return zero


def _rolling_any(flags: _BoolArray, window: int) -> _BoolArray:
    """``True`` at ``i`` when any of ``flags[i - window + 1 .. i]`` is ``True``.

    Before ``window`` the window is the prefix ``flags[0 .. i]``.
    """
    counts = np.cumsum(flags, dtype=np.int64)
    found = counts > 0
    found[window:] = counts[window:] > counts[:-window]
    return found


def _require_default_unstable_period(function: str) -> None:
    # talib.get_unstable_period is not in talib.__all__, so the package stubs do not export it.
    period: object = talib.get_unstable_period(function)  # type: ignore[attr-defined]
    if period != 0:
        raise IndicatorComputationError(
            f"TA-Lib unstable period for {function} is {period!r}; "
            "the indicator registry requires 0"
        )

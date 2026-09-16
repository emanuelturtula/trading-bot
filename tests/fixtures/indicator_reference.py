"""Independent plain-Python reference for the ten indicators (spec 005, Design 6.2 and 7).

Written directly from ``docs/specs/005-indicator-registry.md`` sections 6.2 (definitions) and 7
(undefined values), without reading ``talib_kernels.py`` or any other kernel code (spec 005,
Design 1, "Why the tester writes the reference"). This module never imports ``talib``: it is a
second, independent implementation of the same formulas, so a misunderstanding baked into the
kernels would not also be baked into the check that verifies them (AC7).

Recursive indicators (``ema``, ``rsi``, ``atr``, ``adx``, ``macd``, the ``obv`` signal excepted)
use a plain Python loop over a value seeded from a window average: numpy has no built-in IIR
filter, and this module must not depend on ``scipy``, which is not a project dependency. Window
indicators (``sma``, ``volume_sma``, ``bbands``, ``stoch``) are vectorized with
``numpy.lib.stride_tricks.sliding_window_view``: each window is independent, so a NaN placed at an
undefined position (the stoch high-low range test) propagates through ``numpy.mean`` to exactly
the %K and %D positions section 7 describes, with no separate masking pass needed.

Always import this module as ``tests.fixtures.indicator_reference``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt
import pandas as pd

__all__ = [
    "compute_reference",
    "reference_adx",
    "reference_atr",
    "reference_bbands",
    "reference_ema",
    "reference_macd",
    "reference_obv",
    "reference_rsi",
    "reference_sma",
    "reference_stoch",
    "reference_volume_sma",
]

type FloatArray = npt.NDArray[np.float64]

# TA-Lib's scale-relative zero test on the stoch high-low range and on adx's (+DI + -DI) (spec 7).
_ZERO_FLOOR = 1e-14


def _as_float_array(values: Sequence[float] | FloatArray) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def _rolling_mean(x: FloatArray, window: int) -> FloatArray:
    """``value[i] = mean(x[i-window+1..i])``, NaN before ``window - 1``.

    Any NaN already present in ``x`` propagates to every output position whose window contains
    it, because this uses ``numpy.mean`` (not ``numpy.nanmean``) over independent windows.
    """
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if 0 < window <= n:
        windows: FloatArray = np.lib.stride_tricks.sliding_window_view(x, window)
        result[window - 1 :] = windows.mean(axis=1)
    return result


def _rolling_population_std(x: FloatArray, window: int) -> FloatArray:
    """``value[i]`` is the population standard deviation of ``x[i-window+1..i]``."""
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if 0 < window <= n:
        windows: FloatArray = np.lib.stride_tricks.sliding_window_view(x, window)
        mean = windows.mean(axis=1, keepdims=True)
        variance = np.maximum(((windows - mean) ** 2).mean(axis=1), 0.0)
        result[window - 1 :] = np.sqrt(variance)
    return result


def _rolling_max(x: FloatArray, window: int) -> FloatArray:
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if 0 < window <= n:
        windows: FloatArray = np.lib.stride_tricks.sliding_window_view(x, window)
        result[window - 1 :] = windows.max(axis=1)
    return result


def _rolling_min(x: FloatArray, window: int) -> FloatArray:
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if 0 < window <= n:
        windows: FloatArray = np.lib.stride_tricks.sliding_window_view(x, window)
        result[window - 1 :] = windows.min(axis=1)
    return result


def reference_sma(close: Sequence[float] | FloatArray, length: int) -> FloatArray:
    """``value[i] = mean(close[i-length+1..i])`` (spec 6.2, ``sma``)."""
    return _rolling_mean(_as_float_array(close), length)


def reference_volume_sma(volume: Sequence[float] | FloatArray, length: int) -> FloatArray:
    """``value[i] = mean(volume[i-length+1..i])`` (spec 6.2, ``volume_sma``)."""
    return _rolling_mean(_as_float_array(volume), length)


def reference_ema(close: Sequence[float] | FloatArray, length: int) -> FloatArray:
    """EMA seeded with the simple average of the first ``length`` closes (spec 6.2, ``ema``)."""
    x = _as_float_array(close)
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if length > n:
        return result
    alpha = 2.0 / (length + 1)
    values = x.tolist()
    previous = sum(values[:length]) / length
    result[length - 1] = previous
    for i in range(length, n):
        previous = previous + alpha * (values[i] - previous)
        result[i] = previous
    return result


def reference_rsi(close: Sequence[float] | FloatArray, length: int) -> FloatArray:
    """Wilder's RSI; NaN while every close up to ``i`` is equal (spec 6.2 and 7, ``rsi``)."""
    x = _as_float_array(close)
    n = len(x)
    result = np.full(n, np.nan, dtype=np.float64)
    if length >= n:
        return result
    values = x.tolist()
    changes = [values[i] - values[i - 1] for i in range(1, n)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    gain_average = sum(gains[:length]) / length
    loss_average = sum(losses[:length]) / length
    result[length] = _rsi_value(gain_average, loss_average)
    for i in range(length + 1, n):
        gain_average = (gain_average * (length - 1) + gains[i - 1]) / length
        loss_average = (loss_average * (length - 1) + losses[i - 1]) / length
        result[i] = _rsi_value(gain_average, loss_average)
    return result


def _rsi_value(gain_average: float, loss_average: float) -> float:
    total = gain_average + loss_average
    return 100.0 * gain_average / total if total != 0.0 else math.nan


def reference_atr(
    high: Sequence[float] | FloatArray,
    low: Sequence[float] | FloatArray,
    close: Sequence[float] | FloatArray,
    length: int,
) -> FloatArray:
    """Wilder's average true range (spec 6.2, ``atr``)."""
    highs = _as_float_array(high).tolist()
    lows = _as_float_array(low).tolist()
    closes = _as_float_array(close).tolist()
    n = len(highs)
    result = np.full(n, np.nan, dtype=np.float64)
    if length >= n:
        return result
    true_range = _true_range(highs, lows, closes)
    value = sum(true_range[1 : length + 1]) / length
    result[length] = value
    for i in range(length + 1, n):
        value = (value * (length - 1) + true_range[i]) / length
        result[i] = value
    return result


def _true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    n = len(highs)
    true_range = [0.0] * n  # position 0 is unused (no previous close)
    for i in range(1, n):
        true_range[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return true_range


def reference_adx(
    high: Sequence[float] | FloatArray,
    low: Sequence[float] | FloatArray,
    close: Sequence[float] | FloatArray,
    length: int,
) -> FloatArray:
    """Wilder's ADX with TA-Lib's seeding; NaN until the first directional movement (6.2, 7)."""
    highs = _as_float_array(high).tolist()
    lows = _as_float_array(low).tolist()
    closes = _as_float_array(close).tolist()
    n = len(highs)
    lookback = 2 * length - 1
    result = np.full(n, np.nan, dtype=np.float64)
    if lookback >= n:
        return result
    true_range = _true_range(highs, lows, closes)
    plus_dm, minus_dm = _directional_movement(highs, lows)
    moved_by = np.logical_or.accumulate((np.asarray(plus_dm) > 0.0) | (np.asarray(minus_dm) > 0.0))
    plus_sum = sum(plus_dm[1:length])
    minus_sum = sum(minus_dm[1:length])
    tr_sum = sum(true_range[1:length])
    defined_dx_total = 0.0
    for i in range(length, 2 * length):
        plus_sum = plus_sum - plus_sum / length + plus_dm[i]
        minus_sum = minus_sum - minus_sum / length + minus_dm[i]
        tr_sum = tr_sum - tr_sum / length + true_range[i]
        dx = _dx_value(plus_sum, minus_sum, tr_sum)
        if not math.isnan(dx):
            defined_dx_total += dx
    value = defined_dx_total / length
    result[2 * length - 1] = value
    for i in range(2 * length, n):
        plus_sum = plus_sum - plus_sum / length + plus_dm[i]
        minus_sum = minus_sum - minus_sum / length + minus_dm[i]
        tr_sum = tr_sum - tr_sum / length + true_range[i]
        dx = _dx_value(plus_sum, minus_sum, tr_sum)
        if not math.isnan(dx):
            value = (value * (length - 1) + dx) / length
        result[i] = value
    result[lookback:][~moved_by[lookback:]] = np.nan
    return result


def _directional_movement(highs: list[float], lows: list[float]) -> tuple[list[float], list[float]]:
    n = len(highs)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > 0.0 and up > down) else 0.0
        minus_dm[i] = down if (down > 0.0 and down > up) else 0.0
    return plus_dm, minus_dm


def _dx_value(plus_sum: float, minus_sum: float, tr_sum: float) -> float:
    if tr_sum <= 0.0:
        return math.nan
    plus_di = 100.0 * plus_sum / tr_sum
    minus_di = 100.0 * minus_sum / tr_sum
    total_di = plus_di + minus_di
    if abs(total_di) < _ZERO_FLOOR:
        return math.nan
    return 100.0 * abs(plus_di - minus_di) / total_di


def reference_macd(
    close: Sequence[float] | FloatArray, fast: int, slow: int, signal: int
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Fast EMA minus slow EMA, both seeded on the same candle, and their signal EMA (6.2)."""
    values = _as_float_array(close).tolist()
    n = len(values)
    lookback = slow + signal - 2
    macd_line = np.full(n, np.nan, dtype=np.float64)
    signal_line = np.full(n, np.nan, dtype=np.float64)
    hist = np.full(n, np.nan, dtype=np.float64)
    if lookback >= n:
        return macd_line, signal_line, hist
    alpha_fast = 2.0 / (fast + 1)
    alpha_slow = 2.0 / (slow + 1)
    alpha_signal = 2.0 / (signal + 1)
    slow_value = sum(values[:slow]) / slow
    fast_value = sum(values[slow - fast : slow]) / fast
    line = [math.nan] * n
    line[slow - 1] = fast_value - slow_value
    for i in range(slow, n):
        slow_value = slow_value + alpha_slow * (values[i] - slow_value)
        fast_value = fast_value + alpha_fast * (values[i] - fast_value)
        line[i] = fast_value - slow_value
    seed_start = slow - 1
    signal_value = sum(line[seed_start : lookback + 1]) / signal
    macd_line[lookback] = line[lookback]
    signal_line[lookback] = signal_value
    hist[lookback] = line[lookback] - signal_value
    for i in range(lookback + 1, n):
        signal_value = signal_value + alpha_signal * (line[i] - signal_value)
        macd_line[i] = line[i]
        signal_line[i] = signal_value
        hist[i] = line[i] - signal_value
    return macd_line, signal_line, hist


def reference_bbands(
    close: Sequence[float] | FloatArray, length: int, std: float
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """SMA of the close plus and minus ``std`` population standard deviations (6.2, ``bbands``)."""
    x = _as_float_array(close)
    middle = _rolling_mean(x, length)
    deviation = std * _rolling_population_std(x, length)
    return middle - deviation, middle, middle + deviation


def reference_stoch(
    high: Sequence[float] | FloatArray,
    low: Sequence[float] | FloatArray,
    close: Sequence[float] | FloatArray,
    length: int,
    smooth_k: int,
    smooth_d: int,
) -> tuple[FloatArray, FloatArray]:
    """Slow stochastic with simple averages; NaN windows propagate from %K into %D (6.2, 7)."""
    highs = _as_float_array(high)
    lows = _as_float_array(low)
    closes = _as_float_array(close)
    highest = _rolling_max(highs, length)
    lowest = _rolling_min(lows, length)
    price_range = highest - lowest
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = 100.0 * (closes - lowest) / price_range
    threshold = _ZERO_FLOOR * (highest + lowest)
    defined_range = price_range > threshold  # False (and raw masked) where range is NaN too
    raw = np.where(defined_range, raw, np.nan)
    k = _rolling_mean(raw, smooth_k)
    d = _rolling_mean(k, smooth_d)
    # %K is defined from length + smooth_k - 2, but the registry aligns both outputs to the
    # combined lookback (spec 6.2: "both outputs start at N+K+D-3, although k is defined from
    # N+K-2"), the same alignment as macd and obv. %D already starts there naturally, so only
    # %K needs trimming; it never gains values, since smooth_d >= 1.
    combined_lookback = length + smooth_k + smooth_d - 3
    k = k.copy()
    k[:combined_lookback] = np.nan
    return k, d


def reference_obv(
    close: Sequence[float] | FloatArray, volume: Sequence[float] | FloatArray, signal: int
) -> tuple[FloatArray, FloatArray]:
    """OBV seeded with the first volume, plus its simple moving average (spec 6.2, 8, ``obv``)."""
    closes = _as_float_array(close)
    volumes = _as_float_array(volume)
    n = len(closes)
    level = np.empty(n, dtype=np.float64)
    level[0] = volumes[0]
    if n > 1:
        direction = np.sign(np.diff(closes))
        level[1:] = volumes[0] + np.cumsum(direction * volumes[1:])
    signal_line = _rolling_mean(level, signal)
    value = level.copy()
    value[: signal - 1] = np.nan
    return value, signal_line


def compute_reference(
    name: str, params: Mapping[str, int | float], candles: pd.DataFrame
) -> dict[str, FloatArray]:
    """The reference output of indicator ``name``, keyed like ``REGISTRY.compute`` (AC7)."""
    close = candles["close"].to_numpy(dtype=np.float64)
    high = candles["high"].to_numpy(dtype=np.float64)
    low = candles["low"].to_numpy(dtype=np.float64)
    volume = candles["volume"].to_numpy(dtype=np.float64)
    if name == "sma":
        return {"value": reference_sma(close, int(params["length"]))}
    if name == "volume_sma":
        return {"value": reference_volume_sma(volume, int(params["length"]))}
    if name == "ema":
        return {"value": reference_ema(close, int(params["length"]))}
    if name == "rsi":
        return {"value": reference_rsi(close, int(params["length"]))}
    if name == "atr":
        return {"value": reference_atr(high, low, close, int(params["length"]))}
    if name == "adx":
        return {"value": reference_adx(high, low, close, int(params["length"]))}
    if name == "macd":
        macd_line, signal_line, hist = reference_macd(
            close, int(params["fast"]), int(params["slow"]), int(params["signal"])
        )
        return {"macd": macd_line, "signal": signal_line, "hist": hist}
    if name == "bbands":
        lower, middle, upper = reference_bbands(close, int(params["length"]), float(params["std"]))
        return {"lower": lower, "middle": middle, "upper": upper}
    if name == "stoch":
        k, d = reference_stoch(
            high,
            low,
            close,
            int(params["length"]),
            int(params["smooth_k"]),
            int(params["smooth_d"]),
        )
        return {"k": k, "d": d}
    if name == "obv":
        value, signal_line = reference_obv(close, volume, int(params["signal"]))
        return {"value": value, "signal": signal_line}
    raise ValueError(f"no independent reference for indicator {name!r}")

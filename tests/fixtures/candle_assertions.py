"""Shared assertions about synthetic candle frames (spec 004, Design 7).

Every helper raises ``AssertionError`` with a message explicitly: pytest only rewrites bare
``assert`` statements inside test modules, and they vanish under ``python -O``. Always import
this module as ``tests.fixtures.candle_assertions``.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

from tests.fixtures.candles import MAX_PRICE, MIN_PRICE, TimeframeCode
from trading_bot.domain.candles import PRICE_COLUMNS, CandleValidationError, validate_candles
from trading_bot.domain.timeframe import Timeframe

MIN_FLAT_RUN = 5
VOLUME_SPIKE = 1e12
WEEKEND_DAYS = (5, 6)


def assert_valid_candles(candles: pd.DataFrame, timeframe: Timeframe | TimeframeCode) -> None:
    """The candle contract (``validate_candles``) plus the generator-only invariants.

    The generator also guarantees what the contract leaves open: open times on the timeframe
    grid and every price within ``[MIN_PRICE, MAX_PRICE]``.
    """
    try:
        result = validate_candles(candles)
    except CandleValidationError as error:
        raise AssertionError(f"not a valid candle frame: {error}") from error
    if result is not candles:
        raise AssertionError("validate_candles must return the frame it was given")
    index = _datetime_index(candles)
    off_grid = index != index.floor(_duration(timeframe))
    if off_grid.any():
        first = index[int(np.argmax(off_grid))]
        raise AssertionError(
            f"candle open time {first.isoformat()} is not on the {Timeframe(timeframe)} grid"
        )
    prices = candles[list(PRICE_COLUMNS)].to_numpy(dtype=np.float64)
    if ((prices < MIN_PRICE) | (prices > MAX_PRICE)).any():
        raise AssertionError(f"every price must be within the bounds [{MIN_PRICE}, {MAX_PRICE}]")


def gap_positions(
    candles: pd.DataFrame, timeframe: Timeframe | TimeframeCode
) -> npt.NDArray[np.intp]:
    """Row positions whose step from the previous open time is longer than the timeframe."""
    steps = np.diff(_datetime_index(candles).to_numpy(dtype="datetime64[us]"))
    longer = steps > np.timedelta64(_duration(timeframe).to_pytimedelta())
    return np.flatnonzero(longer) + 1


def assert_has_gap_features(candles: pd.DataFrame, timeframe: Timeframe | TimeframeCode) -> None:
    """No weekend candles, a dropped weekday slot and a price jump after a gap."""
    index = _datetime_index(candles)
    duration = _duration(timeframe)
    if np.isin(index.dayofweek, WEEKEND_DAYS).any():
        raise AssertionError("no candle may fall on a weekend")
    gaps = gap_positions(candles, timeframe)
    weekday_drops = [
        position
        for position in gaps.tolist()
        if not np.isin(
            pd.date_range(
                index[position - 1] + duration, index[position] - duration, freq=duration
            ).dayofweek,
            WEEKEND_DAYS,
        ).any()
    ]
    if not weekday_drops:
        raise AssertionError("at least one gap must come from a dropped weekday slot")
    opens = candles["open"].to_numpy(dtype=np.float64)
    closes = candles["close"].to_numpy(dtype=np.float64)
    if not (opens[gaps] != closes[gaps - 1]).any():
        raise AssertionError(
            "at least one candle after a gap must open away from the previous close (price jump)"
        )


def assert_has_flat_run(candles: pd.DataFrame) -> None:
    """At least ``MIN_FLAT_RUN`` consecutive flat, zero-volume candles at the previous close."""
    opens, highs, lows, closes, volumes = (
        candles[column].to_numpy(dtype=np.float64)
        for column in ("open", "high", "low", "close", "volume")
    )
    previous_closes = np.concatenate(([np.nan], closes[:-1]))
    flat = (
        (opens == highs)
        & (highs == lows)
        & (lows == closes)
        & (closes == previous_closes)
        & (volumes == 0)
    )
    longest = current = 0
    for is_flat in flat.tolist():
        current = current + 1 if is_flat else 0
        longest = max(longest, current)
    if longest < MIN_FLAT_RUN:
        raise AssertionError(f"longest flat run is {longest}, expected at least {MIN_FLAT_RUN}")


def assert_has_extreme_volume_features(candles: pd.DataFrame) -> None:
    """A volume spike and a zero-volume candle: guaranteed for any generator parameters."""
    volumes = candles["volume"].to_numpy(dtype=np.float64)
    if not (volumes >= VOLUME_SPIKE).any():
        raise AssertionError(f"missing a volume spike (volume >= {VOLUME_SPIKE:g})")
    if not (volumes == 0).any():
        raise AssertionError("missing a zero-volume candle")


def assert_has_extreme_features(candles: pd.DataFrame) -> None:
    """The -90% and +900% candles plus the volume features.

    The price features need prices away from the clipping bounds, so assert them only at the
    default ``start_price`` and ``volatility``; use ``assert_has_extreme_volume_features`` for
    drawn parameters.
    """
    opens = candles["open"].to_numpy(dtype=np.float64)
    closes = candles["close"].to_numpy(dtype=np.float64)
    if not (closes <= 0.1 * opens).any():
        raise AssertionError("missing a -90% candle (close <= 0.1 * open)")
    if not (closes >= 10 * opens).any():
        raise AssertionError("missing a +900% candle (close >= 10 * open)")
    assert_has_extreme_volume_features(candles)


def _datetime_index(candles: pd.DataFrame) -> pd.DatetimeIndex:
    index = candles.index
    if not isinstance(index, pd.DatetimeIndex):
        raise AssertionError(f"expected a DatetimeIndex, got {type(index).__name__}")
    return index


def _duration(timeframe: Timeframe | TimeframeCode) -> pd.Timedelta:
    return pd.Timedelta(Timeframe(timeframe).duration)

"""Tiny, hand-written candle frames for indicator golden cases (spec 005, Design 6.4).

``candles_from_prices`` builds a valid frame from explicit prices: ``open`` equals ``close``,
``high`` and ``low`` default to ``close`` and ``volume`` defaults to ``1.0``. The index holds
daily UTC candle open times from ``2024-01-01``. The frame is checked with ``validate_candles``,
so a typo in a golden case fails loudly instead of testing an invalid frame.

Always import this module as ``tests.fixtures.indicator_frames``.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from trading_bot.domain.candles import OHLCV_COLUMNS, validate_candles

FIRST_OPEN = "2024-01-01"  # UTC


def candles_from_prices(
    close: Sequence[float],
    *,
    high: Sequence[float] | None = None,
    low: Sequence[float] | None = None,
    volume: Sequence[float] | None = None,
) -> pd.DataFrame:
    """A valid daily candle frame with ``open == close`` and the given prices and volumes."""
    closes = np.asarray(close, dtype=np.float64)
    columns = {
        "open": closes,
        "high": closes if high is None else np.asarray(high, dtype=np.float64),
        "low": closes if low is None else np.asarray(low, dtype=np.float64),
        "close": closes,
        "volume": np.ones(len(closes)) if volume is None else np.asarray(volume, dtype=np.float64),
    }
    for name, values in columns.items():
        if values.shape != closes.shape:
            raise ValueError(f"{name} has {values.shape} values; close has {closes.shape}")
    index = pd.date_range(FIRST_OPEN, periods=len(closes), freq="D", tz="UTC", name="open_time")
    frame = pd.DataFrame(columns, index=index, columns=list(OHLCV_COLUMNS), dtype=np.float64)
    return validate_candles(frame)

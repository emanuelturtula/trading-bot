"""Calendar-aligned candle frames for market data tests (spec 010, Design 10.1).

``synthetic_candles`` labels are not on the NYSE grid (its ``1d`` labels are 00:00 UTC, which
the calendar rejects as ``off_grid``), so tests of the data layer and the engine build frames on
the calendar grid here. Values are synthetic only (decision D29): no real market data. There is
no network and no clock.

Always import this module as ``tests.fixtures.session_candles``.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Literal

import numpy as np
import pandas as pd

from tests.fixtures.calendars import NEW_YORK
from tests.fixtures.candles import synthetic_candles
from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe

__all__ = ["provider_shaped", "session_candles"]

_INDEX_DTYPE = pd.DatetimeTZDtype(unit="us", tz="UTC")


def session_candles(
    calendar: MarketCalendar, timeframe: Timeframe, start: datetime, end: datetime, *, seed: int = 0
) -> pd.DataFrame:
    """Seeded RANDOM_WALK candles on the calendar grid, one per slot with start <= label < end.

    The values are those of ``synthetic_candles(len(slots), seed=seed, timeframe=timeframe)``
    and the index holds the slot labels as ``datetime64[us, UTC]``, so the result is a canonical
    frame. No slots give an empty canonical frame.
    """
    slots = calendar.candle_slots(timeframe, start, end)
    index = pd.DatetimeIndex([slot.label for slot in slots], dtype=_INDEX_DTYPE)
    if not slots:
        values = np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
    else:
        values = synthetic_candles(len(slots), seed=seed, timeframe=timeframe).to_numpy(
            dtype=np.float64
        )
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS), dtype=np.float64)


def provider_shaped(
    candles: pd.DataFrame,
    *,
    timezone: tzinfo = NEW_YORK,
    unit: Literal["s", "ms", "us", "ns"] = "s",
    index_name: str = "Datetime",
) -> pd.DataFrame:
    """The candles as a yfinance-like source returns them (synthetic values only, decision D29).

    The index is converted to ``timezone`` with ``unit`` and named ``index_name``. The columns
    are ``Open, High, Low, Close, Adj Close (= Close), Volume (np.rint as int64), Dividends (0.0),
    Stock Splits (0.0)``, the shape of ``Ticker.history(auto_adjust=False)``.
    """
    source = pd.DatetimeIndex(candles.index)
    index = source.tz_convert(timezone).as_unit(unit).rename(index_name)
    close = candles["close"].to_numpy(dtype=np.float64)
    zeros = np.zeros(len(candles), dtype=np.float64)
    return pd.DataFrame(
        {
            "Open": candles["open"].to_numpy(dtype=np.float64),
            "High": candles["high"].to_numpy(dtype=np.float64),
            "Low": candles["low"].to_numpy(dtype=np.float64),
            "Close": close,
            "Adj Close": close.copy(),
            "Volume": np.rint(candles["volume"].to_numpy(dtype=np.float64)).astype(np.int64),
            "Dividends": zeros,
            "Stock Splits": zeros.copy(),
        },
        index=index,
    )

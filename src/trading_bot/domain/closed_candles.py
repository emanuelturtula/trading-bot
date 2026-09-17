"""Open-candle removal: keep only the candles closed at an instant (spec 010, Design 4).

``drop_open_candle`` implements CLAUDE.md rule 4 with the real session closes of the market
calendar (spec 009): a candle is closed when its slot's ``close_time <= now`` (half-open, D19).
For canonical labels that is exactly "labelled at or before the label of the last slot closed at
``now``", because slot closes increase with labels, so one calendar query and one binary search
decide the cut. Every later row is dropped, which keeps a ``now`` in the past correct for
simulations and backtests, and makes the result independent of rows after the cut.

Only the last returned label is checked with the calendar: it is the candle the engine evaluates
and whose label becomes ``candle_close_ts``, so a rejected label there fails loudly. Validating
every label is ``normalize_candles``' job.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from trading_bot.domain.candles import validate_candles
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["drop_open_candle"]


def drop_open_candle(
    candles: pd.DataFrame, timeframe: Timeframe, now: datetime, *, calendar: MarketCalendar
) -> pd.DataFrame:
    """The rows of ``candles`` whose candle is closed at ``now``: a new prefix of the frame.

    Checks run in this order: ``TypeError`` for a non-``Timeframe`` or a non-``MarketCalendar``;
    ``to_utc(now)`` (``TypeError``/``ValueError``); ``validate_candles(candles)``
    (``TypeError``/``CandleValidationError``); ``CalendarRangeError`` when ``now`` is outside the
    calendar or no slot has closed by ``now``. The result is ``candles.iloc[:k]``, with the same
    dtypes and index unit, where ``k`` counts the rows labelled at or before the label of the
    last slot closed at ``now``. When ``k > 0``, the last returned label must be a slot label:
    the calendar's ``CandleLabelError``, ``CalendarRangeError`` or ``ValueError`` propagates.
    ``candles`` is never modified. Labels are expected to be canonical (``normalize_candles``).
    """
    if not isinstance(timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    instant = to_utc(now)
    validate_candles(candles)
    last = calendar.closed_candles(timeframe, instant, 1)[-1]
    index = pd.DatetimeIndex(candles.index)
    count = int(index.searchsorted(pd.Timestamp(last.label), side="right"))
    closed = candles.iloc[:count]
    if count > 0:
        calendar.candle_slot(timeframe, index[count - 1])
    return closed

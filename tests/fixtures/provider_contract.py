"""The executable return contract of ``MarketDataProvider.fetch_candles`` (spec 010, Design 10.3).

#10 runs ``assert_closed_candles`` on every recorded-fixture result and #14 on the fake's
output, so both check one statement of the contract. Every check raises ``AssertionError`` with
a message explicitly: pytest only rewrites bare ``assert`` statements inside test modules, and
they vanish under ``python -O``.

Always import this module as ``tests.fixtures.provider_contract``.
"""

from __future__ import annotations

import pandas as pd

from trading_bot.data.provider import CandleRequest
from trading_bot.domain.candles import CandleValidationError, validate_candles
from trading_bot.domain.market_calendar.sessions import CalendarError, MarketCalendar

__all__ = ["assert_closed_candles"]

_CANONICAL_INDEX_DTYPE = pd.DatetimeTZDtype(unit="us", tz="UTC")


def assert_closed_candles(
    frame: pd.DataFrame, request: CandleRequest, *, calendar: MarketCalendar
) -> None:
    """Raise AssertionError unless ``frame`` meets the fetch_candles return contract (Design 7.2).

    The frame passes ``validate_candles`` with a ``datetime64[us, UTC]`` index, is not empty,
    has at most ``request.lookback`` rows, every label is a slot label whose slot closed at or
    before ``request.now``, and the last label is the last slot closed at ``request.now``.
    """
    try:
        validate_candles(frame)
    except (CandleValidationError, TypeError) as error:
        raise AssertionError(f"not a valid candle frame: {error}") from error
    if frame.index.dtype != _CANONICAL_INDEX_DTYPE:
        raise AssertionError(
            f"the index dtype must be datetime64[us, UTC], got {frame.index.dtype}"
        )
    if len(frame) == 0:
        raise AssertionError("the frame must not be empty")
    if len(frame) > request.lookback:
        raise AssertionError(
            f"the frame has {len(frame)} rows, more than lookback={request.lookback}"
        )
    labels = pd.DatetimeIndex(frame.index)
    for label in labels:
        try:
            slot = calendar.candle_slot(request.timeframe, label)
        except CalendarError as error:
            raise AssertionError(
                f"label {label.isoformat()} is not a {request.timeframe} slot label: {error}"
            ) from error
        if slot.close_time > request.now:
            raise AssertionError(
                f"the candle labelled {label.isoformat()} closes at "
                f"{slot.close_time.isoformat()}, after now {request.now.isoformat()}"
            )
    expected = calendar.closed_candles(request.timeframe, request.now, 1)[-1].label
    if labels[-1] != expected:
        raise AssertionError(
            f"the last label is {labels[-1].isoformat()}, but the last closed "
            f"{request.timeframe} slot at {request.now.isoformat()} is {expected.isoformat()}"
        )

"""From a provider's raw frame to the frame the engine evaluates (spec 010, Design 8).

``candle_window`` turns a ``CandleRequest`` into the calendar span a provider must fetch.
``prepare_candles`` is how every provider finishes a fetch: it normalizes the raw frame, logs
the dropped rows, drops the candles that are not closed at ``now`` and requires the last row to
be the last slot closed at ``now`` (decisions D32, D33 and D43). The engine can therefore never
evaluate a stale frame by accident: a missing or invalid last candle is a typed, retryable
``CandleNotPublishedError``.

This is the only module of the data layer that logs. Records carry counts, reason codes, the
normalized ticker, the timeframe code and ISO labels: never frame values, raw column labels or
provider text.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import pandas as pd

from trading_bot.data.errors import (
    CandleNotPublishedError,
    NoDataError,
    ProviderDataError,
    UnpublishedReason,
)
from trading_bot.data.provider import CandleRequest
from trading_bot.domain.candle_normalization import (
    CandleNormalizationError,
    DroppedRow,
    DropReason,
    normalize_candles,
)
from trading_bot.domain.closed_candles import drop_open_candle
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar

__all__ = ["CandleWindow", "candle_window", "prepare_candles"]

_LOGGER_NAME: Final = "trading_bot.data.pipeline"
_DROPPED_MESSAGE: Final = "dropped %d %s candle rows for %s %s (first %s, last %s)"


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleWindow:
    """The closed slots a fetch must cover: the oldest and the newest of ``lookback`` slots."""

    first: CandleSlot  # the oldest of the `lookback` closed slots
    last: CandleSlot  # the last slot closed at `now`

    @property
    def start(self) -> datetime:
        """``first.open_time``: the earliest instant the fetch must cover."""
        return self.first.open_time

    @property
    def end(self) -> datetime:
        """``last.close_time``: the real close of the last closed candle."""
        return self.last.close_time


def candle_window(request: CandleRequest, *, calendar: MarketCalendar) -> CandleWindow:
    """The window of ``calendar.closed_candles(request.timeframe, request.now, request.lookback)``.

    Providers that request by date use ``first.session_day`` and ``last.session_day``.
    ``CalendarRangeError`` propagates when the calendar does not hold that many closed slots.
    """
    _check_arguments(request, calendar)
    slots = calendar.closed_candles(request.timeframe, request.now, request.lookback)
    return CandleWindow(first=slots[0], last=slots[-1])


def prepare_candles(
    raw: pd.DataFrame,
    request: CandleRequest,
    *,
    calendar: MarketCalendar,
    logger: logging.Logger | None = None,
) -> pd.DataFrame:
    """The last ``request.lookback`` valid candles closed at ``request.now``, ending at the last
    slot closed at ``request.now``.

    Steps (Design 8.2): normalize ``raw`` (a ``CandleNormalizationError`` becomes
    ``ProviderDataError`` raised ``from None``); find the last slot closed at ``now``
    (``CalendarRangeError`` propagates); log the dropped rows; drop the candles not closed at
    ``now``; require the last row to be that slot, otherwise raise ``CandleNotPublishedError``
    (``invalid`` when a dropped row had its label, ``missing`` otherwise) or ``NoDataError``
    when no closed candle remains and no dropped row had its label. ``raw`` is never modified.
    ``logger`` defaults to ``trading_bot.data.pipeline``.
    """
    _check_arguments(request, calendar)
    try:
        normalized = normalize_candles(raw, request.timeframe, calendar=calendar)
    except CandleNormalizationError as error:
        column = "" if error.column is None else f" (column {error.column})"
        raise ProviderDataError(
            error.kind.value,
            f"the provider response cannot be normalized{column}",
            ticker=request.ticker,
            timeframe=request.timeframe,
        ) from None
    expected = calendar.closed_candles(request.timeframe, request.now, 1)[-1].label
    _log_dropped(
        normalized.dropped,
        expected,
        request,
        logging.getLogger(_LOGGER_NAME) if logger is None else logger,
    )
    closed = drop_open_candle(normalized.candles, request.timeframe, request.now, calendar=calendar)
    labels = pd.DatetimeIndex(closed.index)
    if len(labels) > 0 and labels[-1] == expected:
        return closed.iloc[-request.lookback :]

    last_label = labels[-1] if len(labels) > 0 else None
    if any(row.label is not None and row.label == expected for row in normalized.dropped):
        reason = UnpublishedReason.INVALID
    elif last_label is None:
        raise NoDataError(
            f"no valid candle closed by {request.now.isoformat()}",
            ticker=request.ticker,
            timeframe=request.timeframe,
        )
    else:
        reason = UnpublishedReason.MISSING
    raise CandleNotPublishedError(
        reason,
        ticker=request.ticker,
        timeframe=request.timeframe,
        expected_label=expected,
        last_label=last_label,
    )


def _check_arguments(request: object, calendar: object) -> None:
    if not isinstance(request, CandleRequest):
        raise TypeError(f"request must be a CandleRequest, got {type(request).__name__}")
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")


def _log_dropped(
    dropped: Sequence[DroppedRow],
    expected: datetime,
    request: CandleRequest,
    logger: logging.Logger,
) -> None:
    """One record per reason and level, in ``DropReason`` order (Design 8.4).

    Rows labelled at or before the last closed slot (or ``NaT``) are ``WARNING``: they were
    closed candles the provider sent broken. Later rows belong to the in-progress candle and
    are ``DEBUG``, and so are exact duplicates, which lose nothing.
    """
    by_reason: dict[DropReason, list[DroppedRow]] = {}
    for row in dropped:
        by_reason.setdefault(row.reason, []).append(row)
    for reason in DropReason:
        rows = by_reason.get(reason, [])
        groups: tuple[tuple[int, list[DroppedRow]], ...]
        if reason is DropReason.DUPLICATE:
            groups = ((logging.DEBUG, rows),)
        else:
            closed = [row for row in rows if row.label is None or row.label <= expected]
            later = [row for row in rows if row.label is not None and row.label > expected]
            groups = ((logging.WARNING, closed), (logging.DEBUG, later))
        for level, group in groups:
            if not group:
                continue
            stamps = [row.label for row in group if row.label is not None]
            first = min(stamps).isoformat() if stamps else "none"
            last = max(stamps).isoformat() if stamps else "none"
            logger.log(
                level,
                _DROPPED_MESSAGE,
                len(group),
                reason.value,
                request.ticker,
                request.timeframe.value,
                first,
                last,
            )

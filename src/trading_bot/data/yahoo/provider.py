"""The Yahoo ``MarketDataProvider`` (spec 011, Design 10; D48, D51, D56-D58, D65, D67 and D68).

``YFinanceProvider.fetch_candles`` plans the request (``plan_history``: the calendar window,
capped at 720 days back for ``1h`` and ``4h``), fetches ``1h`` or ``1d`` bars through the
transport, and finishes with ``prepare_candles``. ``4h`` candles are built from normalized
hourly bars on the session grid (``resample_hourly_to_4h``); the last ``4h`` slot closed at
``now`` is withheld while its closing hourly bar is missing (``publishable_4h``), except for the
truncated final half hour of an early-close session, which Yahoo never publishes
(``is_unpublished_hour``). ``validate_ticker`` reads the chart metadata of a one-month daily
request and applies the v1 instrument policy.

Everything after the client call runs in its own worker thread (``_prepare``, decision D67), so
the event loop that also drives the scheduler and the Telegram poller keeps running while a
frame is normalized, resampled and checked. That worker is outside the transport: it has no
attempt timeout, takes no pacing token and holds no in-flight slot, and the errors it raises
reach the caller unchanged, consume no attempt and trigger no retry.

The provider reads no clock: every decision uses the explicit ``now``. Its only mutable state is
the set of ``(symbol, timeframe)`` pairs already logged as capped. It belongs to the event loop
that first uses it, through its transport (decision D68). It imports no yfinance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final

import pandas as pd

from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    NoDataError,
    ProviderDataError,
)
from trading_bot.data.pipeline import candle_window, log_dropped_rows, prepare_candles
from trading_bot.data.provider import CandleRequest
from trading_bot.data.tickers import TickerInfo, ensure_supported, parse_ticker
from trading_bot.data.transport import ProviderTransport
from trading_bot.data.yahoo.history import HistoryQuery, YahooClient, YahooInterval
from trading_bot.data.yahoo.instruments import check_yahoo_symbol, ticker_info
from trading_bot.domain.candle_normalization import CandleNormalizationError, normalize_candles
from trading_bot.domain.candle_resampling import ResampledCandles, resample_hourly_to_4h
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "INTRADAY_HISTORY",
    "HistoryPlan",
    "YFinanceProvider",
    "is_unpublished_hour",
    "plan_history",
    "publishable_4h",
]

INTRADAY_HISTORY: Final = timedelta(days=720)

_LOGGER_NAME: Final = "trading_bot.data.yahoo.provider"
_ONE_MICROSECOND: Final = timedelta(microseconds=1)
_ONE_HOUR: Final = timedelta(hours=1)
_REGULAR_CLOSE: Final = time(16, 0)
_CAPPED_MESSAGE: Final = "Yahoo history for %s %s capped: requested from %s, available from %s"
_WITHHELD_MESSAGE: Final = "withheld %s %s candle %s: hourly bar %s is missing"
_EARLIER_GAPS_MESSAGE: Final = (
    "built %d earlier %s %s candles from incomplete hourly bars (first %s, last %s)"
)
_FINAL_GAP_MESSAGE: Final = "built %s %s candle %s from %d of %d hourly bars (missing %s)"


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryPlan:
    """What to ask Yahoo for one ``fetch_candles`` request."""

    interval: YahooInterval
    start: datetime  # stdlib UTC; sent to Yahoo
    requested_start: datetime  # the label of candle_window(request).first
    capped: bool


def plan_history(request: CandleRequest, *, calendar: MarketCalendar) -> HistoryPlan:
    """The interval and start of the Yahoo request for ``request`` (Design 10.1).

    The start is the label of the first slot of ``candle_window`` (``CalendarRangeError``
    propagates). ``1d`` is never capped. For ``1h`` and ``4h`` a start before
    ``request.now - INTRADAY_HISTORY`` becomes the label of the first slot of the timeframe at or
    after that instant, and the plan is ``capped``.
    """
    if not isinstance(request, CandleRequest):
        raise TypeError(f"request must be a CandleRequest, got {type(request).__name__}")
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    window = candle_window(request, calendar=calendar)
    requested = window.first.label
    if request.timeframe is Timeframe.D1:
        return HistoryPlan(interval="1d", start=requested, requested_start=requested, capped=False)
    earliest = request.now - INTRADAY_HISTORY
    if requested >= earliest:
        return HistoryPlan(interval="1h", start=requested, requested_start=requested, capped=False)
    # Never empty: the last closed slot is after `earliest`.
    first = calendar.candle_slots(
        request.timeframe, earliest, window.last.label + _ONE_MICROSECOND
    )[0]
    return HistoryPlan(interval="1h", start=first.label, requested_start=requested, capped=True)


def is_unpublished_hour(slot: CandleSlot, *, calendar: MarketCalendar) -> bool:
    """Whether ``slot`` is the truncated final hour of an early-close session (Design 10.4).

    True exactly when the ``1h`` slot lasts less than one hour and closes before 16:00 in the
    calendar time zone: Yahoo never publishes that half hour as hourly data. Other timeframes
    raise ``ValueError``.
    """
    if not isinstance(slot, CandleSlot):
        raise TypeError(f"slot must be a CandleSlot, got {type(slot).__name__}")
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    if slot.timeframe is not Timeframe.H1:
        raise ValueError(f"slot must be a 1h slot, got a {slot.timeframe} slot")
    closes_early = slot.close_time.astimezone(calendar.timezone).time() < _REGULAR_CLOSE
    return slot.close_time - slot.open_time < _ONE_HOUR and closes_early


def publishable_4h(
    resampled: ResampledCandles, *, last: CandleSlot, calendar: MarketCalendar
) -> pd.DataFrame:
    """The resampled ``4h`` candles without an unpublished last slot (Design 10.4; D58, D65).

    The row of ``last`` (the last ``4h`` slot closed at ``now``) is removed when its closing
    hourly bar is missing, unless that bar is the unpublished final hour of an early-close
    session and the hour before it is present. Otherwise ``resampled.candles`` is returned.
    """
    if not isinstance(resampled, ResampledCandles):
        raise TypeError(f"resampled must be a ResampledCandles, got {type(resampled).__name__}")
    if not isinstance(last, CandleSlot):
        raise TypeError(f"last must be a CandleSlot, got {type(last).__name__}")
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    if not resampled.slots:
        return resampled.candles
    entry = resampled.slots[-1]
    if entry.slot != last or not entry.closing_bar_missing:
        return resampled.candles
    hours = calendar.candle_slots(Timeframe.H1, last.open_time, last.close_time)
    # ``hours[-2]`` always exists here: ``last`` has a row, so one of its hourly slots has a bar,
    # while ``closing_bar_missing`` says the last one has none. A length guard would be an
    # unreachable branch.
    if is_unpublished_hour(hours[-1], calendar=calendar) and hours[-2].label not in entry.missing:
        return resampled.candles
    return resampled.candles.iloc[:-1]


class YFinanceProvider:
    """The ``MarketDataProvider`` backed by Yahoo Finance. It never places orders.

    Through its transport, a provider belongs to the event loop that first uses it (D68): build
    one inside the running loop and never share it with another loop.
    """

    def __init__(
        self,
        *,
        client: YahooClient,
        transport: ProviderTransport,
        calendar: MarketCalendar,
        # default: logging.getLogger("trading_bot.data.yahoo.provider")
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(transport, ProviderTransport):
            raise TypeError(
                f"transport must be a ProviderTransport, got {type(transport).__name__}"
            )
        if not isinstance(calendar, MarketCalendar):
            raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
        if logger is not None and not isinstance(logger, logging.Logger):
            raise TypeError(f"logger must be a logging.Logger, got {type(logger).__name__}")
        self._client = client
        self._transport = transport
        self._calendar = calendar
        self._logger = logging.getLogger(_LOGGER_NAME) if logger is None else logger
        self._capped_logged: set[tuple[str, Timeframe]] = set()

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame:
        """The last ``lookback`` candles of ``ticker`` closed at ``now`` (Design 10.2).

        Arguments, the symbol and the plan are checked before any client call; they and the
        capped-history record are the only work on the event loop besides awaiting. The client
        call goes through the transport, and everything after it runs in a worker thread
        (``_prepare``, D67), whose errors reach the caller unchanged without consuming an
        attempt. Errors are those of the ``MarketDataProvider`` contract.
        """
        request = CandleRequest(ticker=ticker, timeframe=timeframe, lookback=lookback, now=now)
        symbol = check_yahoo_symbol(request.ticker)
        plan = plan_history(request, calendar=self._calendar)
        if plan.capped:
            self._log_capped(symbol, request.timeframe, plan)
        query = HistoryQuery(symbol=symbol, interval=plan.interval, start=plan.start)
        history = await self._transport.call(
            lambda: self._client.history(query), ticker=symbol, timeframe=request.timeframe
        )
        return await asyncio.to_thread(self._prepare, history.frame, request, symbol)

    async def validate_ticker(self, ticker: str) -> TickerInfo:
        """The metadata of a supported instrument (Design 10.7).

        Malformed and ISIN-shaped text makes no client call. A ``NoDataError`` from the client
        becomes ``InvalidTickerError(not_found)``, raised ``from None``.
        """
        symbol = check_yahoo_symbol(parse_ticker(ticker))
        query = HistoryQuery(symbol=symbol, interval="1d", period="1mo")
        try:
            history = await self._transport.call(
                lambda: self._client.history(query), ticker=symbol, timeframe=None
            )
        except NoDataError:
            raise InvalidTickerError(
                InvalidTickerReason.NOT_FOUND,
                "Yahoo returned no recent data for this symbol",
                ticker=symbol,
            ) from None
        return ensure_supported(ticker_info(symbol, history.metadata))

    async def aclose(self) -> None:
        """Close the transport: wait for running workers and refuse later calls."""
        await self._transport.aclose()

    def _prepare(self, frame: pd.DataFrame, request: CandleRequest, symbol: str) -> pd.DataFrame:
        """The post-fetch processing (Design 10.2, step 7), run in a worker thread (D67).

        ``1h`` and ``1d`` only run ``prepare_candles``; ``4h`` normalizes the hourly frame, logs
        its dropped rows as ``1h``, resamples, withholds an unpublished last slot, logs the gaps
        and then runs ``prepare_candles``. It reads no clock and touches no yfinance state.
        """
        if request.timeframe is not Timeframe.H4:
            return prepare_candles(frame, request, calendar=self._calendar)
        return prepare_candles(
            self._build_4h(frame, request, symbol), request, calendar=self._calendar
        )

    def _build_4h(self, frame: pd.DataFrame, request: CandleRequest, symbol: str) -> pd.DataFrame:
        """Design 10.2, steps 7.2 to 7.6, with the records of Design 10.5."""
        calendar = self._calendar
        try:
            hourly = normalize_candles(frame, Timeframe.H1, calendar=calendar)
        except CandleNormalizationError as error:
            column = "" if error.column is None else f" (column {error.column})"
            raise ProviderDataError(
                error.kind.value,
                f"the provider response cannot be normalized{column}",
                ticker=symbol,
                timeframe=Timeframe.H4,
            ) from None
        log_dropped_rows(
            hourly.dropped,
            ticker=symbol,
            timeframe=Timeframe.H1,
            last_label=calendar.closed_candles(Timeframe.H1, request.now, 1)[-1].label,
        )
        resampled = resample_hourly_to_4h(hourly.candles, request.now, calendar=calendar)
        last = calendar.closed_candles(Timeframe.H4, request.now, 1)[-1]
        publishable = publishable_4h(resampled, last=last, calendar=calendar)
        self._log_4h(symbol, resampled, len(publishable), last)
        return publishable

    def _log_4h(
        self, symbol: str, resampled: ResampledCandles, returned: int, last: CandleSlot
    ) -> None:
        code = Timeframe.H4.value
        if returned < len(resampled.slots):
            self._logger.debug(
                _WITHHELD_MESSAGE,
                symbol,
                code,
                last.label.isoformat(),
                resampled.slots[-1].missing[-1].isoformat(),
            )
        entries = resampled.slots[:returned]
        final = entries[-1] if entries and entries[-1].slot == last else None
        earlier = [
            entry
            for entry in entries
            if entry is not final and self._relevant_missing(entry.missing)
        ]
        if earlier:
            self._logger.debug(
                _EARLIER_GAPS_MESSAGE,
                len(earlier),
                symbol,
                code,
                earlier[0].slot.label.isoformat(),
                earlier[-1].slot.label.isoformat(),
            )
        if final is not None and (missing := self._relevant_missing(final.missing)):
            self._logger.warning(
                _FINAL_GAP_MESSAGE,
                symbol,
                code,
                final.slot.label.isoformat(),
                final.bars,
                final.bars + len(final.missing),
                ", ".join(label.isoformat() for label in missing),
            )

    def _relevant_missing(self, missing: tuple[datetime, ...]) -> list[datetime]:
        """The missing hours Yahoo would have published: unpublished hours are not gaps."""
        return [
            label
            for label in missing
            if not is_unpublished_hour(
                self._calendar.candle_slot(Timeframe.H1, label), calendar=self._calendar
            )
        ]

    def _log_capped(self, symbol: str, timeframe: Timeframe, plan: HistoryPlan) -> None:
        key = (symbol, timeframe)
        level = logging.DEBUG if key in self._capped_logged else logging.INFO
        self._capped_logged.add(key)
        self._logger.log(
            level,
            _CAPPED_MESSAGE,
            symbol,
            timeframe.value,
            plan.requested_start.isoformat(),
            plan.start.isoformat(),
        )

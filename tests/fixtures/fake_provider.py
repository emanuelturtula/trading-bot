"""An in-memory ``MarketDataProvider`` for engine and scheduler tests (spec 010, Design 10.2).

``FakeMarketDataProvider`` runs the real ``prepare_candles`` on stored raw frames, so engine
tests only ever see frames production can return. Scripted ``MarketDataError`` failures cover
isolation and retry paths, and recorded calls let tests assert what was asked. There is no
network and no clock: every instant comes from the caller.

Always import this module as ``tests.fixtures.fake_provider``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
)
from trading_bot.data.pipeline import prepare_candles
from trading_bot.data.provider import CandleRequest, MarketDataProvider
from trading_bot.data.tickers import TickerInfo, ensure_supported, parse_ticker
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe

__all__ = ["FakeMarketDataProvider", "FetchCall", "as_market_data_provider"]

type ProviderMethod = Literal["fetch_candles", "validate_ticker"]

_METHODS: tuple[ProviderMethod, ...] = ("fetch_candles", "validate_ticker")


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchCall:
    """One recorded ``fetch_candles`` call, with the validated values of its request."""

    ticker: str  # normalized
    timeframe: Timeframe
    lookback: int
    now: datetime  # stdlib UTC


class FakeMarketDataProvider:
    """In-memory MarketDataProvider for engine and scheduler tests: no network, no clock."""

    def __init__(self, *, calendar: MarketCalendar) -> None:
        if not isinstance(calendar, MarketCalendar):
            raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
        self._calendar = calendar
        self._fetch_calls: list[FetchCall] = []
        self._validate_calls: list[str] = []
        self._tickers: dict[str, TickerInfo] = {}
        self._candles: dict[tuple[str, Timeframe], pd.DataFrame] = {}
        self._failures: dict[tuple[str, ProviderMethod], deque[MarketDataError]] = {}

    @property
    def fetch_calls(self) -> tuple[FetchCall, ...]:
        return tuple(self._fetch_calls)

    @property
    def validate_calls(self) -> tuple[str, ...]:
        """The texts passed to ``validate_ticker``, as given."""
        return tuple(self._validate_calls)

    def add_ticker(self, info: TickerInfo) -> None:
        """Make ``info`` known; it replaces an entry with the same symbol."""
        if not isinstance(info, TickerInfo):
            raise TypeError(f"info must be a TickerInfo, got {type(info).__name__}")
        self._tickers[info.symbol] = info

    def set_candles(self, ticker: str, timeframe: Timeframe, raw: pd.DataFrame) -> None:
        """Store a copy of ``raw``, the frame "as published", in any shape normalization accepts.

        Replacing it simulates a newly published candle.
        """
        symbol = normalize_ticker(ticker)
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"timeframe must be a Timeframe, got {type(timeframe).__name__}")
        if not isinstance(raw, pd.DataFrame):
            raise TypeError(f"raw must be a pandas DataFrame, got {type(raw).__name__}")
        self._candles[(symbol, timeframe)] = raw.copy(deep=True)

    def fail_next(
        self,
        error: MarketDataError,
        *,
        ticker: str,
        method: ProviderMethod = "fetch_candles",
        times: int = 1,
    ) -> None:
        """Raise ``error`` on the next ``times`` calls of ``method`` for ``ticker`` (FIFO)."""
        if not isinstance(error, MarketDataError):
            raise TypeError(f"error must be a MarketDataError, got {type(error).__name__}")
        symbol = normalize_ticker(ticker)
        if method not in _METHODS:
            raise ValueError(f"method must be one of {', '.join(_METHODS)}")
        if isinstance(times, bool) or not isinstance(times, int):
            raise TypeError(f"times must be an int, got {type(times).__name__}")
        if times < 1:
            raise ValueError(f"times must be at least 1, got {times}")
        self._failures.setdefault((symbol, method), deque()).extend([error] * times)

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame:
        await asyncio.sleep(0)
        request = CandleRequest(ticker=ticker, timeframe=timeframe, lookback=lookback, now=now)
        self._fetch_calls.append(
            FetchCall(
                ticker=request.ticker,
                timeframe=request.timeframe,
                lookback=request.lookback,
                now=request.now,
            )
        )
        self._raise_scripted_failure(request.ticker, "fetch_candles")
        stored = self._candles.get((request.ticker, request.timeframe))
        if stored is None:
            raise NoDataError(
                f"no candles are stored at {request.now.isoformat()}",
                ticker=request.ticker,
                timeframe=request.timeframe,
            )
        return prepare_candles(stored, request, calendar=self._calendar)

    async def validate_ticker(self, ticker: str) -> TickerInfo:
        await asyncio.sleep(0)
        if not isinstance(ticker, str):
            raise TypeError(f"ticker must be a str, got {type(ticker).__name__}")
        self._validate_calls.append(ticker)
        symbol = parse_ticker(ticker)
        self._raise_scripted_failure(symbol, "validate_ticker")
        info = self._tickers.get(symbol)
        if info is None:
            raise InvalidTickerError(
                InvalidTickerReason.NOT_FOUND,
                "the provider does not know this ticker",
                ticker=symbol,
            )
        return ensure_supported(info)

    def _raise_scripted_failure(self, symbol: str, method: ProviderMethod) -> None:
        queue = self._failures.get((symbol, method))
        if queue:
            # A fresh traceback per raise: an instance scripted several times does not
            # accumulate the frames of earlier raises.
            raise queue.popleft().with_traceback(None)


def as_market_data_provider(provider: FakeMarketDataProvider) -> MarketDataProvider:
    """Returns its argument; strict mypy checks that the fake conforms to the Protocol (AC13)."""
    return provider

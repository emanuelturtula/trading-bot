"""The market data provider port (spec 010, Design 7; decisions D36, D37).

``MarketDataProvider`` is the async contract every candle source implements (``YFinanceProvider``
in #10, ``FakeMarketDataProvider`` in tests). The engine types its provider as this Protocol, and
``main.py`` injects the implementation. ``now`` is explicit: providers never read the wall clock,
so one ``now`` per run keeps retries and simulated clocks deterministic. ``lookback`` counts
closed candles, bounded by ``MAX_LOOKBACK``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

import pandas as pd

from trading_bot.data.tickers import TickerInfo, parse_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["MAX_LOOKBACK", "CandleRequest", "MarketDataProvider"]

# The largest rule warmup of the catalog is 2 255 candles (ema length=500 plus a crossover).
MAX_LOOKBACK: Final = 5_000


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleRequest:
    """The validated arguments of ``fetch_candles``, with normalized values.

    Fields are checked in order: ``ticker`` through ``parse_ticker`` (``TypeError`` or
    ``InvalidTickerError`` with reason ``malformed``); ``timeframe`` must be a ``Timeframe``
    (``TypeError``); ``lookback`` must be an ``int`` that is not a ``bool`` (``TypeError``) in
    ``[1, MAX_LOOKBACK]`` (``ValueError``); ``now`` goes through ``to_utc`` and is stored as a
    stdlib UTC ``datetime`` (``TypeError``/``ValueError``).
    """

    ticker: str
    timeframe: Timeframe
    lookback: int
    now: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", parse_ticker(self.ticker))
        if not isinstance(self.timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe, got {type(self.timeframe).__name__} "
                "(use Timeframe.parse for text)"
            )
        if isinstance(self.lookback, bool) or not isinstance(self.lookback, int):
            raise TypeError(f"lookback must be an int, got {type(self.lookback).__name__}")
        if not 1 <= self.lookback <= MAX_LOOKBACK:
            raise ValueError(f"lookback must be in [1, {MAX_LOOKBACK}], got {int(self.lookback)}")
        object.__setattr__(self, "lookback", int(self.lookback))
        object.__setattr__(self, "now", to_utc(self.now))


class MarketDataProvider(Protocol):
    """An async source of closed candles and ticker metadata. It never places orders.

    Both methods are safe to call concurrently from one event loop and never read the wall clock.
    """

    async def fetch_candles(
        self, ticker: str, timeframe: Timeframe, lookback: int, *, now: datetime
    ) -> pd.DataFrame:
        """The last ``lookback`` candles of ``ticker`` closed at ``now``.

        - Validates its arguments with ``CandleRequest`` before any I/O.
        - Builds its result with ``trading_bot.data.pipeline.prepare_candles``, so the frame
          passes ``validate_candles`` with a ``datetime64[us, UTC]`` index; every label is a
          canonical slot label whose slot closed at or before ``now``; the last label is the
          last slot closed at ``now``; and it has between 1 and ``lookback`` rows (fewer only
          when the history is shorter or a provider limit caps it, which the provider logs).
        - Raises only ``TypeError``/``ValueError`` for invalid arguments; ``InvalidTickerError``
          (``malformed``, ``not_found``); ``NoDataError``, ``CandleNotPublishedError``,
          ``ProviderDataError`` and ``ProviderUnavailableError``; and ``CalendarRangeError`` when
          the injected calendar does not cover the window (a configuration error, propagated
          unchanged). Any other exception is a provider bug. Errors raised while handling a
          library or network exception use ``raise ... from None``.
        """

    async def validate_ticker(self, ticker: str) -> TickerInfo:
        """The metadata of a supported instrument.

        Parses ``ticker`` with ``parse_ticker`` and returns ``ensure_supported(info)`` for an
        instrument the provider knows. Raises only ``TypeError``, ``InvalidTickerError`` (any
        reason), ``ProviderDataError`` and ``ProviderUnavailableError``.
        """

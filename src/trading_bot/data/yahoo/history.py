"""Yahoo history types and the ``YahooClient`` port (spec 011, Design 6).

``HistoryQuery`` is one validated ``Ticker.history`` request, ``ChartMetadata`` the six chart
metadata fields the provider reads, and ``YahooHistory`` a client answer. ``YahooClient`` is the
blocking port the provider calls through the transport; ``client.YFinanceClient`` implements it
with yfinance, and tests replay recordings through it. This module imports neither yfinance nor
any HTTP library.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

import pandas as pd

from trading_bot.domain.utc import to_utc

__all__ = [
    "ISIN_PATTERN",
    "YAHOO_SYMBOL_PATTERN",
    "ChartMetadata",
    "HistoryQuery",
    "YahooClient",
    "YahooHistory",
    "YahooInterval",
]

YAHOO_SYMBOL_PATTERN: Final = re.compile(r"[A-Z0-9^][A-Z0-9.^=-]{0,23}")  # used with fullmatch
ISIN_PATTERN: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")  # used with fullmatch

type YahooInterval = Literal["1h", "1d"]

_INTERVALS: Final = ("1h", "1d")
_PERIOD: Final = "1mo"


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryQuery:
    """One history request: a ``start`` query or a one-month ``period`` query.

    Validated in this order: ``symbol`` is a ``str`` (``TypeError``) that fully matches
    ``YAHOO_SYMBOL_PATTERN`` and does not fully match ``ISIN_PATTERN`` (``ValueError``);
    ``interval`` is ``"1h"`` or ``"1d"`` (``ValueError``); exactly one of ``start`` and
    ``period`` is set (``ValueError``); ``start`` goes through ``to_utc`` and is stored as a
    stdlib UTC ``datetime``; ``period``, when set, is ``"1mo"`` (``ValueError``). These are
    programming errors: the provider validates user text first.
    """

    symbol: str
    interval: YahooInterval
    start: datetime | None = None  # stdlib UTC; exactly one of start and period
    period: Literal["1mo"] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise TypeError(f"symbol must be a str, got {type(self.symbol).__name__}")
        if (
            YAHOO_SYMBOL_PATTERN.fullmatch(self.symbol) is None
            or ISIN_PATTERN.fullmatch(self.symbol) is not None
        ):
            raise ValueError("symbol must be a checked Yahoo symbol (use check_yahoo_symbol)")
        object.__setattr__(self, "symbol", str.__str__(self.symbol))
        if not (isinstance(self.interval, str) and self.interval in _INTERVALS):
            raise ValueError(f"interval must be one of {', '.join(_INTERVALS)}")
        if (self.start is None) == (self.period is None):
            raise ValueError("exactly one of start and period must be set")
        if self.start is not None:
            object.__setattr__(self, "start", to_utc(self.start))
        if self.period is not None and not (
            isinstance(self.period, str) and self.period == _PERIOD
        ):
            raise ValueError(f"period must be {_PERIOD}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChartMetadata:
    """The chart metadata fields the provider reads; ``None`` when absent or not a ``str``."""

    symbol: str | None
    instrument_type: str | None
    exchange_name: str | None
    currency: str | None
    long_name: str | None
    short_name: str | None

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, object]) -> ChartMetadata:
        """Read ``symbol``, ``instrumentType``, ``exchangeName``, ``currency``, ``longName`` and
        ``shortName`` with one ``metadata.get(key)`` each, keeping ``str`` values only.

        The mapping is never iterated and ``tradingPeriods`` is never read: yfinance loads it
        lazily with an extra request. A non-``Mapping`` raises ``TypeError``.
        """
        if not isinstance(metadata, Mapping):
            raise TypeError(f"metadata must be a Mapping, got {type(metadata).__name__}")
        return cls(
            symbol=_text(metadata.get("symbol")),
            instrument_type=_text(metadata.get("instrumentType")),
            exchange_name=_text(metadata.get("exchangeName")),
            currency=_text(metadata.get("currency")),
            long_name=_text(metadata.get("longName")),
            short_name=_text(metadata.get("shortName")),
        )


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class YahooHistory:
    """A client answer. Compared by identity (it holds a frame)."""

    frame: pd.DataFrame  # exactly as yfinance returned it
    metadata: ChartMetadata


class YahooClient(Protocol):
    """The blocking Yahoo port the provider calls through ``ProviderTransport``."""

    def history(self, query: HistoryQuery) -> YahooHistory:
        """The price history and chart metadata of ``query``.

        A blocking call, run in a worker thread by the transport. It raises only
        ``MarketDataError`` subclasses (the error mapping of Design 8.3) and ``BaseException``s
        that are not ``Exception``.
        """
        ...


def _text(value: object) -> str | None:
    return str.__str__(value) if isinstance(value, str) else None

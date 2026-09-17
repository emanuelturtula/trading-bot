"""Ticker metadata and the v1 instrument policy (spec 010, Design 6; decisions D27, D39).

``TickerInfo`` describes any instrument a provider knows, including unsupported ones, so the
policy can be tested on values and callers can explain a rejection. ``ensure_supported`` is the
single implementation of decision D27 (US-listed stocks and ETFs quoted in USD): every provider
calls it before ``validate_ticker`` returns, so a test double rejects exactly what production
rejects. OTC markets map to ``Exchange.OTHER``: OTC quotes are not exchange listings.

``TickerInfo.name`` is provider text: it is validated as printable and bounded but never echoed
in messages, and presentation layers must escape it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason
from trading_bot.domain.signals import normalize_ticker

__all__ = [
    "SUPPORTED_ASSET_TYPES",
    "SUPPORTED_CURRENCY",
    "AssetType",
    "Exchange",
    "TickerInfo",
    "ensure_supported",
    "parse_ticker",
]

_MAX_NAME_LENGTH: Final = 120
_MAX_CURRENCY_LENGTH: Final = 8


class AssetType(StrEnum):
    """Instrument type as the provider reports it; only ``equity`` and ``etf`` are supported."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    MUTUAL_FUND = "mutual_fund"
    CRYPTOCURRENCY = "cryptocurrency"
    CURRENCY = "currency"
    FUTURE = "future"
    OPTION = "option"
    OTHER = "other"


class Exchange(StrEnum):
    """Listing venue as an ISO 10383 market identifier code; OTHER for any venue v1 rejects."""

    NYSE = "XNYS"
    NASDAQ = "XNAS"
    NYSE_ARCA = "ARCX"
    NYSE_AMERICAN = "XASE"
    CBOE_BZX = "BATS"
    OTHER = "OTHER"  # OTC markets, non-US exchanges and unknown provider codes


SUPPORTED_ASSET_TYPES: Final = frozenset({AssetType.EQUITY, AssetType.ETF})
SUPPORTED_CURRENCY: Final = "USD"


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerInfo:
    """Validated instrument metadata. ``symbol`` is normalized, ``name`` stripped."""

    symbol: str
    name: str
    asset_type: AssetType
    exchange: Exchange
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_ticker(self.symbol))
        object.__setattr__(self, "name", _name(self.name))
        if not isinstance(self.asset_type, AssetType):
            raise TypeError(
                f"asset_type must be an AssetType, got {type(self.asset_type).__name__}"
            )
        if not isinstance(self.exchange, Exchange):
            raise TypeError(f"exchange must be an Exchange, got {type(self.exchange).__name__}")
        object.__setattr__(self, "currency", _currency(self.currency))


def parse_ticker(text: str) -> str:
    """``normalize_ticker(text)``; malformed text raises ``InvalidTickerError(MALFORMED)``.

    The error is raised ``from None`` and its message is ``normalize_ticker``'s, which echoes the
    text only as a bounded ``repr()``. A non-``str`` raises ``TypeError``.
    """
    if not isinstance(text, str):
        raise TypeError(f"ticker must be a str, got {type(text).__name__}")
    try:
        return normalize_ticker(text)
    except ValueError as error:
        raise InvalidTickerError(InvalidTickerReason.MALFORMED, str(error)) from None


def ensure_supported(info: TickerInfo) -> TickerInfo:
    """Return ``info`` itself if v1 supports the instrument (decision D27).

    Otherwise raise ``InvalidTickerError`` for the first failing check: the asset type
    (``unsupported_asset_type``), then the exchange (``unsupported_exchange``), then the currency
    (``unsupported_currency``).
    """
    if not isinstance(info, TickerInfo):
        raise TypeError(f"info must be a TickerInfo, got {type(info).__name__}")
    if info.asset_type not in SUPPORTED_ASSET_TYPES:
        supported = " and ".join(sorted(member.value for member in SUPPORTED_ASSET_TYPES))
        raise InvalidTickerError(
            InvalidTickerReason.UNSUPPORTED_ASSET_TYPE,
            f"asset type {info.asset_type.value} is not supported; "
            f"supported asset types are {supported}",
            ticker=info.symbol,
        )
    if info.exchange is Exchange.OTHER:
        codes = [member.value for member in Exchange if member is not Exchange.OTHER]
        raise InvalidTickerError(
            InvalidTickerReason.UNSUPPORTED_EXCHANGE,
            f"exchange {info.exchange.value} is not supported; "
            f"supported exchanges are {', '.join(codes[:-1])} and {codes[-1]}",
            ticker=info.symbol,
        )
    if info.currency != SUPPORTED_CURRENCY:
        raise InvalidTickerError(
            InvalidTickerReason.UNSUPPORTED_CURRENCY,
            f"currency {info.currency} is not supported; "
            f"the supported currency is {SUPPORTED_CURRENCY}",
            ticker=info.symbol,
        )
    return info


def _name(value: object) -> str:
    """A stripped name of 1-120 printable characters. The provider text is never echoed."""
    if not isinstance(value, str):
        raise TypeError(f"name must be a str, got {type(value).__name__}")
    stripped = value.strip()
    if not 0 < len(stripped) <= _MAX_NAME_LENGTH or not stripped.isprintable():
        raise ValueError(
            f"name must be 1-{_MAX_NAME_LENGTH} printable characters without line breaks, tabs "
            f"or control characters once stripped, got {len(stripped)} characters"
        )
    return str.__str__(stripped)


def _currency(value: object) -> str:
    """A currency code of 1-8 ASCII letters, case preserved (``GBp`` is not ``GBP``)."""
    if not isinstance(value, str):
        raise TypeError(f"currency must be a str, got {type(value).__name__}")
    if not (0 < len(value) <= _MAX_CURRENCY_LENGTH and value.isascii() and value.isalpha()):
        raise ValueError(
            f"currency must be 1-{_MAX_CURRENCY_LENGTH} ASCII letters, got {len(value)} characters"
        )
    return str.__str__(value)

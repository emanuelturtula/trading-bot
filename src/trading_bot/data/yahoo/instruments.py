"""Yahoo symbols and instrument metadata (spec 011, Design 7; decisions D27, D51, D52).

``check_yahoo_symbol`` rejects, without any I/O, text that yfinance would interpolate unescaped
into a request path, and ISIN-shaped text, which ``yfinance.Ticker`` would resolve with a search
request inside its constructor. ``ticker_info`` maps the chart metadata of a history call to a
``TickerInfo``: exact, case-sensitive Yahoo codes through ``ASSET_TYPES`` and ``EXCHANGES``, with
OTC markets and unknown codes as ``OTHER``. The v1 policy (``ensure_supported``) is applied by the
provider. Messages never echo provider text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason, ProviderDataError
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.data.yahoo.history import ISIN_PATTERN, YAHOO_SYMBOL_PATTERN, ChartMetadata
from trading_bot.domain.signals import normalize_ticker

__all__ = ["ASSET_TYPES", "EXCHANGES", "check_yahoo_symbol", "ticker_info"]

ASSET_TYPES: Final[Mapping[str, AssetType]] = MappingProxyType(
    {
        "EQUITY": AssetType.EQUITY,
        "ETF": AssetType.ETF,
        "INDEX": AssetType.INDEX,
        "MUTUALFUND": AssetType.MUTUAL_FUND,
        "CRYPTOCURRENCY": AssetType.CRYPTOCURRENCY,
        "CURRENCY": AssetType.CURRENCY,
        "FUTURE": AssetType.FUTURE,
        "OPTION": AssetType.OPTION,
    }
)
EXCHANGES: Final[Mapping[str, Exchange]] = MappingProxyType(
    {
        "NYQ": Exchange.NYSE,
        "NMS": Exchange.NASDAQ,
        "NGM": Exchange.NASDAQ,
        "NCM": Exchange.NASDAQ,
        "PCX": Exchange.NYSE_ARCA,
        "ASE": Exchange.NYSE_AMERICAN,
        "BTS": Exchange.CBOE_BZX,
    }
)

_CURRENCY_PATTERN: Final = re.compile(r"[A-Za-z]{1,8}")


def check_yahoo_symbol(symbol: str) -> str:
    """Return ``symbol`` (the output of ``parse_ticker``) unchanged if Yahoo can be asked for it.

    Raises ``TypeError`` for a non-``str``, ``InvalidTickerError(malformed)`` when it does not
    fully match ``YAHOO_SYMBOL_PATTERN`` and ``InvalidTickerError(not_found)`` when it is shaped
    like an ISIN. The error's ``ticker`` is the symbol when it is already normalized, otherwise
    ``None``; the text itself is never echoed.
    """
    if not isinstance(symbol, str):
        raise TypeError(f"symbol must be a str, got {type(symbol).__name__}")
    if YAHOO_SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise InvalidTickerError(
            InvalidTickerReason.MALFORMED,
            "a Yahoo symbol is 1-24 characters among A-Z, 0-9, '.', '^', '=' and '-', "
            "starting with a letter, a digit or '^'",
            ticker=_normalized_or_none(symbol),
        )
    if ISIN_PATTERN.fullmatch(symbol) is not None:
        raise InvalidTickerError(
            InvalidTickerReason.NOT_FOUND,
            "ISIN codes are not supported; use the ticker symbol",
            ticker=_normalized_or_none(symbol),
        )
    return symbol


def ticker_info(symbol: str, metadata: ChartMetadata) -> TickerInfo:
    """The ``TickerInfo`` described by ``metadata`` for the requested ``symbol``.

    Checks, in order, raising ``ProviderDataError`` with ``ticker=symbol``: the metadata symbol
    is present and normalizes (``invalid_metadata``) to ``symbol`` (``symbol_mismatch``); the
    currency is 1-8 ASCII letters (``invalid_metadata``). The asset type and the exchange come
    from ``ASSET_TYPES`` and ``EXCHANGES`` (``OTHER`` otherwise), and the name is the first of
    ``long_name`` and ``short_name`` that ``TickerInfo`` accepts, otherwise ``symbol``. No
    ``ValueError`` escapes.
    """
    if not isinstance(symbol, str):
        raise TypeError(f"symbol must be a str, got {type(symbol).__name__}")
    if not isinstance(metadata, ChartMetadata):
        raise TypeError(f"metadata must be a ChartMetadata, got {type(metadata).__name__}")
    if metadata.symbol is None:
        raise _metadata_error("invalid_metadata", "the provider metadata has no symbol", symbol)
    try:
        reported = normalize_ticker(metadata.symbol)
    except ValueError:
        raise _metadata_error(
            "invalid_metadata", "the provider metadata symbol is not a valid ticker", symbol
        ) from None
    if reported != symbol:
        raise _metadata_error(
            "symbol_mismatch",
            "the provider metadata describes another symbol than the requested one",
            symbol,
        )
    currency = metadata.currency
    if currency is None or _CURRENCY_PATTERN.fullmatch(currency) is None:
        raise _metadata_error(
            "invalid_metadata", "the provider metadata has no valid currency", symbol
        )
    asset_type = AssetType.OTHER
    if metadata.instrument_type is not None:
        asset_type = ASSET_TYPES.get(metadata.instrument_type, AssetType.OTHER)
    exchange = Exchange.OTHER
    if metadata.exchange_name is not None:
        exchange = EXCHANGES.get(metadata.exchange_name, Exchange.OTHER)
    for name in (metadata.long_name, metadata.short_name):
        if name is None:
            continue
        try:
            return TickerInfo(
                symbol=symbol,
                name=name,
                asset_type=asset_type,
                exchange=exchange,
                currency=currency,
            )
        except ValueError:
            continue
    return TickerInfo(
        symbol=symbol, name=symbol, asset_type=asset_type, exchange=exchange, currency=currency
    )


def _metadata_error(kind: str, message: str, symbol: str) -> ProviderDataError:
    return ProviderDataError(kind, message, ticker=symbol)


def _normalized_or_none(text: str) -> str | None:
    """``text`` when ``normalize_ticker`` leaves it unchanged, otherwise ``None``."""
    try:
        normalized = normalize_ticker(text)
    except ValueError:
        return None
    return normalized if normalized == text else None

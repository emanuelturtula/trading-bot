"""The ``YahooClient`` backed by yfinance (spec 011, Design 8; decisions D48, D50 and D53).

This is the only module of ``src/`` that imports yfinance. ``configure_yfinance`` owns every
change to yfinance's process-wide state: a private cache directory for its SQLite caches (one of
them a pickled cookie jar), exceptions instead of silently empty frames, no internal retries (the
transport retries without blocking a worker thread on sleeps) and a ``yfinance`` logger at
``WARNING``, which keeps the session crumb and request URLs out of ``DEBUG`` logs.

``YFinanceClient.history`` makes one ``Ticker.history`` call without ``end`` (so a retry never
reads yfinance's response cache) and reads the chart metadata of that call. ``map_yahoo_error``
turns every library exception into a typed ``MarketDataError`` with one ordered table, raised
``from None``: messages are built only from the symbol, enum values and fixed texts, so no URL,
query string, header or body from the original exception reaches ``str()``, ``repr()`` or a log.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Final

import pandas as pd
import yfinance
from yfinance.exceptions import (
    YFDataException,
    YFPricesMissingError,
    YFRateLimitError,
    YFTzMissingError,
)

from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    MarketDataError,
    NoDataError,
    ProviderDataError,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.yahoo.history import ChartMetadata, HistoryQuery, YahooHistory

__all__ = ["YAHOO_REQUEST_TIMEOUT", "YFinanceClient", "configure_yfinance", "map_yahoo_error"]

YAHOO_REQUEST_TIMEOUT: Final = 15.0  # seconds, passed to Ticker.history(timeout=...)

_LOGGER: Final = logging.getLogger("trading_bot.data.yahoo.client")
_YFINANCE_LOGGER: Final = "yfinance"
_CACHE_DIR_MODE: Final = 0o700
_DELISTED_REASON: Final = "symbol may be delisted"
_RETRY_AFTER_DIGITS: Final = re.compile(r"[0-9]+")
_MAX_RETRY_AFTER_SECONDS: Final = 3600
_TIMEOUT_CLASS_NAME: Final = "Timeout"
_HTTP_NOT_FOUND: Final = 404
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERRORS: Final = range(500, 600)


def configure_yfinance(cache_dir: Path) -> None:
    """Point yfinance's caches to ``cache_dir`` and set its process-wide options (Design 8.1).

    ``cache_dir`` must be an absolute ``pathlib.Path`` (``TypeError``/``ValueError``); it is
    created with mode ``0o700`` and an ``OSError`` propagates. Then the time-zone, cookie and ISIN
    caches move there, exceptions are raised instead of hidden, yfinance's own logging and retries
    are off, and the ``yfinance`` logger is set to ``WARNING``. No SQLite file is created, no proxy
    is set and no environment variable is read. Calling it again with the same directory gives
    the same state.
    """
    if not isinstance(cache_dir, Path):
        raise TypeError(f"cache_dir must be a pathlib.Path, got {type(cache_dir).__name__}")
    if not cache_dir.is_absolute():
        raise ValueError("cache_dir must be an absolute path")
    cache_dir.mkdir(mode=_CACHE_DIR_MODE, parents=True, exist_ok=True)
    yfinance.set_tz_cache_location(str(cache_dir))
    yfinance.config.debug.hide_exceptions = False
    yfinance.config.debug.logging = False
    yfinance.config.network.retries = 0
    logging.getLogger(_YFINANCE_LOGGER).setLevel(logging.WARNING)


class YFinanceClient:
    """The YahooClient backed by yfinance. The only class in src/ that calls yfinance."""

    def __init__(self, *, cache_dir: Path, request_timeout: float = YAHOO_REQUEST_TIMEOUT) -> None:
        if isinstance(request_timeout, bool) or not isinstance(request_timeout, int | float):
            raise TypeError(
                f"request_timeout must be an int or float, got {type(request_timeout).__name__}"
            )
        if not (math.isfinite(request_timeout) and request_timeout > 0):
            raise ValueError("request_timeout must be finite and greater than zero")
        configure_yfinance(cache_dir)
        self._request_timeout = request_timeout

    def history(self, query: HistoryQuery) -> YahooHistory:
        """One ``Ticker.history`` call and its chart metadata (Design 8.2). Blocking.

        A start query passes ``start`` and a period query ``period="1mo"``; neither passes
        ``end``. Every ``Exception`` is mapped with ``map_yahoo_error`` and raised ``from None``;
        other ``BaseException``s propagate. A frame that is not a ``DataFrame`` or metadata that
        is not a ``Mapping`` raises ``ProviderDataError("invalid_response")``. The frame is
        returned unmodified.
        """
        if not isinstance(query, HistoryQuery):
            raise TypeError(f"query must be a HistoryQuery, got {type(query).__name__}")
        window = {"start": query.start} if query.period is None else {"period": query.period}
        metadata: ChartMetadata | None = None
        try:
            ticker = yfinance.Ticker(query.symbol)
            frame: object = ticker.history(
                **window,  # start=... or period="1mo"; never end (D48)
                interval=query.interval,
                prepost=False,
                actions=True,
                auto_adjust=False,
                back_adjust=False,
                repair=False,
                keepna=False,
                rounding=False,
                timeout=self._request_timeout,
            )
            raw_metadata: object = ticker.get_history_metadata()
            if isinstance(raw_metadata, Mapping):
                metadata = ChartMetadata.from_mapping(raw_metadata)
        except Exception as error:
            raise map_yahoo_error(error, symbol=query.symbol) from None
        if not isinstance(frame, pd.DataFrame) or metadata is None:
            raise ProviderDataError(
                "invalid_response",
                "yfinance returned a response of an unexpected type",
                ticker=query.symbol,
            )
        return YahooHistory(frame=frame, metadata=metadata)


def map_yahoo_error(error: Exception, *, symbol: str) -> MarketDataError:
    """The typed error for an exception raised by yfinance (Design 8.3; the first row matches).

    ``MarketDataError`` is returned as is; yfinance's rate limit is ``rate_limited``; a missing
    time zone or Yahoo's "symbol may be delisted" is ``not_found``; any other missing prices are
    ``NoDataError``; ``YFDataException`` and malformed JSON are ``invalid_response``; timeouts are
    ``timeout``; HTTP statuses map 404 to ``not_found``, 429 to ``rate_limited`` (with a numeric
    ``Retry-After`` capped at one hour), 5xx to ``server_error`` and the rest to
    ``invalid_response``; other ``OSError``s are ``connection``. Anything else is
    ``ProviderDataError("unexpected_provider_error")`` with one ``ERROR`` record naming only the
    exception class and the symbol. Only this last row logs.
    """
    if not isinstance(error, Exception):
        raise TypeError(f"error must be an Exception, got {type(error).__name__}")
    if not isinstance(symbol, str):
        raise TypeError(f"symbol must be a str, got {type(symbol).__name__}")
    if isinstance(error, MarketDataError):
        return error
    if isinstance(error, YFRateLimitError):
        return _unavailable(ProviderFailure.RATE_LIMITED, symbol)
    if isinstance(error, YFTzMissingError):
        return _not_found(symbol)
    if isinstance(error, YFPricesMissingError):
        reason: object = error.yahoo_reason
        if isinstance(reason, str) and _DELISTED_REASON in reason.lower():
            return _not_found(symbol)
        return NoDataError("Yahoo returned no price data for the requested range", ticker=symbol)
    if isinstance(error, YFDataException | json.JSONDecodeError):
        return _unavailable(ProviderFailure.INVALID_RESPONSE, symbol)
    if isinstance(error, TimeoutError) or any(
        klass.__name__ == _TIMEOUT_CLASS_NAME for klass in type(error).__mro__
    ):
        return _unavailable(ProviderFailure.TIMEOUT, symbol)
    status, headers = _http_status(error)
    if status is not None:
        if status == _HTTP_NOT_FOUND:
            return _not_found(symbol)
        if status == _HTTP_TOO_MANY_REQUESTS:
            return _unavailable(
                ProviderFailure.RATE_LIMITED, symbol, retry_after=_retry_after(headers)
            )
        if status in _HTTP_SERVER_ERRORS:
            return _unavailable(ProviderFailure.SERVER_ERROR, symbol)
        return _unavailable(ProviderFailure.INVALID_RESPONSE, symbol)
    if isinstance(error, OSError):
        return _unavailable(ProviderFailure.CONNECTION, symbol)
    error_type = type(error)
    _LOGGER.error(
        "unexpected %s from yfinance for %s",
        f"{error_type.__module__}.{error_type.__qualname__}",
        symbol,
    )
    return ProviderDataError(
        "unexpected_provider_error", "yfinance raised an unexpected error", ticker=symbol
    )


def _unavailable(
    failure: ProviderFailure, symbol: str, *, retry_after: timedelta | None = None
) -> ProviderUnavailableError:
    return ProviderUnavailableError(failure, ticker=symbol, retry_after=retry_after)


def _not_found(symbol: str) -> InvalidTickerError:
    return InvalidTickerError(
        InvalidTickerReason.NOT_FOUND, "Yahoo does not know this symbol", ticker=symbol
    )


def _http_status(error: Exception) -> tuple[int | None, object]:
    """``(response.status_code, response.headers)`` when the status is an ``int``, not a ``bool``.

    Both HTTP backends attach the response to their HTTP errors. Attributes that cannot be read
    count as absent.
    """
    try:
        response: object = getattr(error, "response", None)
        status: object = getattr(response, "status_code", None)
        headers: object = getattr(response, "headers", None)
    except Exception:
        return None, None
    if isinstance(status, bool) or not isinstance(status, int):
        return None, None
    return int(status), headers


def _retry_after(headers: object) -> timedelta | None:
    """A ``Retry-After`` of ASCII digits only (seconds), capped at one hour; otherwise ``None``."""
    if not isinstance(headers, Mapping):
        return None
    try:
        value: object = headers.get("Retry-After")
    except Exception:
        return None
    if not isinstance(value, str) or _RETRY_AFTER_DIGITS.fullmatch(value) is None:
        return None
    digits = value.lstrip("0")
    # More than four digits is always above the cap; int() of a huge string is never attempted.
    if len(digits) > len(str(_MAX_RETRY_AFTER_SECONDS)):
        return timedelta(seconds=_MAX_RETRY_AFTER_SECONDS)
    return timedelta(seconds=min(int(digits or "0"), _MAX_RETRY_AFTER_SECONDS))

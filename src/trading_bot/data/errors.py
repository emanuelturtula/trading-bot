"""Typed market data errors (spec 010, Design 5; decision D38).

Callers branch on the class and on the class-level ``retryable`` flag: providers retry only
``ProviderUnavailableError`` internally, the scheduler retries ``CandleNotPublishedError``
within a bounded window, and every other error is final for the run. No error is a
``ValueError``, so an ``except ValueError`` meant for programming errors never swallows a
provider failure.

Messages are single-line and built only from normalized tickers, timeframe codes, enum values
and ISO timestamps: ``str(error)`` is ``"[<code>: ]<message>[ [field=value, ...]]"``. Errors
raised while handling a third-party exception must use ``raise ... from None``, so URLs, query
strings, headers or response bodies in library exceptions never reach a log. Constructor
arguments are validated; a bad value is a programming error (``TypeError``/``ValueError``).

This module imports only the standard library and the lightweight domain modules: no pandas.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Final

from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = [
    "CandleNotPublishedError",
    "InvalidTickerError",
    "InvalidTickerReason",
    "MarketDataError",
    "NoDataError",
    "ProviderDataError",
    "ProviderFailure",
    "ProviderUnavailableError",
    "UnpublishedReason",
]

_MAX_MESSAGE_LENGTH: Final = 300
_KIND_PATTERN: Final = re.compile(r"[a-z_]{1,32}")

type _Fields = tuple[tuple[str, str], ...]


class MarketDataError(Exception):
    """Base class of market data errors. ``retryable`` tells whether a later attempt may pass."""

    retryable: ClassVar[bool] = False
    ticker: str | None  # normalized with normalize_ticker
    timeframe: Timeframe | None

    def __init__(
        self, message: str, *, ticker: str | None = None, timeframe: Timeframe | None = None
    ) -> None:
        _check_message(message)
        self.ticker = None if ticker is None else normalize_ticker(ticker)
        if timeframe is not None and not isinstance(timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe or None, got {type(timeframe).__name__}"
            )
        self.timeframe = timeframe
        fields: _Fields = (
            *((("ticker", self.ticker),) if self.ticker is not None else ()),
            *((("timeframe", self.timeframe.value),) if self.timeframe is not None else ()),
            *self._details(),
        )
        located = f" [{', '.join(f'{name}={value}' for name, value in fields)}]" if fields else ""
        super().__init__(f"{self._prefix()}{message}{located}")

    def _prefix(self) -> str:
        """The code that starts the message (``"<code>: "``), if the class has one."""
        return ""

    def _details(self) -> _Fields:
        """Extra ``(name, value)`` fields shown after ticker and timeframe."""
        return ()


class InvalidTickerReason(StrEnum):
    MALFORMED = "malformed"
    NOT_FOUND = "not_found"
    UNSUPPORTED_ASSET_TYPE = "unsupported_asset_type"
    UNSUPPORTED_EXCHANGE = "unsupported_exchange"
    UNSUPPORTED_CURRENCY = "unsupported_currency"


class InvalidTickerError(MarketDataError):
    """The ticker text is malformed, unknown to the provider, or an unsupported instrument."""

    reason: InvalidTickerReason

    def __init__(
        self, reason: InvalidTickerReason, message: str, *, ticker: str | None = None
    ) -> None:
        if not isinstance(reason, InvalidTickerReason):
            raise TypeError(f"reason must be an InvalidTickerReason, got {type(reason).__name__}")
        self.reason = reason
        super().__init__(message, ticker=ticker)

    def _prefix(self) -> str:
        return f"{self.reason.value}: "


class NoDataError(MarketDataError):
    """No candle closed by ``now`` survives, and no dropped row has the expected label."""


class ProviderDataError(MarketDataError):
    """The provider response cannot be normalized, or its metadata is unusable."""

    kind: str  # a NormalizationErrorKind value, or a provider code; [a-z_]{1,32}

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        ticker: str | None = None,
        timeframe: Timeframe | None = None,
    ) -> None:
        if not isinstance(kind, str):
            raise TypeError(f"kind must be a str, got {type(kind).__name__}")
        if _KIND_PATTERN.fullmatch(kind) is None:
            raise ValueError("kind must be 1-32 characters among lowercase ASCII letters and '_'")
        self.kind = str.__str__(kind)  # a plain str, also for str subclasses
        super().__init__(message, ticker=ticker, timeframe=timeframe)

    def _prefix(self) -> str:
        return f"{self.kind}: "


class UnpublishedReason(StrEnum):
    MISSING = "missing"  # the provider returned no row for the candle
    INVALID = "invalid"  # the row was dropped by normalization


class CandleNotPublishedError(MarketDataError):
    """The last slot closed at ``now`` has no valid row yet (decision D43). Retryable."""

    retryable: ClassVar[bool] = True
    reason: UnpublishedReason
    expected_label: datetime  # stdlib UTC: the label of the last slot closed at now
    last_label: datetime | None  # stdlib UTC: the last closed candle available, if any

    def __init__(
        self,
        reason: UnpublishedReason,
        *,
        ticker: str,
        timeframe: Timeframe,
        expected_label: datetime,
        last_label: datetime | None,
    ) -> None:
        if not isinstance(reason, UnpublishedReason):
            raise TypeError(f"reason must be an UnpublishedReason, got {type(reason).__name__}")
        if not isinstance(ticker, str):
            raise TypeError(f"ticker must be a str, got {type(ticker).__name__}")
        if not isinstance(timeframe, Timeframe):
            raise TypeError(f"timeframe must be a Timeframe, got {type(timeframe).__name__}")
        self.reason = reason
        self.expected_label = to_utc(expected_label)
        self.last_label = None if last_label is None else to_utc(last_label)
        if reason is UnpublishedReason.MISSING:
            message = "the provider has no row for the last closed candle"
        else:
            message = "the provider row for the last closed candle is invalid"
        super().__init__(message, ticker=ticker, timeframe=timeframe)

    def _prefix(self) -> str:
        return f"{self.reason.value}: "

    def _details(self) -> _Fields:
        last = "none" if self.last_label is None else self.last_label.isoformat()
        return (("expected_label", self.expected_label.isoformat()), ("last_label", last))


class ProviderFailure(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"


class ProviderUnavailableError(MarketDataError):
    """A transport failure, timeout, rate limit or server error. Retryable."""

    retryable: ClassVar[bool] = True
    failure: ProviderFailure
    retry_after: timedelta | None  # >= 0 when set

    def __init__(
        self,
        failure: ProviderFailure,
        *,
        ticker: str | None = None,
        timeframe: Timeframe | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        if not isinstance(failure, ProviderFailure):
            raise TypeError(f"failure must be a ProviderFailure, got {type(failure).__name__}")
        if retry_after is not None:
            if not isinstance(retry_after, timedelta):
                raise TypeError(
                    f"retry_after must be a timedelta or None, got {type(retry_after).__name__}"
                )
            if retry_after < timedelta(0):
                raise ValueError("retry_after must not be negative")
        self.failure = failure
        self.retry_after = retry_after
        super().__init__(
            "the market data provider is unavailable", ticker=ticker, timeframe=timeframe
        )

    def _prefix(self) -> str:
        return f"{self.failure.value}: "

    def _details(self) -> _Fields:
        if self.retry_after is None:
            return ()
        return (("retry_after", f"{self.retry_after.total_seconds()}s"),)


def _check_message(message: object) -> None:
    """A message is one line of at most 300 characters. The rejected text is never echoed."""
    if not isinstance(message, str):
        raise TypeError(f"message must be a str, got {type(message).__name__}")
    if "\n" in message or "\r" in message:
        raise ValueError("message must be a single line without line breaks")
    if len(message) > _MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"message must be at most {_MAX_MESSAGE_LENGTH} characters, got {len(message)}"
        )

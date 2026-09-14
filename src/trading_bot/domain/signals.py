"""Signals and their idempotency key (spec 004, Design 6; CLAUDE.md rule 5).

A signal is identified by ``(ticker, timeframe, rule_id, candle_close_ts)``. Every field of the
key is validated and normalized on construction (upper-cased ticker, UTC instant), so equal
inputs always give equal keys and the same ``str(key)`` across processes. ``hash(key)`` is
process-specific: persist the key fields or ``str(key)``, never ``hash(key)``.

This module imports neither pandas nor numpy.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from numbers import Real
from typing import Final

from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["IndicatorValues", "Side", "Signal", "SignalKey", "normalize_ticker"]

_MAX_TICKER_LENGTH: Final = 32
_MAX_RULE_ID_LENGTH: Final = 64
_ECHO_LIMIT: Final = 32  # characters of rejected input echoed in error messages
_ECHO_WIDTH: Final = 64  # columns of that echo once rendered with repr()
_CHARSET_HINT: Final = "printable ASCII characters without whitespace or '|'"


class Side(StrEnum):
    """Direction of a signal. The bot only notifies: it never places orders."""

    BUY = "BUY"
    SELL = "SELL"


def normalize_ticker(value: str) -> str:
    """Strip surrounding whitespace and upper-case a symbol of 1-32 printable ASCII characters.

    Whitespace inside the symbol, control characters, ``|`` and non-ASCII characters raise
    ``ValueError``; a non-``str`` raises ``TypeError``. Whether the symbol exists is not checked.
    """
    if not isinstance(value, str):
        raise TypeError(f"ticker must be a str, got {type(value).__name__}")
    stripped = value.strip()
    # Validate before upper-casing: some non-ASCII letters upper-case to ASCII letters
    # (U+017F LATIN SMALL LETTER LONG S becomes "S").
    if not _is_token(stripped, _MAX_TICKER_LENGTH):
        raise ValueError(
            f"invalid ticker {_echo(value)}: expected 1-{_MAX_TICKER_LENGTH} {_CHARSET_HINT}"
        )
    return stripped.upper()


class IndicatorValues(Mapping[str, float]):
    """Read-only, hashable, picklable mapping of indicator names to finite floats.

    Insertion order is kept for display; equality and hashing ignore it.
    """

    __slots__ = ("_items",)

    _items: dict[str, float]

    def __init__(self, values: Mapping[str, float] | None = None) -> None:
        items: dict[str, float] = {}
        if values is not None:
            if not isinstance(values, Mapping):
                raise TypeError(f"indicator values must be a mapping, got {type(values).__name__}")
            for name, value in values.items():
                key = _indicator_name(name)
                items[key] = _finite_real(value, f"indicator value {_echo(key)}")
        object.__setattr__(self, "_items", items)

    def __getitem__(self, key: str) -> float:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        # Consistent with Mapping equality, which ignores insertion order.
        return hash(frozenset(self._items.items()))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __reduce__(self) -> tuple[type[IndicatorValues], tuple[dict[str, float]]]:
        # Rebuilt through __init__, so restored copies are validated and read-only too.
        return (type(self), (dict(self._items),))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._items!r})"


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalKey:
    """The idempotency key of a signal. ``str(key)`` is canonical and stable across processes."""

    ticker: str
    timeframe: Timeframe
    rule_id: str
    candle_close_ts: datetime

    def __post_init__(self) -> None:
        _normalize_identity(self)

    def __str__(self) -> str:
        return f"{self.ticker}|{self.timeframe}|{self.rule_id}|{self.candle_close_ts.isoformat()}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal:
    """A rule that fired on a closed candle. It compares by value; identity is its key."""

    ticker: str
    timeframe: Timeframe
    rule_id: str
    side: Side
    candle_close_ts: datetime
    close_price: float
    indicator_values: Mapping[str, float] = field(default_factory=IndicatorValues)

    def __post_init__(self) -> None:
        _normalize_identity(self)
        if not isinstance(self.side, Side):
            raise TypeError(f"side must be a Side, got {type(self.side).__name__}")
        close_price = _finite_real(self.close_price, "close_price")
        if close_price <= 0.0:
            raise ValueError(f"close_price must be greater than zero, got {close_price!r}")
        object.__setattr__(self, "close_price", close_price)
        if not isinstance(self.indicator_values, Mapping):
            raise TypeError(
                f"indicator values must be a mapping, got {type(self.indicator_values).__name__}"
            )
        object.__setattr__(self, "indicator_values", IndicatorValues(self.indicator_values))

    @property
    def idempotency_key(self) -> SignalKey:
        """The ``(ticker, timeframe, rule_id, candle_close_ts)`` key that deduplicates signals."""
        return SignalKey(
            ticker=self.ticker,
            timeframe=self.timeframe,
            rule_id=self.rule_id,
            candle_close_ts=self.candle_close_ts,
        )


def _normalize_identity(instance: Signal | SignalKey) -> None:
    """Validate and normalize the key fields in place. ``Signal`` and ``SignalKey`` share it."""
    object.__setattr__(instance, "ticker", normalize_ticker(instance.ticker))
    if not isinstance(instance.timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(instance.timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )
    object.__setattr__(instance, "rule_id", _rule_id(instance.rule_id))
    object.__setattr__(instance, "candle_close_ts", to_utc(instance.candle_close_ts))


def _rule_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"rule_id must be a str, got {type(value).__name__}")
    if not _is_token(value, _MAX_RULE_ID_LENGTH):
        raise ValueError(
            f"invalid rule_id {_echo(value)}: expected 1-{_MAX_RULE_ID_LENGTH} {_CHARSET_HINT}"
        )
    return str.__str__(value)  # a plain str, also for str subclasses


def _indicator_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"indicator names must be str, got {type(name).__name__}")
    if not name:
        raise ValueError("indicator names must not be empty")
    return str.__str__(name)


def _finite_real(value: object, label: str) -> float:
    """``value`` as a built-in ``float`` if it is a finite real number that is not a ``bool``."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{label} must be a real number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{label} must be finite, got a number too large for a float") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite, got {number!r}")
    return number


def _is_token(text: str, max_length: int) -> bool:
    """1 to ``max_length`` printable ASCII characters, none of them whitespace or ``|``."""
    return 0 < len(text) <= max_length and all("!" <= char <= "~" and char != "|" for char in text)


def _echo(text: str) -> str:
    """``repr()`` of the longest prefix of ``text`` (at most 32 characters) that fits 64 columns.

    Escapes can make ``repr()`` up to 10 times longer than its input, so the prefix shrinks
    until the rendering is short enough: messages that echo user input stay single-line and
    bounded.
    """
    prefix = text[:_ECHO_LIMIT]
    while len(rendered := repr(prefix)) > _ECHO_WIDTH:
        prefix = prefix[:-1]
    return rendered

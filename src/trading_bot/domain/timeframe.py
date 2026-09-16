"""Candle timeframes: canonical codes, durations and the nominal candle close (spec 004)."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from trading_bot.domain.utc import to_utc

__all__ = ["Timeframe", "UnknownTimeframeError"]

_ECHO_LIMIT: Final = 32  # characters of rejected input echoed in error messages
_ECHO_WIDTH: Final = 64  # columns of that echo once rendered with repr()


class UnknownTimeframeError(ValueError):
    """Raised by ``Timeframe.parse`` for text that is not an exact timeframe code."""


class Timeframe(StrEnum):
    """Supported candle timeframes. The value is the canonical code used everywhere."""

    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @classmethod
    def parse(cls, value: str) -> Timeframe:
        """Parse user text: surrounding whitespace is ignored, case and spelling must be exact.

        There are no aliases (``"1H"``, ``"60m"`` and ``"daily"`` are rejected), so Telegram,
        the CLI, the API and rule JSON share one spelling.
        """
        if not isinstance(value, str):
            raise TypeError(f"timeframe must be a str, got {type(value).__name__}")
        try:
            return cls(value.strip())
        except ValueError:
            expected = ", ".join(member.value for member in cls)
            raise UnknownTimeframeError(
                f"unknown timeframe {_echo(value)}; expected one of {expected}"
            ) from None

    @property
    def duration(self) -> timedelta:
        """The fixed length of one candle."""
        match self:
            case Timeframe.H1:
                return timedelta(hours=1)
            case Timeframe.H4:
                return timedelta(hours=4)
            # mypy proves this match exhaustive ("Missing return statement" otherwise); coverage
            # cannot see that, so the impossible fall-through arc is excluded.
            case Timeframe.D1:  # pragma: no branch
                return timedelta(days=1)

    def nominal_close(self, open_time: datetime) -> datetime:
        """``to_utc(open_time) + duration``: a calendar-free candle close used as an identifier.

        It is not the market close (spec 004, Design 5): never use it to decide whether a candle
        is closed or to schedule a run.
        """
        return to_utc(open_time) + self.duration


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

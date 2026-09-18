"""Errors of the persistence layer (spec 013, Design 6.2).

``PersistenceError`` is **not** a ``ValueError``, for the same reason as ``MarketDataError``
(spec 010): an ``except ValueError`` meant for programming errors must not swallow a storage
failure. Argument errors keep coming from the domain (``normalize_ticker``, ``Timeframe.parse``)
as ``ValueError`` or ``TypeError``.

Every message is one English line built from ids, normalized symbols, timeframe codes and a
``repr()``-escaped, truncated rule name. A rule **document** never appears in a message:
``StoredRuleError`` carries the kinds and paths of spec 006, which are already bounded and safe
to log. ``SchemaMismatchError`` names the two revisions only, never a path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from trading_bot.domain.rules.errors import RuleProblem
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "AssignedTimeframeError",
    "DuplicateRuleNameError",
    "DuplicateTickerError",
    "PersistenceError",
    "SchemaMismatchError",
    "StoredRuleError",
    "TimeframeMismatchError",
    "UnknownRuleError",
    "UnknownTickerError",
]

_ECHO_LIMIT: Final = 40  # characters of a rule name echoed in a message
_ECHO_WIDTH: Final = 80  # columns of that echo once rendered with repr()
_MAX_PROBLEMS: Final = 5  # problems named in one StoredRuleError message


def echo_name(name: str) -> str:
    """A rule name as a bounded, ``repr()``-escaped echo, safe for logs and Telegram.

    A stored name is already bounded by spec 006, but an error can be built from text a caller
    typed, and a truncation is always visible so a message never looks like the whole name.
    """
    text = name[:_ECHO_LIMIT]
    if len(text) < len(name):
        text += "..."
    rendered = repr(text)
    if len(rendered) > _ECHO_WIDTH:  # escapes can multiply the length of every character
        rendered = rendered[: _ECHO_WIDTH - 4] + "...'"
    return rendered


class PersistenceError(Exception):
    """A storage failure. Never a ``ValueError``: it is not a programming error."""


class DuplicateTickerError(PersistenceError):
    """A ticker with that symbol and timeframe is already stored."""

    def __init__(self, symbol: str, timeframe: Timeframe) -> None:
        super().__init__(f"ticker {symbol} {timeframe.value} already exists")
        self.symbol = symbol
        self.timeframe = timeframe


class DuplicateRuleNameError(PersistenceError):
    """Rule names are globally unique (decision D85)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a rule named {echo_name(name)} already exists")
        self.name = name


class UnknownTickerError(PersistenceError):
    """No ticker carries that identifier."""

    def __init__(self, ticker_id: int) -> None:
        super().__init__(f"no ticker with id {ticker_id}")
        self.ticker_id = ticker_id


class UnknownRuleError(PersistenceError):
    """No rule carries that identifier."""

    def __init__(self, rule_id: int) -> None:
        super().__init__(f"no rule with id {rule_id}")
        self.rule_id = rule_id


class TimeframeMismatchError(PersistenceError):
    """A rule may only be assigned to a ticker of its own timeframe (spec 006, D11)."""

    def __init__(self, ticker_timeframe: Timeframe, rule_timeframe: Timeframe) -> None:
        super().__init__(
            f"the rule is evaluated on {rule_timeframe.value},"
            f" but the ticker is tracked on {ticker_timeframe.value}"
        )
        self.ticker_timeframe = ticker_timeframe
        self.rule_timeframe = rule_timeframe


class AssignedTimeframeError(PersistenceError):
    """An assigned rule cannot change timeframe: unassign it first (decision D97)."""

    def __init__(
        self, rule_id: int, assignment_count: int, stored: Timeframe, requested: Timeframe
    ) -> None:
        tickers = "ticker" if assignment_count == 1 else "tickers"
        super().__init__(
            f"rule {rule_id} is assigned to {assignment_count} {tickers},"
            f" so its timeframe cannot change from {stored.value} to {requested.value}"
        )
        self.rule_id = rule_id
        self.assignment_count = assignment_count
        self.stored = stored
        self.requested = requested


class StoredRuleError(PersistenceError):
    """A rule document is not valid; a stored one is never skipped silently (decision D93).

    ``rule_id`` is ``None`` on the write path, where the document failed its round trip and
    **nothing was stored**: the message must not claim a row that does not exist.
    """

    def __init__(self, rule_id: int | None, problems: Iterable[RuleProblem]) -> None:
        collected = tuple(problems)
        named = ", ".join(
            f"{problem.kind.value} at {problem.path or 'the document'}"
            for problem in collected[:_MAX_PROBLEMS]
        )
        remaining = len(collected) - _MAX_PROBLEMS
        if remaining > 0:
            named += f" and {remaining} more"
        subject = "the rule document" if rule_id is None else f"the document of rule {rule_id}"
        super().__init__(f"{subject} is not valid: {named}")
        self.rule_id = rule_id
        self.problems = collected


class SchemaMismatchError(PersistenceError):
    """The database schema is not at the revision this build expects (decision D95)."""

    def __init__(self, current: str | None, expected: str) -> None:
        super().__init__(
            f"the database schema is at revision {current or 'none'},"
            f" but revision {expected} is required"
        )
        self.current = current
        self.expected = expected

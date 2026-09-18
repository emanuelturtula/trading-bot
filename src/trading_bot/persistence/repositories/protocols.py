"""The three configuration ports (spec 013, Design 7, D69, D91).

Synchronous ``Protocol``s, mirroring ``MarketDataProvider``: the port is a ``Protocol`` and the
implementation is injected. Implementations take a ``Session`` rather than the ``Database``, so
"one ``Database.session()`` per unit of work" stays in the caller, where the transaction
boundary belongs, and one block can use all three repositories atomically.

A consumer running inside the event loop wraps its whole unit of work in ``asyncio.to_thread``
and never shares a ``Session`` across threads or across an ``await``.
"""

from __future__ import annotations

from typing import Protocol

from trading_bot.domain.rules.schema import Rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import Assignment, StoredRule, StoredTicker

__all__ = ["AssignmentRepository", "RuleRepository", "TickerRepository"]


class TickerRepository(Protocol):
    """The symbols the bot watches, keyed by ``(symbol, timeframe)``."""

    def add(self, symbol: str, timeframe: Timeframe, *, enabled: bool = True) -> StoredTicker:
        """Store a new ticker, or raise ``DuplicateTickerError`` when it already exists."""
        ...

    def get(self, ticker_id: int) -> StoredTicker | None: ...

    def get_by_symbol(self, symbol: str, timeframe: Timeframe) -> StoredTicker | None: ...

    def list_all(self) -> tuple[StoredTicker, ...]:
        """Every ticker, ordered by ``(symbol, timeframe)``."""
        ...

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredTicker, ...]: ...

    def set_enabled(self, ticker_id: int, enabled: bool) -> StoredTicker:
        """Switch the flag; writing nothing when it already has that value."""
        ...

    def delete(self, ticker_id: int) -> bool:
        """Delete the ticker and, through the database cascade, its assignments."""
        ...


class RuleRepository(Protocol):
    """The stored rules, addressed by their unique name (decision D85)."""

    def add(self, rule: Rule, *, enabled: bool = False) -> StoredRule:
        """Store a parsed rule, disabled by default (decision D98)."""
        ...

    def get(self, rule_id: int) -> StoredRule | None: ...

    def get_by_name(self, name: str) -> StoredRule | None: ...

    def list_all(self) -> tuple[StoredRule, ...]:
        """Every rule, ordered by name."""
        ...

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredRule, ...]: ...

    def replace(self, rule_id: int, rule: Rule, *, enabled: bool | None = None) -> StoredRule:
        """Replace the document, and the flag when one is given; a no-op writes nothing."""
        ...

    def set_enabled(self, rule_id: int, enabled: bool) -> StoredRule: ...

    def delete(self, rule_id: int) -> bool: ...


class AssignmentRepository(Protocol):
    """Which rule is evaluated on which ticker."""

    def assign(self, ticker_id: int, rule_id: int) -> Assignment:
        """Assign the rule to the ticker; assigning twice returns the existing row."""
        ...

    def unassign(self, ticker_id: int, rule_id: int) -> bool: ...

    def list_all(self) -> tuple[Assignment, ...]:
        """Every assignment, ordered by ``(ticker_id, rule_id)``."""
        ...

    def rules_for_ticker(
        self, ticker_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredRule, ...]:
        """The rules assigned to that ticker, ordered by name; the flag filters the rules."""
        ...

    def tickers_for_rule(
        self, rule_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredTicker, ...]:
        """The tickers the rule is assigned to, ordered by ``(symbol, timeframe)``."""
        ...

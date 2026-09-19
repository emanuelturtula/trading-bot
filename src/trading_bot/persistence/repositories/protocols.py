"""The five repository ports (spec 013, Design 7; spec 014, Design 7, 9; D69, D91).

Synchronous ``Protocol``s, mirroring ``MarketDataProvider``: the port is a ``Protocol`` and the
implementation is injected. Implementations take a ``Session`` rather than the ``Database``, so
"one ``Database.session()`` per unit of work" stays in the caller, where the transaction
boundary belongs, and one block can use all three repositories atomically.

A consumer running inside the event loop wraps its whole unit of work in ``asyncio.to_thread``
and never shares a ``Session`` across threads or across an ``await``, and never opens two
sessions at once in one thread (spec 014, D110).

The two signal-side implementations load no rule schema (decision D120); this module does,
because ``RuleRepository`` speaks in parsed rules, and every consumer of the ports loads it
anyway.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trading_bot.domain.rules.schema import Rule
from trading_bot.domain.signals import Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import Assignment, StoredRule, StoredTicker
from trading_bot.persistence.signal_records import (
    DEFAULT_PAGE_SIZE,
    RecordOutcome,
    SignalCursor,
    SignalPage,
    StoredSignal,
)
from trading_bot.persistence.state import BotState, LastRun

__all__ = [
    "AssignmentRepository",
    "BotStateRepository",
    "RuleRepository",
    "SignalRepository",
    "TickerRepository",
]


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


class SignalRepository(Protocol):
    """The signal history, whose identity is the key of CLAUDE.md rule 5."""

    def record(self, signal: Signal) -> RecordOutcome:
        """Store the signal, or return the one already stored under its key (decision D112).

        ``is_new`` is ``True`` for exactly one call per key and only counts once the caller's
        session commits: commit first, then notify, and only on ``is_new=True`` (D115).
        """
        ...

    def get(self, signal_id: int) -> StoredSignal | None: ...

    def mark_notified(self, signal_id: int) -> StoredSignal:
        """Stamp the delivery instant once; a second call keeps the first one."""
        ...

    def latest(
        self, ticker_id: int, rule_id: int, *, notified_only: bool = False
    ) -> StoredSignal | None:
        """The pair's signal with the greatest candle close, which a cooldown counts from."""
        ...

    def history(
        self,
        *,
        ticker_id: int | None = None,
        rule_id: int | None = None,
        timeframe: Timeframe | None = None,
        notified: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        before: SignalCursor | None = None,
    ) -> SignalPage:
        """One keyset page, newest first; ``[since, until)`` bounds the candle close."""
        ...

    def count(self, *, ticker_id: int | None = None, rule_id: int | None = None) -> int: ...


class BotStateRepository(Protocol):
    """The runtime state of the bot: the pause, the heartbeat and the last run per timeframe."""

    def load(self) -> BotState:
        """Every stored fact at once; a missing row means "never"."""
        ...

    def pause(self) -> datetime:
        """Pause the bot and return the instant it was paused; pausing twice keeps the first."""
        ...

    def resume(self) -> bool:
        """Remove the pause; ``False`` when the bot was not paused."""
        ...

    def record_heartbeat(self) -> datetime:
        """Store the clock's instant as the latest sign of life; last write wins."""
        ...

    def record_run(self, timeframe: Timeframe, scheduled_at: datetime) -> LastRun:
        """Record a completed run, monotonically: an earlier or repeated report writes nothing."""
        ...

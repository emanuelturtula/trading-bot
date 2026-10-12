"""Watching and unwatching a ticker, shared by Telegram and the dashboard (spec 020, D6).

Both front ends do exactly the same thing: ``watch_ticker`` stores the ticker enabled (or
re-enables it) and assigns every stored rule of its timeframe (spec 018, Q1); ``unwatch_ticker``
disables it and keeps its assignments and signal history (spec 018, Q2). The functions work on
repositories inside the caller's unit of work and return what happened, so each front end words
its own reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredTicker
from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    RuleRepository,
    TickerRepository,
)

__all__ = ["UnwatchResult", "WatchOutcome", "WatchResult", "unwatch_ticker", "watch_ticker"]


class WatchOutcome(StrEnum):
    ADDED = "added"
    ALREADY_WATCHED = "already_watched"
    REENABLED = "reenabled"


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchResult:
    ticker: StoredTicker
    outcome: WatchOutcome
    assigned_rules: int
    enabled_rules: int


@dataclass(frozen=True, slots=True, kw_only=True)
class UnwatchResult:
    matches: tuple[StoredTicker, ...]  # every ticker of that symbol (and timeframe, if given)
    disabled: tuple[StoredTicker, ...]  # the ones this call switched off


def watch_ticker(
    tickers: TickerRepository,
    rules: RuleRepository,
    assignments: AssignmentRepository,
    symbol: str,
    timeframe: Timeframe,
) -> WatchResult:
    """Store ``symbol`` enabled on ``timeframe`` and assign every rule of that timeframe."""
    existing = tickers.get_by_symbol(symbol, timeframe)
    if existing is None:
        stored = tickers.add(symbol, timeframe, enabled=True)
        outcome = WatchOutcome.ADDED
    elif existing.enabled:
        stored = existing
        outcome = WatchOutcome.ALREADY_WATCHED
    else:
        stored = tickers.set_enabled(existing.id, True)
        outcome = WatchOutcome.REENABLED
    for rule in rules.list_all():
        if rule.rule.timeframe is timeframe:
            assignments.assign(stored.id, rule.id)  # assigning twice is a no-op
    assigned = assignments.rules_for_ticker(stored.id)
    return WatchResult(
        ticker=stored,
        outcome=outcome,
        assigned_rules=len(assigned),
        enabled_rules=sum(1 for rule in assigned if rule.enabled),
    )


def unwatch_ticker(
    tickers: TickerRepository, symbol: str, timeframe: Timeframe | None
) -> UnwatchResult:
    """Disable ``symbol`` on ``timeframe``, or on every timeframe when it is ``None``."""
    matches = tuple(
        ticker
        for ticker in tickers.list_all()
        if ticker.symbol == symbol and (timeframe is None or ticker.timeframe is timeframe)
    )
    disabled = tuple(ticker for ticker in matches if ticker.enabled)
    for ticker in disabled:
        tickers.set_enabled(ticker.id, False)
    return UnwatchResult(matches=matches, disabled=disabled)

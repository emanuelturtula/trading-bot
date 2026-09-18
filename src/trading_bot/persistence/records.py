"""What leaves a session: frozen configuration records (spec 013, Design 6.1, D91).

Records are immutable, hashable by value, free of ORM state and safe to return from an
``asyncio.to_thread`` worker: no lazy load after the session closes, no accidental mutation
reaching the database and no session affinity. ``StoredRule.rule`` is already parsed, so a
caller never has to touch ``definition_json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_bot.domain.rules.schema import Rule
from trading_bot.domain.timeframe import Timeframe

__all__ = ["Assignment", "StoredRule", "StoredTicker"]


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredTicker:
    """A watched symbol on one timeframe, as stored."""

    id: int
    symbol: str
    timeframe: Timeframe
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredRule:
    """A stored rule with its parsed document; ``enabled`` and the id belong to the database."""

    id: int
    rule: Rule
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @property
    def name(self) -> str:
        """The unique name the user addresses the rule by (decision D85)."""
        return self.rule.name


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """One rule evaluated on one ticker. Both ends share the timeframe (decision D86)."""

    ticker_id: int
    rule_id: int
    timeframe: Timeframe
    created_at: datetime

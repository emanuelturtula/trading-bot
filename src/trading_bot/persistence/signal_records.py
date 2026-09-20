"""What leaves a session on the signal side: frozen records and the page bounds (spec 014, 6.1).

Like the configuration records, these are immutable, hashable by value, free of ORM state and
safe to return from an ``asyncio.to_thread`` worker: no lazy load after the session closes and
no session affinity. ``StoredSignal.signal`` is the domain ``Signal`` a notification is built
from, so a caller never touches the stored JSON text, and ``rule_name`` is the rule's **current**
name, read through the join: a renamed rule shows its new name in the history (decision D111).

This module is light on purpose (decision D120): it loads ``domain.signals``, never the rule
schema and the analysis stack behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from trading_bot.domain.signals import Signal, SignalKey
from trading_bot.domain.utc import to_utc

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "RecordOutcome",
    "SignalCursor",
    "SignalPage",
    "StoredSignal",
]

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 500  # bounds the cost of one query on the Raspberry Pi (D116)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSignal:
    """A recorded signal, as stored, with the rule's current name."""

    id: int
    ticker_id: int
    rule_id: int
    rule_name: str
    signal: Signal
    created_at: datetime  # when the row was recorded, from the injected clock
    notified_at: datetime | None  # when a notification was confirmed, ``None`` until then

    @property
    def key(self) -> SignalKey:
        """The identity of CLAUDE.md rule 5, which the unique constraint enforces."""
        return self.signal.idempotency_key


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordOutcome:
    """What ``record`` did: the stored row, and whether this call is the one that created it.

    ``is_new`` is ``True`` for exactly one call per key, and it is **provisional** until the
    caller's session commits: a unit of work that rolls back never created that row, and the
    next attempt gets ``is_new=True`` again. Only the caller that got ``True`` on a committed
    unit of work notifies (decision D115).
    """

    stored: StoredSignal
    is_new: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalCursor:
    """A position in the newest-first history: the last item a page returned (decision D116)."""

    candle_close_ts: datetime
    id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "candle_close_ts", to_utc(self.candle_close_ts))

    @classmethod
    def of(cls, stored: StoredSignal) -> SignalCursor:
        """The cursor that marks ``stored`` as the last item read."""
        return cls(candle_close_ts=stored.signal.candle_close_ts, id=stored.id)


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalPage:
    """One page of the history, newest first, and where to continue from."""

    items: tuple[StoredSignal, ...]
    next_cursor: SignalCursor | None  # ``None`` on the last page

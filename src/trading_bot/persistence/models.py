"""The ORM models of the application (spec 012, Design 5; spec 013, Design 3; spec 014, 3).

The three configuration tables, the signal history and the bot state. **Every model must be
defined here or imported by this module**, because ``migrations/env.py`` reads
``Base.metadata`` through it and ``--autogenerate`` only compares what is imported. Timestamp
columns always use ``UtcDateTime`` (``types.py``).

The ``Row`` suffix keeps ``RuleRow`` from shadowing the domain's ``Rule`` in every module that
handles both. The models declare no relationship: repositories return frozen records
(spec 013, D91), so nothing lazy-loads after a session closes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.base import Base
from trading_bot.persistence.state import StateKey
from trading_bot.persistence.types import SideType, StateKeyType, TimeframeType, UtcDateTime

__all__ = ["Base", "BotStateRow", "RuleRow", "SignalRow", "TickerRow", "TickerRuleRow"]

# Built from the enumerations, so adding a member changes the rendered DDL and fails the
# golden-DDL assertion until a migration is written (spec 013, Design 3).
_TIMEFRAME_CODES = ", ".join(f"'{member.value}'" for member in Timeframe)
_SIDE_CODES = ", ".join(f"'{member.value}'" for member in Side)
_STATE_KEYS = ", ".join(f"'{member.value}'" for member in StateKey)


class TickerRow(Base):
    """A watched symbol on one timeframe. ``(symbol, timeframe)`` is its natural key."""

    __tablename__ = "tickers"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe"),
        # The composite foreign key of ``ticker_rules`` needs this key on the parent, which is
        # what makes the D11 timeframe rule a schema invariant.
        UniqueConstraint("id", "timeframe"),
        CheckConstraint(f"timeframe IN ({_TIMEFRAME_CODES})", name="timeframe"),
        # Without it SQLite reuses the rowid of a deleted last row, and #13's signal identity
        # would let a new ticker inherit the notification history of a deleted one.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[Timeframe] = mapped_column(TimeframeType)
    enabled: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class RuleRow(Base):
    """A stored rule: the canonical document plus the columns queries and constraints need.

    ``name``, ``signal`` and ``timeframe`` duplicate values of ``definition_json`` (D90). The
    repository is their only writer and derives all three from the same parsed rule, in the
    same statement as the document, so they cannot drift.
    """

    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("name"),
        UniqueConstraint("id", "timeframe"),
        CheckConstraint(f"timeframe IN ({_TIMEFRAME_CODES})", name="timeframe"),
        CheckConstraint(f"signal IN ({_SIDE_CODES})", name="signal"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    signal: Mapped[Side] = mapped_column(SideType)
    timeframe: Mapped[Timeframe] = mapped_column(TimeframeType)
    definition_json: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class TickerRuleRow(Base):
    """An assignment: this rule is evaluated on this ticker.

    Both foreign keys carry the timeframe, so the database refuses an assignment whose ends
    disagree and refuses to change either end's timeframe while it exists (spec 006 D11,
    spec 013 D86). Deleting a ticker or a rule cascades here and nowhere else.
    """

    __tablename__ = "ticker_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ticker_id", "timeframe"], ["tickers.id", "tickers.timeframe"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["rule_id", "timeframe"], ["rules.id", "rules.timeframe"], ondelete="CASCADE"
        ),
    )

    ticker_id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(primary_key=True, index=True)
    timeframe: Mapped[Timeframe] = mapped_column(TimeframeType)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class SignalRow(Base):
    """A recorded signal: its identity, what a notification shows and its delivery state.

    The unique key is the signal identity of CLAUDE.md rule 5, so no thread, process, retry or
    restart can store one twice (spec 014, D111, D112). The ticker key carries the timeframe,
    whose value never changes for a ticker (D97); the rule key does not, so a rule without
    assignments may change timeframe and its history keeps the one it was recorded with
    (D113). Deleting a ticker or a rule deletes its signals through the cascade.
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("ticker_id", "rule_id", "timeframe", "candle_close_ts"),
        ForeignKeyConstraint(
            ["ticker_id", "timeframe"], ["tickers.id", "tickers.timeframe"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["rule_id"], ["rules.id"], ondelete="CASCADE"),
        CheckConstraint(f"timeframe IN ({_TIMEFRAME_CODES})", name="timeframe"),
        CheckConstraint(f"side IN ({_SIDE_CODES})", name="side"),
        CheckConstraint("close_price > 0", name="close_price"),
        # The rule history and the rule cascade; the unique key already serves the ticker's.
        Index(None, "rule_id", "candle_close_ts"),
        # Signal ids appear in cursors and references: a reused id would repoint them.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int]
    rule_id: Mapped[int]
    timeframe: Mapped[Timeframe] = mapped_column(TimeframeType)
    # The nominal close exactly as the evaluation gives it, never recomputed (D114); the
    # index serves the unfiltered newest-first page.
    candle_close_ts: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    side: Mapped[Side] = mapped_column(SideType)
    close_price: Mapped[float] = mapped_column(Float)
    indicator_values_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class BotStateRow(Base):
    """One fact of the bot state: a ``StateKey`` and the UTC instant it holds (D118).

    A missing row means "never", so the table needs no seed data.
    """

    __tablename__ = "bot_state"
    __table_args__ = (CheckConstraint(f"key IN ({_STATE_KEYS})", name="key"),)

    key: Mapped[StateKey] = mapped_column(StateKeyType, primary_key=True)
    value: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

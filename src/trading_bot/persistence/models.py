"""The ORM models of the application (spec 012, Design 5; spec 013, Design 3).

This feature ships the three configuration tables; signals and ``bot_state`` arrive with #13.
**Every model must be defined here or imported by this module**, because ``migrations/env.py``
reads ``Base.metadata`` through it and ``--autogenerate`` only compares what is imported.
Timestamp columns always use ``UtcDateTime`` (``types.py``).

The ``Row`` suffix keeps ``RuleRow`` from shadowing the domain's ``Rule`` in every module that
handles both. The models declare no relationship: repositories return frozen records
(spec 013, D91), so nothing lazy-loads after a session closes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.base import Base
from trading_bot.persistence.types import SideType, TimeframeType, UtcDateTime

__all__ = ["Base", "RuleRow", "TickerRow", "TickerRuleRow"]

# Built from the enumerations, so adding a member changes the rendered DDL and fails the
# golden-DDL assertion until a migration is written (spec 013, Design 3).
_TIMEFRAME_CODES = ", ".join(f"'{member.value}'" for member in Timeframe)
_SIDE_CODES = ", ".join(f"'{member.value}'" for member in Side)


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

"""add the signal history and the bot state tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-19

Generated with ``--autogenerate`` from the models of ``persistence/models.py`` and reviewed
(spec 014, Design 11). The revision is **self-contained**, as ``0002`` is: the check expressions
are frozen here as text and the column types are the SQLAlchemy ones the project types render
(``UtcDateTime`` is a ``DateTime``; ``TimeframeType``, ``SideType`` and ``StateKeyType`` are
``String``), so renaming or removing one of them later cannot break ``upgrade head`` on a fresh
database. A later change to a ``Timeframe``, ``Side`` or ``StateKey`` member needs a revision of
its own, not a silent rewrite of this one. No row is written, so there is nothing to backfill.
``downgrade`` drops both tables and with them the signal history and the bot state; it exists
for the tests and for a clean rollback of an unreleased schema, never as an operational
procedure: the pre-deploy backup is the recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``signals`` with its two indexes, and ``bot_state``.

    The unique key is the signal identity of CLAUDE.md rule 5 (decision D112).
    ``sqlite_autoincrement`` keeps a deleted signal's id from being reused, since ids appear in
    cursors and references. The ticker key carries the timeframe and the rule key does not
    (D113); both cascade on delete.
    """
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("candle_close_ts", sa.DateTime(), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("indicator_values_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        # The constraints are declared in the order of ``models.py``: SQLAlchemy renders them
        # in declaration order, so the stored DDL is the one the golden assertion pins.
        sa.UniqueConstraint(
            "ticker_id",
            "rule_id",
            "timeframe",
            "candle_close_ts",
            name=op.f("uq_signals_ticker_id_rule_id_timeframe_candle_close_ts"),
        ),
        sa.ForeignKeyConstraint(
            ["ticker_id", "timeframe"],
            ["tickers.id", "tickers.timeframe"],
            name=op.f("fk_signals_ticker_id_timeframe_tickers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name=op.f("fk_signals_rule_id_rules"), ondelete="CASCADE"
        ),
        sa.CheckConstraint("timeframe IN ('1h', '4h', '1d')", name=op.f("ck_signals_timeframe")),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_signals_side")),
        sa.CheckConstraint("close_price > 0", name=op.f("ck_signals_close_price")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_signals_rule_id_candle_close_ts"),
        "signals",
        ["rule_id", "candle_close_ts"],
        unique=False,
    )
    op.create_index(
        op.f("ix_signals_candle_close_ts"), "signals", ["candle_close_ts"], unique=False
    )
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("value", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "key IN ('paused_since', 'last_heartbeat', 'last_run.1h', 'last_run.4h',"
            " 'last_run.1d')",
            name=op.f("ck_bot_state_key"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_bot_state")),
    )


def downgrade() -> None:
    """Drop both indexes, then ``signals``, then ``bot_state``; the configuration stays."""
    op.drop_index(op.f("ix_signals_candle_close_ts"), table_name="signals")
    op.drop_index(op.f("ix_signals_rule_id_candle_close_ts"), table_name="signals")
    op.drop_table("signals")
    op.drop_table("bot_state")

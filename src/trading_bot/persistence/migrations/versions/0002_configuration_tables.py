"""add the ticker, rule and assignment tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17

Generated with ``--autogenerate`` from the models of ``persistence/models.py`` and reviewed
(spec 013, Design 5.1). The revision is **self-contained**: the check expressions are frozen
here as text and the column types are the SQLAlchemy ones the project types render
(``UtcDateTime`` is a ``DateTime``, ``TimeframeType`` and ``SideType`` are ``String``), so
renaming or removing one of them later cannot break ``upgrade head`` on a fresh database, which
would leave a container unable to start. A later change to a ``Timeframe`` or ``Side`` member
needs a revision of its own, not a silent rewrite of this one.
``downgrade`` drops the three tables and with them the configuration; it exists for the tests
and for a clean rollback of an unreleased schema, never as an operational procedure.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``tickers``, ``rules`` and ``ticker_rules``.

    ``sqlite_autoincrement`` keeps SQLite from reusing the rowid of a deleted last row, which
    #13's signal identity depends on. The two composite foreign keys make the timeframe rule of
    spec 006 D11 a schema invariant (spec 013, D86).
    """
    op.create_table(
        "tickers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # The constraints are declared in the order of ``models.py``: SQLAlchemy renders them
        # in declaration order, so the stored DDL is the one the golden assertion pins.
        sa.UniqueConstraint("symbol", "timeframe", name=op.f("uq_tickers_symbol_timeframe")),
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_tickers_id_timeframe")),
        sa.CheckConstraint("timeframe IN ('1h', '4h', '1d')", name=op.f("ck_tickers_timeframe")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickers")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("signal", sa.String(length=4), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name=op.f("uq_rules_name")),
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_rules_id_timeframe")),
        sa.CheckConstraint("timeframe IN ('1h', '4h', '1d')", name=op.f("ck_rules_timeframe")),
        sa.CheckConstraint("signal IN ('BUY', 'SELL')", name=op.f("ck_rules_signal")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "ticker_rules",
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticker_id", "timeframe"],
            ["tickers.id", "tickers.timeframe"],
            name=op.f("fk_ticker_rules_ticker_id_timeframe_tickers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "timeframe"],
            ["rules.id", "rules.timeframe"],
            name=op.f("fk_ticker_rules_rule_id_timeframe_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticker_id", "rule_id", name=op.f("pk_ticker_rules")),
    )
    op.create_index(op.f("ix_ticker_rules_rule_id"), "ticker_rules", ["rule_id"], unique=False)


def downgrade() -> None:
    """Drop the three tables, children first: the foreign keys are enforced."""
    op.drop_index(op.f("ix_ticker_rules_rule_id"), table_name="ticker_rules")
    op.drop_table("ticker_rules")
    op.drop_table("rules")
    op.drop_table("tickers")

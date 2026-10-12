"""add the weekly timeframe

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12

Spec 028. The ``CHECK`` constraints of ``tickers``, ``rules`` and ``signals`` accept ``1w``, and
the one of ``bot_state`` accepts ``last_run.1w``. SQLite cannot change a constraint in place, so
the tables are rebuilt the way revision ``0005`` rebuilt ``tickers`` and ``rules``: the children
(``ticker_rules`` and ``signals``) are copied aside and dropped first, so dropping a parent never
runs their ``ON DELETE CASCADE``; the parents are renamed out of the way and recreated; the
children are recreated with their exact DDL and the ``sqlite_sequence`` counters are restored, so
no deleted id is ever reused. Self-contained like the earlier revisions.

``downgrade`` deletes every ``1w`` row first (tickers, rules and their assignments and signals,
and ``last_run.1w``), because the old constraints reject them, then rebuilds the old DDL. The
pre-deploy backup is the recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TIMEFRAMES = "timeframe IN ('1h', '4h', '1d')"
_NEW_TIMEFRAMES = "timeframe IN ('1h', '4h', '1d', '1w')"
_OLD_STATE_KEYS = (
    "key IN ('paused_since', 'last_heartbeat', 'last_run.1h', 'last_run.4h', 'last_run.1d')"
)
_NEW_STATE_KEYS = (
    "key IN ('paused_since', 'last_heartbeat', 'last_run.1h', 'last_run.4h', 'last_run.1d',"
    " 'last_run.1w')"
)
_SEQUENCED = ("tickers", "rules", "signals")


def upgrade() -> None:
    _rebuild(_NEW_TIMEFRAMES, _NEW_STATE_KEYS)


def downgrade() -> None:
    # Children first, so no cascade is needed; a 1w ticker or rule only has 1w children.
    op.execute("DELETE FROM signals WHERE timeframe = '1w'")
    op.execute("DELETE FROM ticker_rules WHERE timeframe = '1w'")
    op.execute("DELETE FROM tickers WHERE timeframe = '1w'")
    op.execute("DELETE FROM rules WHERE timeframe = '1w'")
    op.execute("DELETE FROM bot_state WHERE key = 'last_run.1w'")
    _rebuild(_OLD_TIMEFRAMES, _OLD_STATE_KEYS)


def _rebuild(timeframes: str, state_keys: str) -> None:
    connection = op.get_bind()
    sequences = _sequences(connection)
    _set_children_aside()
    _rebuild_parents(timeframes)
    _restore_children(timeframes)
    _rebuild_bot_state(state_keys)
    _restore_sequences(connection, sequences)


def _sequences(connection: sa.Connection) -> dict[str, int]:
    rows = connection.exec_driver_sql("SELECT name, seq FROM sqlite_sequence").all()
    return {str(name): int(seq) for name, seq in rows if name in _SEQUENCED}


def _restore_sequences(connection: sa.Connection, sequences: dict[str, int]) -> None:
    for name, seq in sequences.items():
        updated = connection.exec_driver_sql(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (seq, name)
        )
        if not updated.rowcount:
            connection.exec_driver_sql(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, seq)
            )


def _set_children_aside() -> None:
    op.execute("CREATE TABLE _weekly_ticker_rules AS SELECT * FROM ticker_rules")
    op.execute("CREATE TABLE _weekly_signals AS SELECT * FROM signals")
    # Dropping a child deletes its own rows only: nothing references it.
    op.execute("DROP TABLE signals")
    op.execute("DROP TABLE ticker_rules")


def _restore_children(timeframes: str) -> None:
    _create_ticker_rules()
    _create_signals(timeframes)
    op.execute(
        "INSERT INTO ticker_rules (ticker_id, rule_id, timeframe, created_at)"
        " SELECT ticker_id, rule_id, timeframe, created_at FROM _weekly_ticker_rules"
    )
    op.execute(
        "INSERT INTO signals (id, ticker_id, rule_id, timeframe, candle_close_ts, side,"
        " close_price, indicator_values_json, created_at, notified_at)"
        " SELECT id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at, notified_at FROM _weekly_signals"
    )
    op.execute("DROP TABLE _weekly_ticker_rules")
    op.execute("DROP TABLE _weekly_signals")


def _rebuild_parents(timeframes: str) -> None:
    op.execute("ALTER TABLE tickers RENAME TO _weekly_tickers")
    op.execute("ALTER TABLE rules RENAME TO _weekly_rules")
    _create_tickers(timeframes)
    _create_rules(timeframes)
    op.execute(
        "INSERT INTO tickers (id, symbol, timeframe, enabled, created_at, user_id)"
        " SELECT id, symbol, timeframe, enabled, created_at, user_id FROM _weekly_tickers"
    )
    op.execute(
        "INSERT INTO rules (id, name, signal, timeframe, definition_json, enabled,"
        " created_at, updated_at, user_id)"
        " SELECT id, name, signal, timeframe, definition_json, enabled, created_at,"
        " updated_at, user_id FROM _weekly_rules"
    )
    op.execute("DROP TABLE _weekly_tickers")
    op.execute("DROP TABLE _weekly_rules")


def _rebuild_bot_state(state_keys: str) -> None:
    op.execute("ALTER TABLE bot_state RENAME TO _weekly_bot_state")
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("value", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(state_keys, name=op.f("ck_bot_state_key")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_bot_state")),
    )
    op.execute(
        'INSERT INTO bot_state ("key", value, updated_at)'
        ' SELECT "key", value, updated_at FROM _weekly_bot_state'
    )
    op.execute("DROP TABLE _weekly_bot_state")


def _create_tickers(timeframes: str) -> None:
    """The DDL of revision ``0005`` with ``timeframes`` as the timeframe check."""
    op.create_table(
        "tickers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "symbol", "timeframe", name=op.f("uq_tickers_user_id_symbol_timeframe")
        ),
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_tickers_id_timeframe")),
        sa.CheckConstraint(timeframes, name=op.f("ck_tickers_timeframe")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_tickers_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickers")),
        sqlite_autoincrement=True,
    )


def _create_rules(timeframes: str) -> None:
    """The DDL of revision ``0005`` with ``timeframes`` as the timeframe check."""
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
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_rules_user_id_name")),
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_rules_id_timeframe")),
        sa.CheckConstraint(timeframes, name=op.f("ck_rules_timeframe")),
        sa.CheckConstraint("signal IN ('BUY', 'SELL')", name=op.f("ck_rules_signal")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_rules_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
        sqlite_autoincrement=True,
    )


def _create_ticker_rules() -> None:
    """Exactly the DDL of revision ``0002``."""
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


def _create_signals(timeframes: str) -> None:
    """The DDL of revision ``0003`` with ``timeframes`` as the timeframe check."""
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
        sa.CheckConstraint(timeframes, name=op.f("ck_signals_timeframe")),
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

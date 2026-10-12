"""give tickers and rules an owner

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12

Spec 024, D1 and D4. ``tickers`` and ``rules`` gain ``user_id`` and their natural keys become
``(user_id, symbol, timeframe)`` and ``(user_id, name)``. Every existing row goes to the
administrator with the lowest id; when there is no user yet, an ``admin`` account without a
password is created first, and the first login sets its password (spec 023, D2).

SQLite cannot change a table constraint in place, so both tables are rebuilt. Dropping a table
that other tables reference would run their ``ON DELETE CASCADE`` (foreign keys are enforced on
every connection), so the children go first: ``ticker_rules`` and ``signals`` are copied aside,
dropped and recreated with their exact DDL once the parents are rebuilt, and the
``sqlite_sequence`` counters are restored so no deleted id is ever reused. The old parent is
renamed out of the way before the new one is created, which keeps the stored DDL identical to
what the models render. Self-contained like the earlier revisions.

``downgrade`` drops ``user_id`` again; it fails on a unique constraint when two users share a
symbol and timeframe or a rule name. The pre-deploy backup is the recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMEFRAMES = "timeframe IN ('1h', '4h', '1d')"
_SEQUENCED = ("tickers", "rules", "signals")


def upgrade() -> None:
    connection = op.get_bind()
    owner = _owner(connection)
    sequences = _sequences(connection)
    _set_children_aside()
    _rebuild_parents(owned=True, owner=owner)
    _restore_children()
    _restore_sequences(connection, sequences)


def downgrade() -> None:
    connection = op.get_bind()
    sequences = _sequences(connection)
    _set_children_aside()
    _rebuild_parents(owned=False, owner=None)
    _restore_children()
    _restore_sequences(connection, sequences)


def _owner(connection: sa.Connection) -> int:
    """The administrator with the lowest id, created as ``admin`` when there is no user."""
    found = connection.exec_driver_sql(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
    ).scalar()
    if found is not None:
        return int(found)
    if connection.exec_driver_sql("SELECT COUNT(*) FROM users").scalar():
        first = int(connection.exec_driver_sql("SELECT MIN(id) FROM users").scalar_one())
        connection.exec_driver_sql("UPDATE users SET is_admin = 1 WHERE id = ?", (first,))
        return first
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    connection.exec_driver_sql(
        "INSERT INTO users (username, password_hash, is_admin, active, invite_digest,"
        " invite_expires_at, created_at, updated_at)"
        " VALUES ('admin', NULL, 1, 1, NULL, NULL, ?, ?)",
        (now, now),
    )
    return int(connection.exec_driver_sql("SELECT MIN(id) FROM users").scalar_one())


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
    op.execute("CREATE TABLE _owners_ticker_rules AS SELECT * FROM ticker_rules")
    op.execute("CREATE TABLE _owners_signals AS SELECT * FROM signals")
    # Dropping a child deletes its own rows only: nothing references it.
    op.execute("DROP TABLE signals")
    op.execute("DROP TABLE ticker_rules")


def _restore_children() -> None:
    _create_ticker_rules()
    _create_signals()
    op.execute("INSERT INTO ticker_rules SELECT * FROM _owners_ticker_rules")
    op.execute(
        "INSERT INTO signals (id, ticker_id, rule_id, timeframe, candle_close_ts, side,"
        " close_price, indicator_values_json, created_at, notified_at)"
        " SELECT id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at, notified_at FROM _owners_signals"
    )
    op.execute("DROP TABLE _owners_ticker_rules")
    op.execute("DROP TABLE _owners_signals")


def _rebuild_parents(*, owned: bool, owner: int | None) -> None:
    op.execute("ALTER TABLE tickers RENAME TO _owners_tickers")
    op.execute("ALTER TABLE rules RENAME TO _owners_rules")
    _create_tickers(owned=owned)
    _create_rules(owned=owned)
    connection = op.get_bind()
    if owned:
        connection.exec_driver_sql(
            "INSERT INTO tickers (id, symbol, timeframe, enabled, created_at, user_id)"
            " SELECT id, symbol, timeframe, enabled, created_at, ? FROM _owners_tickers",
            (owner,),
        )
        connection.exec_driver_sql(
            "INSERT INTO rules (id, name, signal, timeframe, definition_json, enabled,"
            " created_at, updated_at, user_id)"
            " SELECT id, name, signal, timeframe, definition_json, enabled, created_at,"
            " updated_at, ? FROM _owners_rules",
            (owner,),
        )
    else:
        op.execute(
            "INSERT INTO tickers (id, symbol, timeframe, enabled, created_at)"
            " SELECT id, symbol, timeframe, enabled, created_at FROM _owners_tickers"
        )
        op.execute(
            "INSERT INTO rules (id, name, signal, timeframe, definition_json, enabled,"
            " created_at, updated_at)"
            " SELECT id, name, signal, timeframe, definition_json, enabled, created_at,"
            " updated_at FROM _owners_rules"
        )
    op.execute("DROP TABLE _owners_tickers")
    op.execute("DROP TABLE _owners_rules")


def _create_tickers(*, owned: bool) -> None:
    columns: list[Any] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    ]
    if owned:
        columns += [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "symbol", "timeframe", name=op.f("uq_tickers_user_id_symbol_timeframe")
            ),
        ]
    else:
        columns.append(
            sa.UniqueConstraint("symbol", "timeframe", name=op.f("uq_tickers_symbol_timeframe"))
        )
    columns += [
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_tickers_id_timeframe")),
        sa.CheckConstraint(_TIMEFRAMES, name=op.f("ck_tickers_timeframe")),
    ]
    if owned:
        columns.append(
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name=op.f("fk_tickers_user_id_users"), ondelete="CASCADE"
            )
        )
    columns.append(sa.PrimaryKeyConstraint("id", name=op.f("pk_tickers")))
    op.create_table("tickers", *columns, sqlite_autoincrement=True)


def _create_rules(*, owned: bool) -> None:
    columns: list[Any] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("signal", sa.String(length=4), nullable=False),
        sa.Column("timeframe", sa.String(length=2), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]
    if owned:
        columns += [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.UniqueConstraint("user_id", "name", name=op.f("uq_rules_user_id_name")),
        ]
    else:
        columns.append(sa.UniqueConstraint("name", name=op.f("uq_rules_name")))
    columns += [
        sa.UniqueConstraint("id", "timeframe", name=op.f("uq_rules_id_timeframe")),
        sa.CheckConstraint(_TIMEFRAMES, name=op.f("ck_rules_timeframe")),
        sa.CheckConstraint("signal IN ('BUY', 'SELL')", name=op.f("ck_rules_signal")),
    ]
    if owned:
        columns.append(
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name=op.f("fk_rules_user_id_users"), ondelete="CASCADE"
            )
        )
    columns.append(sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")))
    op.create_table("rules", *columns, sqlite_autoincrement=True)


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


def _create_signals() -> None:
    """Exactly the DDL of revision ``0003``."""
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
        sa.CheckConstraint(_TIMEFRAMES, name=op.f("ck_signals_timeframe")),
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

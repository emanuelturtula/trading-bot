"""add each user's pause and Telegram bot

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12

Spec 025, D1-D6. Three new tables, so no existing row is touched: ``user_pauses`` (a row means
the user paused their own signals), ``telegram_bots`` (one bot per user, the token encrypted
under ``TB_MASTER_KEY``) and ``telegram_chats`` (the chats linked to that bot). Self-contained
like the earlier revisions. ``downgrade`` drops the three tables; the pre-deploy backup is the
recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_pauses",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("paused_since", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_pauses_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_pauses")),
    )
    op.create_table(
        "telegram_bots",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("token_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("bot_username", sa.String(length=64), nullable=True),
        sa.Column("link_digest", sa.String(length=64), nullable=True),
        sa.Column("link_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_telegram_bots")),
        sa.UniqueConstraint("token_fingerprint", name=op.f("uq_telegram_bots_token_fingerprint")),
        sa.UniqueConstraint("link_digest", name=op.f("uq_telegram_bots_link_digest")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_bots_user_id_users"),
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "telegram_chats",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_chats_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "chat_id", name=op.f("pk_telegram_chats")),
    )


def downgrade() -> None:
    op.drop_table("telegram_chats")
    op.drop_table("telegram_bots")
    op.drop_table("user_pauses")

"""add the dashboard users table

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12

Spec 023, D1. Self-contained like ``0002`` and ``0003``: the column types are the SQLAlchemy
ones the project types render (``UtcDateTime`` is a ``DateTime``). No row is written: the first
administrator is created by the dashboard from ``TB_DASHBOARD_PASSWORD_HASH`` while the table is
empty (D2), because a migration cannot read the settings. ``downgrade`` drops the table and every
account with it; the pre-deploy backup is the recovery path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``users``; ``sqlite_autoincrement`` keeps a deleted account's id from being reused."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("invite_digest", sa.String(length=64), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        sa.UniqueConstraint("invite_digest", name=op.f("uq_users_invite_digest")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("users")

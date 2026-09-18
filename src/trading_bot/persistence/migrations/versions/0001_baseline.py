"""baseline: the empty starting point of the schema

Revision ID: 0001
Revises:
Create Date: 2026-09-17

The first revision creates nothing (spec 012, D78). It exists so that ``upgrade head`` on an
empty volume really creates ``alembic_version``, and so that #12 has a ``down_revision`` to
anchor its own revision to instead of opening a second head.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create nothing: the first tables arrive with #12 and #13."""


def downgrade() -> None:
    """Drop nothing: this revision creates no database object."""

"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the change. Use ``op.batch_alter_table`` for any ALTER (SQLite, spec 012 D75)."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert the change: every revision must be reversible on a temporary database."""
    ${downgrades if downgrades else "pass"}

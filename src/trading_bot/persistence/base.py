"""The declarative base and the constraint naming convention (spec 012, Design 5)."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# SQLite names unnamed constraints itself and emulates ``ALTER TABLE`` by copying the table,
# which makes an unnamed constraint impossible to drop in a later migration. Fixing the
# convention before the first table means every name is deterministic and reviewable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The single declarative base of the application; never build a second one."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

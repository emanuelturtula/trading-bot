"""Whose configuration a repository works on (spec 024, D2).

Tickers and rules belong to a user. A repository is built with an ``owner``:

- a user id: it reads and writes that user's rows only, and a row of another user is treated as
  missing, so no caller can reach it by id;
- ``None`` (the default): the **default owner**, the administrator with the lowest id. The
  Telegram bot and the management CLI work on that user's configuration, as they did before
  there were users;
- ``ALL_OWNERS``: every user's rows, for the engine. Such a repository refuses to create rows.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.persistence.errors import PersistenceError
from trading_bot.persistence.models import UserRow

__all__ = ["ALL_OWNERS", "AllOwners", "NoOwnerError", "Owner", "OwnerScope", "default_owner_id"]


class AllOwners(Enum):
    ALL = "all"


ALL_OWNERS: Final = AllOwners.ALL

type Owner = int | AllOwners | None


class NoOwnerError(PersistenceError):
    """There is no administrator to own the configuration (migration ``0005`` creates one)."""

    def __init__(self) -> None:
        super().__init__("no administrator owns the configuration")


def default_owner_id(session: Session) -> int:
    """The id of the administrator with the lowest id."""
    statement = select(UserRow.id).where(UserRow.is_admin.is_(True)).order_by(UserRow.id).limit(1)
    found = session.execute(statement).scalar_one_or_none()
    if found is None:
        raise NoOwnerError
    return int(found)


class OwnerScope:
    """The resolved ``Owner`` of one repository; the default owner is looked up once, lazily."""

    def __init__(self, session: Session, owner: Owner) -> None:
        if owner is not None and not isinstance(owner, AllOwners | int):
            raise TypeError(f"owner must be a user id, None or ALL_OWNERS, got {owner!r}")
        self._session = session
        self._owner = owner
        self._resolved: int | None = None

    @property
    def everyone(self) -> bool:
        return self._owner is ALL_OWNERS

    @property
    def user_id(self) -> int:
        """The single owner rows are read for and written as; refused for ``ALL_OWNERS``."""
        if isinstance(self._owner, AllOwners):
            raise ValueError("a repository over every owner cannot create rows")
        if isinstance(self._owner, int):
            return self._owner
        if self._resolved is None:
            self._resolved = default_owner_id(self._session)
        return self._resolved

    def allows(self, user_id: int) -> bool:
        """Whether a row of ``user_id`` is visible through this scope."""
        return self.everyone or user_id == self.user_id

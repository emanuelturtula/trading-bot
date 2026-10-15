"""The user repository over a ``Session`` (spec 023, D1-D3).

Like the other repositories it never commits, rolls back or closes. A duplicate username is
detected with a ``SELECT`` before the insert, so the typed error leaves the session usable; the
unique constraint stays the backstop. Access links are looked up by the SHA-256 digest of the
link token: the token itself is never stored (D3).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import DuplicateUsernameError, UnknownUserError
from trading_bot.persistence.models import UserPauseRow, UserRow
from trading_bot.persistence.records import StoredUser

__all__ = ["SqlUserRepository", "normalize_username"]

_USERNAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")


def normalize_username(text: str) -> str:
    """``text`` lowercased and stripped; 3-32 of ``a-z 0-9 . _ -``, starting alphanumeric.

    Raises ``ValueError`` with a fixed message, so a caller may show it as is.
    """
    username = text.strip().lower()
    if not _USERNAME.fullmatch(username):
        raise ValueError(
            "A username has 3 to 32 characters: letters, digits, '.', '_' or '-',"
            " starting with a letter or a digit."
        )
    return username


def to_record(row: UserRow) -> StoredUser:
    return StoredUser(
        id=row.id,
        username=row.username,
        password_hash=row.password_hash,
        is_admin=row.is_admin,
        active=row.active,
        invite_expires_at=None if row.invite_expires_at is None else to_utc(row.invite_expires_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


class SqlUserRepository:
    """``UserRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def count(self) -> int:
        return int(self._session.execute(select(func.count()).select_from(UserRow)).scalar_one())

    def count_active_admins(self) -> int:
        statement = (
            select(func.count())
            .select_from(UserRow)
            .where(UserRow.is_admin.is_(True), UserRow.active.is_(True))
        )
        return int(self._session.execute(statement).scalar_one())

    def get(self, user_id: int) -> StoredUser | None:
        row = self._session.get(UserRow, user_id)
        return None if row is None else to_record(row)

    def get_by_username(self, username: str) -> StoredUser | None:
        row = self._by_username(username.strip().lower())
        return None if row is None else to_record(row)

    def list_all(self) -> tuple[StoredUser, ...]:
        rows = self._session.execute(select(UserRow).order_by(UserRow.username)).scalars().all()
        return tuple(to_record(row) for row in rows)

    def add(
        self, username: str, *, is_admin: bool = False, password_hash: str | None = None
    ) -> StoredUser:
        normalized = normalize_username(username)
        if self._by_username(normalized) is not None:
            raise DuplicateUsernameError(normalized)
        now = to_utc(self._clock())
        row = UserRow(
            username=normalized,
            password_hash=password_hash,
            is_admin=is_admin,
            active=True,
            invite_digest=None,
            invite_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return to_record(row)

    def set_password(self, user_id: int, password_hash: str) -> StoredUser:
        """Store a new hash and cancel any pending access link."""
        row = self._row(user_id)
        row.password_hash = password_hash
        row.invite_digest = None
        row.invite_expires_at = None
        return self._touched(row)

    def issue_invite(self, user_id: int, digest: str, lifetime: timedelta) -> StoredUser:
        """Replace any earlier link with ``digest`` and clear the password (D4).

        Clearing the password logs every session of the account out, since a session is bound to
        the hash, and leaves the new link as the only way in.
        """
        row = self._row(user_id)
        row.password_hash = None
        row.invite_digest = digest
        row.invite_expires_at = to_utc(self._clock()) + lifetime
        return self._touched(row)

    def invited(self, digest: str) -> StoredUser | None:
        """The active user whose unexpired access link has ``digest``, or ``None``."""
        row = self._invited_row(digest)
        return None if row is None else to_record(row)

    def redeem_invite(self, digest: str, password_hash: str) -> StoredUser | None:
        """Set the password of the user a valid link belongs to; the link stops working."""
        row = self._invited_row(digest)
        if row is None:
            return None
        return self.set_password(row.id, password_hash)

    def rename(self, user_id: int, username: str) -> StoredUser:
        normalized = normalize_username(username)
        row = self._row(user_id)
        if row.username == normalized:
            return to_record(row)
        if self._by_username(normalized) is not None:
            raise DuplicateUsernameError(normalized)
        row.username = normalized
        return self._touched(row)

    def set_active(self, user_id: int, active: bool) -> StoredUser:
        row = self._row(user_id)
        if row.active == active:
            return to_record(row)
        row.active = active
        return self._touched(row)

    def paused_since(self, user_id: int) -> datetime | None:
        """When ``user_id`` paused their own signals, or ``None`` (spec 025, D6)."""
        row = self._session.get(UserPauseRow, user_id)
        return None if row is None else to_utc(row.paused_since)

    def pause(self, user_id: int) -> datetime:
        """Pause ``user_id``'s signals; idempotent, returns when the pause started."""
        self._row(user_id)
        row = self._session.get(UserPauseRow, user_id)
        if row is None:
            row = UserPauseRow(user_id=user_id, paused_since=to_utc(self._clock()))
            self._session.add(row)
            self._session.flush()
        return to_utc(row.paused_since)

    def resume(self, user_id: int) -> bool:
        """End ``user_id``'s pause. ``False`` when they were not paused."""
        row = self._session.get(UserPauseRow, user_id)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def _invited_row(self, digest: str) -> UserRow | None:
        if not digest:
            return None
        statement = select(UserRow).where(UserRow.invite_digest == digest)
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None or not row.active or row.invite_expires_at is None:
            return None
        if to_utc(row.invite_expires_at) <= to_utc(self._clock()):
            return None
        return row

    def _by_username(self, username: str) -> UserRow | None:
        statement = select(UserRow).where(UserRow.username == username)
        return self._session.execute(statement).scalar_one_or_none()

    def _row(self, user_id: int) -> UserRow:
        row = self._session.get(UserRow, user_id)
        if row is None:
            raise UnknownUserError(user_id)
        return row

    def _touched(self, row: UserRow) -> StoredUser:
        row.updated_at = to_utc(self._clock())
        self._session.flush()
        return to_record(row)

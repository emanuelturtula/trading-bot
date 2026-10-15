"""Each user's Telegram bot and linked chats, over a ``Session`` (spec 025, D1-D4).

Like the other repositories it never commits, rolls back or closes. The repository only moves
ciphertext: encrypting and decrypting the token is the caller's job (``SecretBox``), so the
database layer never holds a token in clear. A link code is looked up by its SHA-256 digest.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import DuplicateBotTokenError
from trading_bot.persistence.models import TelegramBotRow, TelegramChatRow, UserRow
from trading_bot.persistence.records import LinkedChat, StoredBot

__all__ = ["MAX_CHAT_TITLE", "SqlTelegramRepository"]

MAX_CHAT_TITLE = 128


def _bot(row: TelegramBotRow) -> StoredBot:
    return StoredBot(
        user_id=row.user_id,
        token_ciphertext=row.token_ciphertext,
        token_fingerprint=row.token_fingerprint,
        bot_username=row.bot_username,
        link_expires_at=None if row.link_expires_at is None else to_utc(row.link_expires_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def _chat(row: TelegramChatRow) -> LinkedChat:
    return LinkedChat(
        user_id=row.user_id,
        chat_id=row.chat_id,
        title=row.title,
        created_at=to_utc(row.created_at),
    )


class SqlTelegramRepository:
    """The Telegram configuration of every user; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def bot(self, user_id: int) -> StoredBot | None:
        row = self._session.get(TelegramBotRow, user_id)
        return None if row is None else _bot(row)

    def runnable(self) -> tuple[StoredBot, ...]:
        """The bots of active users, by user id: the ones the supervisor must run."""
        statement = (
            select(TelegramBotRow)
            .join(UserRow, UserRow.id == TelegramBotRow.user_id)
            .where(UserRow.active.is_(True))
            .order_by(TelegramBotRow.user_id)
        )
        return tuple(_bot(row) for row in self._session.execute(statement).scalars())

    def save_bot(
        self, user_id: int, *, ciphertext: str, fingerprint: str, username: str | None
    ) -> StoredBot:
        """Store ``user_id``'s bot, replacing any earlier one.

        A different token is a different bot: the chats linked to the old one and its pending
        link code go, since those chats never talked to the new bot.
        """
        statement = select(TelegramBotRow.user_id).where(
            TelegramBotRow.token_fingerprint == fingerprint
        )
        owner = self._session.execute(statement).scalar_one_or_none()
        if owner is not None and owner != user_id:
            raise DuplicateBotTokenError()
        now = to_utc(self._clock())
        row = self._session.get(TelegramBotRow, user_id)
        if row is None:
            row = TelegramBotRow(
                user_id=user_id,
                token_ciphertext=ciphertext,
                token_fingerprint=fingerprint,
                bot_username=username,
                link_digest=None,
                link_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            if row.token_fingerprint != fingerprint:
                self._delete_chats(user_id)
                row.link_digest = None
                row.link_expires_at = None
            row.token_ciphertext = ciphertext
            row.token_fingerprint = fingerprint
            row.bot_username = username
            row.updated_at = now
        self._session.flush()
        return _bot(row)

    def set_username(self, user_id: int, username: str | None) -> None:
        row = self._session.get(TelegramBotRow, user_id)
        if row is not None and row.bot_username != username:
            row.bot_username = username
            self._session.flush()

    def delete_bot(self, user_id: int) -> bool:
        """Forget ``user_id``'s bot and its chats. ``False`` when there was none."""
        row = self._session.get(TelegramBotRow, user_id)
        if row is None:
            return False
        self._delete_chats(user_id)
        self._session.delete(row)
        self._session.flush()
        return True

    def issue_link(self, user_id: int, digest: str, lifetime: timedelta) -> datetime | None:
        """Replace the pending link code of ``user_id``'s bot; its expiry, or ``None``."""
        row = self._session.get(TelegramBotRow, user_id)
        if row is None:
            return None
        expires = to_utc(self._clock()) + lifetime
        row.link_digest = digest
        row.link_expires_at = expires
        self._session.flush()
        return expires

    def redeem_link(self, user_id: int, digest: str, chat_id: int, title: str | None) -> bool:
        """Link ``chat_id`` when ``digest`` is ``user_id``'s unexpired code; the code is spent."""
        row = self._session.get(TelegramBotRow, user_id)
        if row is None or not digest or row.link_digest != digest:
            return False
        if row.link_expires_at is None or to_utc(row.link_expires_at) <= to_utc(self._clock()):
            return False
        row.link_digest = None
        row.link_expires_at = None
        clipped = None if title is None else title[:MAX_CHAT_TITLE]
        chat = self._session.get(TelegramChatRow, (user_id, chat_id))
        if chat is None:
            self._session.add(
                TelegramChatRow(
                    user_id=user_id,
                    chat_id=chat_id,
                    title=clipped,
                    created_at=to_utc(self._clock()),
                )
            )
        else:
            chat.title = clipped
        self._session.flush()
        return True

    def chats(self, user_id: int) -> tuple[LinkedChat, ...]:
        statement = (
            select(TelegramChatRow)
            .where(TelegramChatRow.user_id == user_id)
            .order_by(TelegramChatRow.created_at, TelegramChatRow.chat_id)
        )
        return tuple(_chat(row) for row in self._session.execute(statement).scalars())

    def link_chat(self, user_id: int, chat_id: int, title: str | None = None) -> None:
        """Link ``chat_id`` without a code: the import from ``secrets.env`` (D7)."""
        if self._session.get(TelegramChatRow, (user_id, chat_id)) is None:
            self._session.add(
                TelegramChatRow(
                    user_id=user_id,
                    chat_id=chat_id,
                    title=title,
                    created_at=to_utc(self._clock()),
                )
            )
            self._session.flush()

    def unlink(self, user_id: int, chat_id: int) -> bool:
        row = self._session.get(TelegramChatRow, (user_id, chat_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def _delete_chats(self, user_id: int) -> None:
        self._session.execute(delete(TelegramChatRow).where(TelegramChatRow.user_id == user_id))

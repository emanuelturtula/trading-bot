"""One Telegram bot per user, all in this process (spec 025, D4-D7).

``BotSupervisor`` keeps one ``TelegramPoller`` and one ``TelegramNotifier`` per user with a bot.
``reload`` reads the bots of active users from the database, decrypts their tokens with the
``SecretBox`` and starts, restarts or stops pollers so the running set matches; the dashboard
calls it after every change. A changed token restarts only that user's bot; a linked or unlinked
chat only changes its allowlist and recipients. The fingerprint is the identity of a token, so
the same token is never polled twice (rule 7).

Without ``TB_MASTER_KEY`` the database bots cannot be read: the supervisor then runs only the bot
of ``TB_TELEGRAM_BOT_TOKEN``, for the first administrator and the chats of
``TB_TELEGRAM_ALLOWED_CHAT_IDS``, exactly as before spec 025 (``run_static``).

Signals reach a user's chats through ``UserRouter``; a user without a bot or without a linked
chat has their signals logged. Nothing here logs a token, a chat id or a bot username: a bot is
named by its owner's user id.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from typing import Final

from pydantic import SecretStr

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.notifications.log_notifier import LogNotifier
from trading_bot.notifications.notifier import SignalNotification
from trading_bot.notifications.telegram import MessageSender, TelegramNotifier
from trading_bot.persistence.database import Database
from trading_bot.persistence.records import StoredBot
from trading_bot.persistence.repositories.telegram import SqlTelegramRepository
from trading_bot.secretbox import SecretBox, UnreadableSecretError
from trading_bot.telegram_bot.poller import MessageHandler, TelegramPoller, UpdateSource

__all__ = [
    "LINK_CODE_ALPHABET",
    "LINK_CODE_LENGTH",
    "LINK_LIFETIME",
    "BotSupervisor",
    "UserRouter",
    "import_env_bot",
    "link_code_digest",
    "new_link_code",
]

LINK_LIFETIME: Final = timedelta(minutes=30)
LINK_CODE_LENGTH: Final = 8
# No 0/O or 1/I/L: the code is typed by hand.
LINK_CODE_ALPHABET: Final = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_LOGGER_NAME: Final = "trading_bot.telegram_bot.supervisor"

SourceBuilder = Callable[[SecretStr], UpdateSource]
SenderBuilder = Callable[[SecretStr], MessageSender]
HandlerBuilder = Callable[[int], MessageHandler]


def new_link_code() -> str:
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


def link_code_digest(code: str) -> str:
    """The stored form of a link code; case and surrounding spaces do not matter."""
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


@dataclass(slots=True)
class _Running:
    fingerprint: str | None  # ``None`` for the bot of ``TB_TELEGRAM_BOT_TOKEN``
    poller: TelegramPoller
    sender: MessageSender
    chats: tuple[int, ...] = field(default=())
    notifier: TelegramNotifier | None = None


class BotSupervisor:
    """Runs every user's bot and knows where each user's signals go."""

    def __init__(
        self,
        *,
        database: Database,
        box: SecretBox | None,
        build_source: SourceBuilder,
        build_sender: SenderBuilder,
        make_handler: HandlerBuilder,
        production: bool,
        display_tz: tzinfo,
        calendar: MarketCalendar,
    ) -> None:
        self._database = database
        self._box = box
        self._build_source = build_source
        self._build_sender = build_sender
        self._make_handler = make_handler
        self._production = production
        self._display_tz = display_tz
        self._calendar = calendar
        self._running: dict[int, _Running] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._logger = logging.getLogger(_LOGGER_NAME)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bots={len(self._running)})"

    @property
    def enabled(self) -> bool:
        """Whether users can store bots: ``TB_MASTER_KEY`` is configured and valid."""
        return self._box is not None

    @property
    def box(self) -> SecretBox | None:
        return self._box

    def running(self, user_id: int) -> bool:
        entry = self._running.get(user_id)
        return entry is not None and entry.poller.running

    def notifier_for(self, user_id: int) -> TelegramNotifier | None:
        entry = self._running.get(user_id)
        return None if entry is None else entry.notifier

    async def validate(self, token: SecretStr) -> str | None:
        """The bot's username when Telegram accepts ``token``; raises ``PollError`` otherwise."""
        source = self._build_source(token)
        try:
            return await source.get_username()
        finally:
            await source.aclose()

    async def run_static(self, user_id: int, token: SecretStr, chats: Sequence[int]) -> None:
        """Run the bot of ``TB_TELEGRAM_BOT_TOKEN`` for ``user_id`` (no ``TB_MASTER_KEY``)."""
        async with self._lock:
            if not self._closed:
                await self._start(user_id, None, token, tuple(chats))

    async def reload(self) -> None:
        """Make the running bots match the database. Never raises an ``Exception``."""
        if self._box is None:
            return
        async with self._lock:
            if self._closed:
                return
            try:
                bots, chats = await asyncio.to_thread(self._read)
            except Exception as error:
                self._logger.error("telegram bots not reloaded: %s", type(error).__name__)
                return
            for user_id in [uid for uid in self._running if uid not in bots]:
                await self._stop(user_id)
            for user_id, bot in bots.items():
                entry = self._running.get(user_id)
                if entry is not None and entry.fingerprint != bot.token_fingerprint:
                    await self._stop(user_id)
                    entry = None
                if entry is None:
                    try:
                        token = self._box.decrypt(bot.token_ciphertext)
                    except UnreadableSecretError:
                        self._logger.error(
                            "the bot of user %d cannot be decrypted; was TB_MASTER_KEY changed?",
                            user_id,
                        )
                        continue
                    await self._start(user_id, bot.token_fingerprint, token, chats[user_id])
                else:
                    self._set_chats(entry, chats[user_id])

    async def refresh_chats(self, user_id: int) -> None:
        """Re-read ``user_id``'s linked chats into the running bot (after ``/link``)."""

        def read() -> tuple[int, ...]:
            with self._database.session() as session:
                return tuple(c.chat_id for c in SqlTelegramRepository(session).chats(user_id))

        chats = await asyncio.to_thread(read)
        entry = self._running.get(user_id)
        if entry is not None:
            self._set_chats(entry, chats)

    async def stop_polling(self) -> None:
        """Stop every poller and keep the senders: signals of a run in flight still go out."""
        async with self._lock:
            self._closed = True
            for user_id, entry in self._running.items():
                await self._close(user_id, "poller", entry.poller.aclose)

    async def aclose(self) -> None:
        """Stop every bot. Idempotent; never raises an ``Exception``."""
        async with self._lock:
            self._closed = True
            for user_id in list(self._running):
                await self._stop(user_id)

    # --- internals ---------------------------------------------------------------------------

    def _read(self) -> tuple[dict[int, StoredBot], Mapping[int, tuple[int, ...]]]:
        with self._database.session() as session:
            repository = SqlTelegramRepository(session)
            bots = {bot.user_id: bot for bot in repository.runnable()}
            chats = {
                user_id: tuple(chat.chat_id for chat in repository.chats(user_id))
                for user_id in bots
            }
        return bots, chats

    async def _start(
        self, user_id: int, fingerprint: str | None, token: SecretStr, chats: tuple[int, ...]
    ) -> None:
        sender = self._build_sender(token)
        try:
            poller = TelegramPoller(
                source=self._build_source(token),
                handler=self._make_handler(user_id),
                allowed_chat_ids=chats,
                linker=self._linker(user_id) if fingerprint is not None else None,
                on_username=self._username_sink(user_id) if fingerprint is not None else None,
                label=f"telegram bot of user {user_id}",
            )
        except BaseException:
            await self._close(user_id, "sender", sender.aclose)
            raise
        entry = _Running(fingerprint=fingerprint, poller=poller, sender=sender)
        self._set_chats(entry, chats)
        self._running[user_id] = entry
        poller.start()
        self._logger.info("started the telegram bot of user %d", user_id)

    async def _stop(self, user_id: int) -> None:
        entry = self._running.pop(user_id)
        if entry.poller.running:
            await self._close(user_id, "poller", entry.poller.aclose)
        await self._close(user_id, "sender", entry.sender.aclose)
        self._logger.info("stopped the telegram bot of user %d", user_id)

    async def _close(self, user_id: int, name: str, close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except Exception as error:
            self._logger.error(
                "closing the telegram %s of user %d failed: %s", name, user_id, type(error).__name__
            )

    def _set_chats(self, entry: _Running, chats: tuple[int, ...]) -> None:
        if entry.notifier is not None and chats == entry.chats:
            return
        entry.chats = chats
        entry.poller.set_allowed(chats)
        entry.notifier = (
            TelegramNotifier(
                sender=entry.sender,
                chat_ids=chats,
                production=self._production,
                display_tz=self._display_tz,
                calendar=self._calendar,
            )
            if chats
            else None
        )

    def _linker(self, user_id: int) -> _Linker:
        return _Linker(self._database, user_id, self.refresh_chats)

    def _username_sink(self, user_id: int) -> _UsernameSink:
        return _UsernameSink(self._database, user_id)


class _Linker:
    """``/link CODE`` on ``user_id``'s bot: spends the code and links the chat (D4)."""

    def __init__(
        self, database: Database, user_id: int, refresh: Callable[[int], Awaitable[None]]
    ) -> None:
        self._database = database
        self._user_id = user_id
        self._refresh = refresh

    async def __call__(self, chat_id: int, code: str, title: str | None) -> str:
        digest = link_code_digest(code)

        def redeem() -> bool:
            with self._database.session() as session:
                repository = SqlTelegramRepository(session)
                return repository.redeem_link(self._user_id, digest, chat_id, title)

        if not await asyncio.to_thread(redeem):
            return "That code is wrong or expired. Get a new one on the dashboard's Telegram page."
        await self._refresh(self._user_id)
        logging.getLogger(_LOGGER_NAME).info("linked a chat to the bot of user %d", self._user_id)
        return "Linked. This chat now gets your signals and can send commands; try /help."


class _UsernameSink:
    """Stores the username Telegram reports, so the dashboard can show it."""

    def __init__(self, database: Database, user_id: int) -> None:
        self._database = database
        self._user_id = user_id

    async def __call__(self, username: str | None) -> None:
        def store() -> None:
            with self._database.session() as session:
                SqlTelegramRepository(session).set_username(self._user_id, username)

        await asyncio.to_thread(store)


def import_env_bot(
    database: Database, box: SecretBox, user_id: int, token: SecretStr, chats: Sequence[int]
) -> bool:
    """Store the bot of ``TB_TELEGRAM_BOT_TOKEN`` and its chats as ``user_id``'s (D7).

    Runs once: ``False``, writing nothing, when ``user_id`` already has a bot or another account
    already uses that token.
    """
    fingerprint = box.fingerprint(token)
    with database.session() as session:
        repository = SqlTelegramRepository(session)
        if repository.bot(user_id) is not None:
            return False
        if any(bot.token_fingerprint == fingerprint for bot in repository.runnable()):
            return False
        repository.save_bot(
            user_id, ciphertext=box.encrypt(token), fingerprint=fingerprint, username=None
        )
        for chat_id in chats:
            repository.link_chat(user_id, chat_id)
    return True


class UserRouter:
    """A ``Notifier`` that sends each signal through its owner's bot, or logs it (D5)."""

    def __init__(self, supervisor: BotSupervisor) -> None:
        self._supervisor = supervisor
        self._log = LogNotifier()

    async def notify(self, notification: SignalNotification) -> None:
        notifier = self._supervisor.notifier_for(notification.owner_id)
        if notifier is None:
            await self._log.notify(notification)
            return
        await notifier.notify(notification)

    async def aclose(self) -> None:
        """The senders belong to the supervisor, which closes them."""

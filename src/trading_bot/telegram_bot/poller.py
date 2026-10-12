"""The ``getUpdates`` loop (spec 018, D1-D3, D5, D6; AC9, AC11-AC13; issue #20).

``TelegramPoller`` long-polls an ``UpdateSource``, drops every message from a chat outside the
allowlist before anything else (D5), hands the text of authorized ones to the
``CommandHandler`` and sends its reply. ``BotUpdateSource`` is the only code of the package that
imports python-telegram-bot; it maps the library's errors onto ``PollError``.

**It never blocks or fails startup** (AC12): ``start`` only creates a task. Telegram being
unreachable, a rejected token or a ``409 Conflict`` (another process polling with the same
token, rule 7) is logged by class name once per streak and retried with capped backoff.

**Offsets.** Telegram re-sends every update until a later ``getUpdates`` call confirms it with
a greater offset. The poller advances its offset before handling a batch, so the next call
confirms it, and ``aclose`` makes one last short call to confirm what was handled: a clean
restart never runs a command twice (AC13). Updates queued while the bot was down are handled
when it starts (Q3).

Like ``notifications/telegram.py``, nothing secret leaves the module: ``Bot.initialize`` is
never called (its rejected-token error embeds the token), errors are raised ``from None`` with
fixed messages, and chat ids, user names and message texts are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from pydantic import SecretStr
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Conflict, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.request import HTTPXRequest

__all__ = [
    "LONG_POLL_SECONDS",
    "BotUpdateSource",
    "IncomingMessage",
    "MessageHandler",
    "PollError",
    "PollErrorKind",
    "TelegramPoller",
    "UpdateSource",
]

LONG_POLL_SECONDS: Final = 30  # D2
_FIRST_BACKOFF: Final = 1.0
_MAX_BACKOFF: Final = 60.0
_CONFLICT_BACKOFF: Final = 30.0  # D3: never hammer the other poller
_STOP_GRACE: Final = 10.0  # how long ``aclose`` lets a command being handled finish
_REQUEST_TIMEOUT: Final = 10.0
_LOGGER_NAME: Final = "trading_bot.telegram_bot.poller"

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One update, reduced to what the poller needs. ``text`` is ``None`` for non-text messages."""

    update_id: int
    chat_id: int | None
    text: str | None


class PollErrorKind(StrEnum):
    TRANSIENT = "transient"  # network, timeout, server error, flood control
    CONFLICT = "conflict"  # 409: another process polls with this token
    REJECTED_TOKEN = "rejected_token"  # noqa: S105 - a failure kind, not a secret  # 401/404: the token is wrong or revoked


class PollError(Exception):
    def __init__(self, kind: PollErrorKind, *, retry_after: float | None = None) -> None:
        super().__init__(f"telegram polling failed: {kind.value}")
        self.kind = kind
        self.retry_after = retry_after


class UpdateSource(Protocol):
    """The Bot API calls the poller makes. Every method raises only ``PollError``."""

    async def get_updates(self, offset: int | None, timeout: int) -> Sequence[IncomingMessage]: ...

    async def send_message(self, chat_id: int, text: str) -> None: ...

    async def get_username(self) -> str | None: ...

    async def aclose(self) -> None: ...


class MessageHandler(Protocol):
    async def handle(self, text: str, *, bot_username: str | None = None) -> str | None: ...


class BotUpdateSource:
    """The python-telegram-bot adapter."""

    def __init__(self, token: SecretStr) -> None:
        if not isinstance(token, SecretStr):
            raise TypeError(f"token must be a SecretStr, got {type(token).__name__}")
        self._request = HTTPXRequest(
            connect_timeout=_REQUEST_TIMEOUT,
            read_timeout=_REQUEST_TIMEOUT,
            write_timeout=_REQUEST_TIMEOUT,
        )
        # A separate connection for the long poll, whose read lasts up to LONG_POLL_SECONDS.
        self._updates_request = HTTPXRequest(
            connect_timeout=_REQUEST_TIMEOUT,
            read_timeout=LONG_POLL_SECONDS + _REQUEST_TIMEOUT,
            write_timeout=_REQUEST_TIMEOUT,
        )
        self._bot = Bot(
            token.get_secret_value(),
            request=self._request,
            get_updates_request=self._updates_request,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def get_updates(self, offset: int | None, timeout: int) -> Sequence[IncomingMessage]:
        updates = await _mapped(
            self._bot.get_updates(
                offset=offset,
                timeout=timeout,
                read_timeout=timeout + _REQUEST_TIMEOUT,
                allowed_updates=["message"],
            )
        )
        messages: list[IncomingMessage] = []
        for update in updates:
            message = update.message
            messages.append(
                IncomingMessage(
                    update_id=update.update_id,
                    chat_id=message.chat.id if message is not None else None,
                    text=message.text if message is not None else None,
                )
            )
        return messages

    async def send_message(self, chat_id: int, text: str) -> None:
        await _mapped(
            self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        )

    async def get_username(self) -> str | None:
        user = await _mapped(self._bot.get_me())
        return user.username

    async def aclose(self) -> None:
        await self._request.shutdown()
        await self._updates_request.shutdown()


async def _mapped[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except Conflict:
        raise PollError(PollErrorKind.CONFLICT) from None
    except (InvalidToken, Forbidden):
        raise PollError(PollErrorKind.REJECTED_TOKEN) from None
    except RetryAfter as error:
        # Always a timedelta internally; the public property warns about its type on v22.
        seconds = error._retry_after.total_seconds()
        raise PollError(PollErrorKind.TRANSIENT, retry_after=seconds) from None
    except TelegramError:  # NetworkError, TimedOut, BadRequest, server errors
        raise PollError(PollErrorKind.TRANSIENT) from None


class TelegramPoller:
    """Polls one source for one process and answers the chats of the allowlist."""

    def __init__(
        self,
        *,
        source: UpdateSource,
        handler: MessageHandler,
        allowed_chat_ids: Collection[int],
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        allowed = frozenset(allowed_chat_ids)
        if not allowed:
            raise ValueError("at least one allowed chat id is required")
        self._source = source
        self._handler = handler
        self._allowed = allowed
        self._sleep = sleep
        self._offset: int | None = None
        self._confirmed: int | None = None
        self._username: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._waiting = (
            False  # True while the task waits on Telegram or a backoff, never mid-command
        )
        self._logger = logging.getLogger(_LOGGER_NAME)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(chats={len(self._allowed)})"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start polling in a task of the running loop. Never performs I/O itself."""
        if self._task is not None:
            raise RuntimeError("the poller is already started")
        self._task = asyncio.get_running_loop().create_task(self._run(), name="telegram-poller")

    async def aclose(self) -> None:
        """Stop polling, let a command being handled finish, then confirm the handled updates."""
        task, self._task = self._task, None
        self._stopping = True
        if task is not None:
            if self._waiting:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), _STOP_GRACE)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._confirm()
        await self._source.aclose()

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            try:
                await self._poll_once()
            except PollError as error:
                failures += 1
                wait = self._backoff(error, failures)
                self._log_failure(error, failures, wait)
                self._waiting = True
                try:
                    await self._sleep(wait)
                finally:
                    self._waiting = False
                continue
            except Exception as error:  # a bug must not kill the poller silently
                failures += 1
                self._logger.error("telegram poller error: %s", type(error).__name__)
                self._waiting = True
                try:
                    await self._sleep(_MAX_BACKOFF)
                finally:
                    self._waiting = False
                continue
            if failures:
                self._logger.info("telegram polling recovered after %d failure(s)", failures)
                failures = 0

    async def _poll_once(self) -> None:
        if self._username is None:
            self._username = await self._source.get_username()
        self._waiting = True
        try:
            messages = await self._source.get_updates(self._offset, LONG_POLL_SECONDS)
        finally:
            self._waiting = False
        self._confirmed = self._offset  # this call confirmed everything before the offset
        if not messages:
            return
        # Advance before handling: the next call confirms the batch, so a command is never run
        # twice by a clean restart (AC13).
        self._offset = max(message.update_id for message in messages) + 1
        for message in sorted(messages, key=lambda item: item.update_id):
            await self._dispatch(message)

    async def _dispatch(self, message: IncomingMessage) -> None:
        if message.chat_id is None or message.chat_id not in self._allowed:
            self._logger.info("ignored a message from a chat outside the allowlist")  # AC9
            return
        if message.text is None:
            return
        reply = await self._handler.handle(message.text, bot_username=self._username)
        if reply is None:
            return
        try:
            await self._source.send_message(message.chat_id, reply)
        except PollError as error:
            self._logger.warning("telegram reply failed: %s", error.kind.value)

    async def _confirm(self) -> None:
        """One short call that confirms the updates handled since the last poll (AC13)."""
        if self._offset is None or self._offset == self._confirmed:
            return
        try:
            await self._source.get_updates(self._offset, 0)
        except PollError as error:
            self._logger.warning("could not confirm the handled updates: %s", error.kind.value)
            return
        self._confirmed = self._offset

    def _backoff(self, error: PollError, failures: int) -> float:
        wait = min(_FIRST_BACKOFF * 2.0 ** (failures - 1), _MAX_BACKOFF)
        if error.retry_after is not None:
            wait = max(wait, min(error.retry_after, _MAX_BACKOFF))
        if error.kind is PollErrorKind.CONFLICT:
            wait = max(wait, _CONFLICT_BACKOFF)
        if error.kind is PollErrorKind.REJECTED_TOKEN:
            wait = _MAX_BACKOFF
        return wait

    def _log_failure(self, error: PollError, failures: int, wait: float) -> None:
        if failures > 1:
            self._logger.debug("telegram polling still failing (%s)", error.kind.value)
            return
        if error.kind is PollErrorKind.CONFLICT:
            self._logger.warning(
                "another process is polling with this bot token (409 Conflict); "
                "retrying every %.0fs. Beta and prod need different tokens.",
                wait,
            )
        elif error.kind is PollErrorKind.REJECTED_TOKEN:
            self._logger.error(
                "telegram rejected the bot token; check TB_TELEGRAM_BOT_TOKEN. "
                "Retrying every %.0fs",
                wait,
            )
        else:
            self._logger.warning("telegram polling failed (%s); retrying", error.kind.value)

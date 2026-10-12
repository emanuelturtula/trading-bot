"""``TelegramNotifier``: one text message per allowed chat (spec 017, AC7 to AC10; issue #17).

The retry policy (decision D3) lives here and is library-free: it talks to a ``MessageSender``
that raises ``TransientSendError`` or ``PermanentSendError``. ``BotMessageSender`` is the only
code that touches python-telegram-bot, and it maps the library's errors onto those two.

**Nothing secret leaves this module.** The token lives in a ``SecretStr`` until the ``Bot`` is
built and only ever appears in the request URL the library builds; the ``httpx`` and ``telegram``
loggers are pinned to ``WARNING`` (``logging_setup``) so that URL is never logged. Chat ids are
never logged: a recipient is named by its position in the list. Exceptions raised from here carry
counts only, never a token, a chat id or Telegram's response text, and are raised ``from None``
so no library exception is chained to them (decision D135 of spec 015).

``Bot.initialize`` is never called: it performs a ``getMe`` and, on a rejected token, raises an
error whose message contains the token. The request object is used directly instead.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta, tzinfo
from typing import Final, Protocol

from pydantic import SecretStr
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.request import HTTPXRequest
from telegram.warnings import PTBDeprecationWarning

from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.notifications.message import format_signal_message
from trading_bot.notifications.notifier import NotificationError, SignalNotification

__all__ = [
    "MAX_ATTEMPTS",
    "BotMessageSender",
    "MessageSender",
    "PermanentSendError",
    "TelegramNotifier",
    "TransientSendError",
]

MAX_ATTEMPTS: Final = 4
_BACKOFF_SECONDS: Final = (1.0, 2.0, 4.0)  # before the 2nd, 3rd and 4th attempt
_MAX_BACKOFF_SECONDS: Final = 30.0
_MAX_RETRY_AFTER_SECONDS: Final = 60.0
_REQUEST_TIMEOUT_SECONDS: Final = 10.0
_LOGGER_NAME: Final = "trading_bot.notifications.telegram"

Sleep = Callable[[float], Awaitable[None]]


class TransientSendError(Exception):
    """A failure worth retrying. ``retry_after`` is the wait Telegram asked for, if any."""

    def __init__(self, *, retry_after: float | None = None) -> None:
        super().__init__("transient send failure")
        self.retry_after = retry_after


class PermanentSendError(Exception):
    """A failure that a retry cannot fix: bad request, blocked bot, rejected token."""

    def __init__(self) -> None:
        super().__init__("permanent send failure")


class MessageSender(Protocol):
    """Sends one HTML message to one chat; raises only the two errors above."""

    async def send_message(self, chat_id: int, text: str) -> None: ...

    async def aclose(self) -> None: ...


class BotMessageSender:
    """The python-telegram-bot adapter: ``sendMessage`` with HTML parse mode."""

    def __init__(self, token: SecretStr) -> None:
        if not isinstance(token, SecretStr):
            raise TypeError(f"token must be a SecretStr, got {type(token).__name__}")
        self._request = HTTPXRequest(
            connect_timeout=_REQUEST_TIMEOUT_SECONDS,
            read_timeout=_REQUEST_TIMEOUT_SECONDS,
            write_timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        self._bot = Bot(token.get_secret_value(), request=self._request)

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def send_message(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except RetryAfter as error:
            with warnings.catch_warnings():  # v22 warns that the type will become timedelta
                warnings.simplefilter("ignore", PTBDeprecationWarning)
                retry_after = _seconds(error.retry_after)
            raise TransientSendError(retry_after=retry_after) from None
        except BadRequest:  # a subclass of NetworkError, but retrying it cannot help
            raise PermanentSendError() from None
        except NetworkError:  # TimedOut included
            raise TransientSendError() from None
        except TelegramError:  # Forbidden, InvalidToken, ChatMigrated, ...
            raise PermanentSendError() from None

    async def aclose(self) -> None:
        await self._request.shutdown()


class TelegramNotifier:
    """Delivers each signal to every allowed chat, retrying transient failures (D3, U1).

    Delivery succeeds when at least one chat received the message; the others are logged by
    position. When none did, ``notify`` raises ``NotificationError``.
    """

    def __init__(
        self,
        *,
        sender: MessageSender,
        chat_ids: Sequence[int],
        production: bool,
        display_tz: tzinfo,
        calendar: MarketCalendar,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        recipients = tuple(chat_ids)
        if not recipients:
            raise ValueError("at least one chat id is required")
        if not all(isinstance(chat_id, int) for chat_id in recipients):
            raise TypeError("chat ids must be integers")
        self._sender = sender
        self._chat_ids = recipients
        self._production = production
        self._display_tz = display_tz
        self._calendar = calendar
        self._sleep = sleep
        self._logger = logging.getLogger(_LOGGER_NAME)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(recipients={len(self._chat_ids)})"

    async def notify(self, notification: SignalNotification) -> None:
        text = format_signal_message(
            notification,
            production=self._production,
            display_tz=self._display_tz,
            calendar=self._calendar,
        )
        failed = 0
        for position, chat_id in enumerate(self._chat_ids, start=1):
            if not await self._deliver(position, chat_id, text):
                failed += 1
        if failed == len(self._chat_ids):
            raise NotificationError(f"telegram delivery failed for all {failed} recipient(s)")
        if failed:
            self._logger.warning(
                "telegram delivery failed for %d of %d recipients", failed, len(self._chat_ids)
            )

    async def aclose(self) -> None:
        await self._sender.aclose()

    async def _deliver(self, position: int, chat_id: int, text: str) -> bool:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await self._sender.send_message(chat_id, text)
            except PermanentSendError:
                self._logger.warning(
                    "telegram recipient %d: permanent failure on attempt %d", position, attempt
                )
                return False
            except TransientSendError as error:
                if attempt == MAX_ATTEMPTS:
                    self._logger.warning(
                        "telegram recipient %d: gave up after %d attempts", position, attempt
                    )
                    return False
                wait = _wait_before(attempt, error.retry_after)
                self._logger.info(
                    "telegram recipient %d: transient failure on attempt %d, retrying in %.0fs",
                    position,
                    attempt,
                    wait,
                )
                await self._sleep(wait)
            else:
                return True
        return False  # unreachable: the loop always returns


def _wait_before(attempt: int, retry_after: float | None) -> float:
    """The wait before attempt ``attempt + 1``: Telegram's own when it gave one (capped)."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), _MAX_RETRY_AFTER_SECONDS)
    return min(_BACKOFF_SECONDS[attempt - 1], _MAX_BACKOFF_SECONDS)


def _seconds(value: int | timedelta) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value)

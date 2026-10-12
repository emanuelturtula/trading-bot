"""``TelegramNotifier``, ``BotMessageSender`` and ``LogNotifier`` (spec 017, AC7, AC9-AC11).

No test reaches the network: the notifier talks to a recording sender, and the adapter's
error mapping is checked by replacing the ``Bot`` call with one that raises the library's own
exceptions. Fake tokens are built at runtime (CLAUDE.md rule 2).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from telegram import Bot
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.runtime import RecordingSender
from tests.fixtures.signal_messages import notification
from trading_bot.logging_setup import QUIET_LOGGERS, configure_logging
from trading_bot.notifications.log_notifier import LogNotifier
from trading_bot.notifications.notifier import NotificationError
from trading_bot.notifications.telegram import (
    MAX_ATTEMPTS,
    BotMessageSender,
    PermanentSendError,
    TelegramNotifier,
    TransientSendError,
)

FAKE_TOKEN = "123456789" + ":" + "x" * 35
CHAT_A = 1000 + 1
CHAT_B = -(10**12) - 7  # a supergroup id


def run_async(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an ``async def`` test body in a fresh loop; fixtures pass through ``functools.wraps``."""

    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


class RecordingSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make_notifier(
    sender: RecordingSender, *chat_ids: int, sleep: RecordingSleep | None = None
) -> TelegramNotifier:
    return TelegramNotifier(
        sender=sender,
        chat_ids=chat_ids or (CHAT_A,),
        production=False,
        display_tz=ZoneInfo("Europe/Madrid"),
        calendar=nyse_test_calendar(),
        sleep=sleep or RecordingSleep(),
    )


@run_async
async def test_every_allowed_chat_receives_the_same_message() -> None:
    sender = RecordingSender()

    await make_notifier(sender, CHAT_A, CHAT_B).notify(notification())

    assert [chat for chat, _ in sender.sent] == [CHAT_A, CHAT_B]
    assert sender.sent[0][1] == sender.sent[1][1]
    assert sender.sent[0][1].startswith("<b>[BETA] 🟢 BUY · AAPL · 1d</b>")


@run_async
async def test_transient_failures_are_retried_with_the_documented_backoff() -> None:
    sender = RecordingSender()
    sender.failures.extend([TransientSendError(), TransientSendError(), TransientSendError()])
    sleep = RecordingSleep()

    await make_notifier(sender, sleep=sleep).notify(notification())

    assert sleep.waits == [1.0, 2.0, 4.0]
    assert len(sender.sent) == 1


@run_async
async def test_retry_after_is_honoured_and_capped() -> None:
    sender = RecordingSender()
    sender.failures.extend(
        [TransientSendError(retry_after=7.0), TransientSendError(retry_after=3600.0)]
    )
    sleep = RecordingSleep()

    await make_notifier(sender, sleep=sleep).notify(notification())

    assert sleep.waits == [7.0, 60.0]


@run_async
async def test_giving_up_raises_a_notification_error_without_sensitive_text() -> None:
    sender = RecordingSender()
    sender.failures.extend(TransientSendError() for _ in range(MAX_ATTEMPTS))

    with pytest.raises(NotificationError) as caught:
        await make_notifier(sender).notify(notification())

    message = str(caught.value)
    assert str(CHAT_A) not in message
    assert caught.value.__cause__ is None
    assert sender.sent == []


@run_async
async def test_a_permanent_failure_is_not_retried() -> None:
    sender = RecordingSender()
    sender.failures.append(PermanentSendError())
    sleep = RecordingSleep()

    with pytest.raises(NotificationError):
        await make_notifier(sender, sleep=sleep).notify(notification())

    assert sleep.waits == []


@run_async
async def test_one_delivered_recipient_is_enough(caplog: pytest.LogCaptureFixture) -> None:
    sender = RecordingSender()
    sender.failures.append(PermanentSendError())  # the first chat fails, the second succeeds

    with caplog.at_level(logging.INFO):
        await make_notifier(sender, CHAT_A, CHAT_B).notify(notification())

    assert [chat for chat, _ in sender.sent] == [CHAT_B]
    assert "failed for 1 of 2 recipients" in caplog.text


@run_async
async def test_no_log_record_names_a_chat_id(caplog: pytest.LogCaptureFixture) -> None:
    sender = RecordingSender()
    sender.failures.extend([TransientSendError(), PermanentSendError()])

    with caplog.at_level(logging.DEBUG), pytest.raises(NotificationError):
        await make_notifier(sender, CHAT_B).notify(notification())

    assert caplog.records
    assert str(CHAT_B) not in caplog.text
    assert str(-CHAT_B) not in caplog.text


def test_the_notifier_needs_at_least_one_integer_chat() -> None:
    with pytest.raises(ValueError, match="at least one"):
        TelegramNotifier(
            sender=RecordingSender(),
            chat_ids=(),
            production=True,
            display_tz=ZoneInfo("UTC"),
            calendar=nyse_test_calendar(),
        )
    with pytest.raises(TypeError):
        TelegramNotifier(
            sender=RecordingSender(),
            chat_ids=("1",),  # type: ignore[list-item]
            production=True,
            display_tz=ZoneInfo("UTC"),
            calendar=nyse_test_calendar(),
        )


def test_repr_names_no_chat_id() -> None:
    assert repr(make_notifier(RecordingSender(), CHAT_A, CHAT_B)) == (
        "TelegramNotifier(recipients=2)"
    )


@run_async
async def test_aclose_closes_the_sender() -> None:
    sender = RecordingSender()

    await make_notifier(sender).aclose()

    assert sender.closed


# --- BotMessageSender: the library's errors, mapped (D2, D3) ---------------------------------


@pytest.mark.parametrize(
    ("raised", "expected", "retry_after"),
    [
        (TimedOut(), TransientSendError, None),
        (NetworkError("Bad Gateway"), TransientSendError, None),
        (RetryAfter(timedelta(seconds=12)), TransientSendError, 12.0),
        (BadRequest("Chat not found"), PermanentSendError, None),
        (Forbidden("bot was blocked by the user"), PermanentSendError, None),
        (InvalidToken("Unauthorized"), PermanentSendError, None),
    ],
)
@run_async
async def test_the_adapter_maps_library_errors(
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected: type[Exception],
    retry_after: float | None,
) -> None:
    sender = BotMessageSender(SecretStr(FAKE_TOKEN))

    async def failing_send(self: Bot, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(Bot, "send_message", failing_send)
    try:
        with pytest.raises(expected) as caught:
            await sender.send_message(CHAT_A, "text")
    finally:
        await sender.aclose()

    assert caught.value.__cause__ is None
    assert FAKE_TOKEN not in str(caught.value)
    if isinstance(caught.value, TransientSendError):
        assert caught.value.retry_after == retry_after


@run_async
async def test_the_adapter_sends_html_without_link_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = BotMessageSender(SecretStr(FAKE_TOKEN))
    calls: list[dict[str, object]] = []

    async def recording_send(self: Bot, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(Bot, "send_message", recording_send)
    await sender.send_message(CHAT_A, "<b>hi</b>")
    await sender.aclose()

    assert calls == [
        {
            "chat_id": CHAT_A,
            "text": "<b>hi</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ]


def test_the_adapter_never_shows_the_token() -> None:
    sender = BotMessageSender(SecretStr(FAKE_TOKEN))

    assert FAKE_TOKEN not in repr(sender)
    with pytest.raises(TypeError):
        BotMessageSender(FAKE_TOKEN)  # type: ignore[arg-type]


def test_request_loggers_never_go_below_warning() -> None:
    """A Bot API URL carries the token: ``httpx`` logs every URL at ``INFO`` (spec 017, §2)."""
    configure_logging("DEBUG", [FAKE_TOKEN])

    for name in QUIET_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


# --- LogNotifier (U2) ------------------------------------------------------------------------


@run_async
async def test_the_log_notifier_logs_one_line_per_signal(caplog: pytest.LogCaptureFixture) -> None:
    notifier = LogNotifier()

    with caplog.at_level(logging.INFO, logger="trading_bot.notifications.log"):
        await notifier.notify(notification())
    await notifier.aclose()

    records = [r for r in caplog.records if r.name == "trading_bot.notifications.log"]
    assert len(records) == 1
    assert "BUY AAPL 1d rule 'RSI bounce'" in records[0].getMessage()

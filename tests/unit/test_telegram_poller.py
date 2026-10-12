"""``TelegramPoller`` and ``BotUpdateSource`` (spec 018, AC9, AC11-AC13, AC15).

The poller is driven by ``FakeUpdateSource`` and a recording sleep, so backoff waits are
asserted without waiting. The adapter's error mapping replaces the ``Bot`` calls with ones that
return or raise the library's own objects: no test reaches the network. Fake tokens are built at
runtime (CLAUDE.md rule 2).
"""

from __future__ import annotations

import ast
import asyncio
import functools
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from telegram import Bot, Chat, Message, Update, User
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

import trading_bot
from tests.fixtures.runtime import FakeUpdateSource
from trading_bot.telegram_bot.poller import (
    LONG_POLL_SECONDS,
    BotUpdateSource,
    IncomingMessage,
    PollError,
    PollErrorKind,
    TelegramPoller,
)

FAKE_TOKEN = "123456789" + ":" + "x" * 35
ALLOWED = 1000 + 1
STRANGER = 4000 + 2
POLLER_LOGGER = "trading_bot.telegram_bot.poller"


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
        await asyncio.sleep(0)


class EchoHandler:
    """Replies ``echo: <text>``; can be made to fail or to block until released."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.failures: list[Exception] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def handle(self, text: str, *, bot_username: str | None = None) -> str | None:
        self.calls.append((text, bot_username))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            raise self.failures.pop(0)
        if text == "/silent":
            return None
        return f"echo: {text}"


def message(
    update_id: int, text: str | None = "/help", chat: int | None = ALLOWED
) -> IncomingMessage:
    return IncomingMessage(update_id=update_id, chat_id=chat, text=text)


def make_poller(
    source: FakeUpdateSource, handler: EchoHandler, sleep: RecordingSleep
) -> TelegramPoller:
    return TelegramPoller(source=source, handler=handler, allowed_chat_ids=[ALLOWED], sleep=sleep)


async def idle(source: FakeUpdateSource) -> None:
    """Wait until the poller consumed everything queued and is long-polling again."""
    for _ in range(1000):
        if not source.batches and source._wakeup is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("the poller never became idle")


async def run_until_idle(
    source: FakeUpdateSource, handler: EchoHandler, sleep: RecordingSleep | None = None
) -> TelegramPoller:
    poller = make_poller(source, handler, sleep or RecordingSleep())
    poller.start()
    await idle(source)
    await poller.aclose()
    return poller


def poller_messages(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == POLLER_LOGGER
    ]


# --- construction ---------------------------------------------------------------------------


def test_an_empty_allowlist_is_rejected() -> None:
    with pytest.raises(ValueError, match="allowed chat id"):
        TelegramPoller(source=FakeUpdateSource(), handler=EchoHandler(), allowed_chat_ids=[])


@run_async
async def test_start_twice_is_an_error_and_start_does_no_io() -> None:
    source = FakeUpdateSource()
    poller = make_poller(source, EchoHandler(), RecordingSleep())

    poller.start()
    assert source.offsets == []  # nothing ran yet: startup is never blocked (AC12)
    assert poller.running
    with pytest.raises(RuntimeError, match="already started"):
        poller.start()
    await poller.aclose()

    assert not poller.running
    assert source.closed


# --- dispatch (AC9) -------------------------------------------------------------------------


@run_async
async def test_authorized_messages_are_answered_in_order() -> None:
    source = FakeUpdateSource()
    source.queue([message(11, "/list"), message(10, "/help")])
    handler = EchoHandler()

    await run_until_idle(source, handler)

    assert handler.calls == [("/help", "trading_test_bot"), ("/list", "trading_test_bot")]
    assert source.sent == [(ALLOWED, "echo: /help"), (ALLOWED, "echo: /list")]


@run_async
async def test_messages_outside_the_allowlist_are_dropped_without_identifying_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    source.queue([message(1, "/pause", chat=STRANGER), message(2, "/pause", chat=None)])
    handler = EchoHandler()

    with caplog.at_level(logging.DEBUG, logger=POLLER_LOGGER):
        await run_until_idle(source, handler)

    assert handler.calls == []
    assert source.sent == []
    assert (
        poller_messages(caplog)
        == [
            (logging.INFO, "ignored a message from a chat outside the allowlist"),
        ]
        * 2
    )
    assert str(STRANGER) not in caplog.text
    assert "/pause" not in caplog.text


@run_async
async def test_non_text_messages_and_empty_replies_send_nothing() -> None:
    source = FakeUpdateSource()
    source.queue([message(1, text=None), message(2, "/silent")])
    handler = EchoHandler()

    await run_until_idle(source, handler)

    assert handler.calls == [("/silent", "trading_test_bot")]
    assert source.sent == []


@run_async
async def test_a_failed_reply_is_logged_and_polling_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    source.send_errors.append(PollError(PollErrorKind.TRANSIENT))
    source.queue([message(1, "/help"), message(2, "/list")])

    with caplog.at_level(logging.WARNING, logger=POLLER_LOGGER):
        await run_until_idle(source, EchoHandler())

    assert source.sent == [(ALLOWED, "echo: /list")]
    assert poller_messages(caplog) == [(logging.WARNING, "telegram reply failed: transient")]


@run_async
async def test_a_handler_bug_does_not_kill_the_poller_nor_rerun_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    handler = EchoHandler()
    handler.failures.append(RuntimeError("boom"))
    source.queue([message(1, "/help")])
    source.queue([message(2, "/list")])
    sleep = RecordingSleep()

    with caplog.at_level(logging.ERROR, logger=POLLER_LOGGER):
        await run_until_idle(source, handler, sleep)

    assert [text for text, _ in handler.calls] == ["/help", "/list"]
    assert source.sent == [(ALLOWED, "echo: /list")]
    assert sleep.waits == [60.0]
    assert poller_messages(caplog) == [(logging.ERROR, "telegram poller error: RuntimeError")]


# --- offsets (AC13) -------------------------------------------------------------------------


@run_async
async def test_each_poll_confirms_the_previous_batch() -> None:
    source = FakeUpdateSource()
    source.queue([message(5), message(6)])
    source.queue([message(7)])

    await run_until_idle(source, EchoHandler())

    assert source.offsets == [
        (None, LONG_POLL_SECONDS),
        (7, LONG_POLL_SECONDS),
        (8, LONG_POLL_SECONDS),
        (8, 0),
    ]  # close cancels the last long poll, which may never have reached Telegram: confirm again


@run_async
async def test_close_lets_the_command_finish_and_confirms_it() -> None:
    source = FakeUpdateSource()
    handler = EchoHandler()
    handler.gate = asyncio.Event()
    source.queue([message(41, "/pause")])
    poller = make_poller(source, handler, RecordingSleep())
    poller.start()
    await handler.entered.wait()

    closing = asyncio.create_task(poller.aclose())
    await asyncio.sleep(0)
    assert not closing.done()  # the command being handled is not cancelled
    handler.gate.set()
    await closing

    assert source.sent == [(ALLOWED, "echo: /pause")]
    assert source.offsets == [(None, LONG_POLL_SECONDS), (42, 0)]
    assert source.closed


@run_async
async def test_a_failed_confirmation_is_logged_and_close_still_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    handler = EchoHandler()
    handler.gate = asyncio.Event()
    source.queue([message(1)])
    poller = make_poller(source, handler, RecordingSleep())
    poller.start()
    await handler.entered.wait()
    source.queue(PollError(PollErrorKind.TRANSIENT))  # answers the confirmation call
    handler.gate.set()

    with caplog.at_level(logging.WARNING, logger=POLLER_LOGGER):
        await poller.aclose()

    assert source.closed
    assert (
        logging.WARNING,
        "could not confirm the handled updates: transient",
    ) in poller_messages(caplog)


# --- failures and backoff (AC12) ------------------------------------------------------------


@run_async
async def test_failures_back_off_and_log_once_per_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    source.username_errors.append(PollError(PollErrorKind.TRANSIENT))
    for item in (
        PollError(PollErrorKind.TRANSIENT),
        PollError(PollErrorKind.TRANSIENT, retry_after=10.0),
        (),
        PollError(PollErrorKind.CONFLICT),
        PollError(PollErrorKind.CONFLICT),
        [message(1)],
    ):
        source.queue(item)
    sleep = RecordingSleep()

    with caplog.at_level(logging.DEBUG, logger=POLLER_LOGGER):
        await run_until_idle(source, EchoHandler(), sleep)

    assert sleep.waits == [1.0, 2.0, 10.0, 30.0, 30.0]
    assert source.sent == [(ALLOWED, "echo: /help")]
    assert poller_messages(caplog) == [
        (logging.WARNING, "telegram polling failed (transient); retrying"),
        (logging.DEBUG, "telegram polling still failing (transient)"),
        (logging.DEBUG, "telegram polling still failing (transient)"),
        (logging.INFO, "telegram polling recovered after 3 failure(s)"),
        (
            logging.WARNING,
            "another process is polling with this bot token (409 Conflict); "
            "retrying every 30s. Beta and prod need different tokens.",
        ),
        (logging.DEBUG, "telegram polling still failing (conflict)"),
        (logging.INFO, "telegram polling recovered after 2 failure(s)"),
    ]


@run_async
async def test_backoff_is_capped(caplog: pytest.LogCaptureFixture) -> None:
    source = FakeUpdateSource()
    for _ in range(8):
        source.queue(PollError(PollErrorKind.TRANSIENT, retry_after=600.0))
    source.queue(())
    for _ in range(8):
        source.queue(PollError(PollErrorKind.TRANSIENT))
    sleep = RecordingSleep()

    await run_until_idle(source, EchoHandler(), sleep)

    assert sleep.waits[:8] == [60.0] * 8
    assert sleep.waits[8:] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


@run_async
async def test_a_rejected_token_waits_a_minute_and_says_what_to_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeUpdateSource()
    source.queue(PollError(PollErrorKind.REJECTED_TOKEN))
    source.queue(PollError(PollErrorKind.REJECTED_TOKEN))
    sleep = RecordingSleep()

    with caplog.at_level(logging.WARNING, logger=POLLER_LOGGER):
        await run_until_idle(source, EchoHandler(), sleep)

    assert sleep.waits == [60.0, 60.0]
    assert poller_messages(caplog) == [
        (
            logging.ERROR,
            "telegram rejected the bot token; check TB_TELEGRAM_BOT_TOKEN. Retrying every 60s",
        )
    ]


@run_async
async def test_close_during_a_backoff_does_not_wait_for_it() -> None:
    source = FakeUpdateSource()
    source.queue(PollError(PollErrorKind.CONFLICT))
    entered = asyncio.Event()

    async def endless_sleep(seconds: float) -> None:
        entered.set()
        await asyncio.Event().wait()

    poller = TelegramPoller(
        source=source, handler=EchoHandler(), allowed_chat_ids=[ALLOWED], sleep=endless_sleep
    )
    poller.start()
    await entered.wait()

    await asyncio.wait_for(poller.aclose(), 1)

    assert source.closed


# --- BotUpdateSource: the library, mapped (D1, D2, AC15) ------------------------------------


def update(update_id: int, chat_id: int, text: str | None) -> Update:
    chat = Chat(id=chat_id, type=Chat.PRIVATE)
    sent = Message(
        message_id=update_id, date=datetime(2026, 3, 2, tzinfo=UTC), chat=chat, text=text
    )
    return Update(update_id=update_id, message=sent)


@run_async
async def test_the_adapter_reduces_updates_and_asks_only_for_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def get_updates(self: Bot, **kwargs: object) -> tuple[Update, ...]:
        calls.append(kwargs)
        return (update(3, ALLOWED, "/help"), update(4, STRANGER, None), Update(update_id=5))

    monkeypatch.setattr(Bot, "get_updates", get_updates)
    source = BotUpdateSource(SecretStr(FAKE_TOKEN))
    try:
        messages = await source.get_updates(3, LONG_POLL_SECONDS)
    finally:
        await source.aclose()

    assert list(messages) == [
        IncomingMessage(update_id=3, chat_id=ALLOWED, text="/help"),
        IncomingMessage(update_id=4, chat_id=STRANGER, text=None),
        IncomingMessage(update_id=5, chat_id=None, text=None),
    ]
    assert calls == [
        {
            "offset": 3,
            "timeout": LONG_POLL_SECONDS,
            "read_timeout": LONG_POLL_SECONDS + 10.0,
            "allowed_updates": ["message"],
        }
    ]


@run_async
async def test_the_adapter_sends_html_and_reads_its_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    async def send_message(self: Bot, **kwargs: object) -> None:
        sent.append(kwargs)

    async def get_me(self: Bot) -> User:
        return User(id=7, first_name="Trading", is_bot=True, username="trading_test_bot")

    monkeypatch.setattr(Bot, "send_message", send_message)
    monkeypatch.setattr(Bot, "get_me", get_me)
    source = BotUpdateSource(SecretStr(FAKE_TOKEN))
    try:
        await source.send_message(ALLOWED, "<b>hi</b>")
        username = await source.get_username()
    finally:
        await source.aclose()

    assert username == "trading_test_bot"
    assert sent == [
        {
            "chat_id": ALLOWED,
            "text": "<b>hi</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ]


@pytest.mark.parametrize(
    ("raised", "kind", "retry_after"),
    [
        (Conflict("terminated by other getUpdates request"), PollErrorKind.CONFLICT, None),
        (InvalidToken("Unauthorized"), PollErrorKind.REJECTED_TOKEN, None),
        (Forbidden("Forbidden"), PollErrorKind.REJECTED_TOKEN, None),
        (RetryAfter(timedelta(seconds=12)), PollErrorKind.TRANSIENT, 12.0),
        (TimedOut(), PollErrorKind.TRANSIENT, None),
        (NetworkError("Bad Gateway"), PollErrorKind.TRANSIENT, None),
        (BadRequest("Bad Request"), PollErrorKind.TRANSIENT, None),
    ],
)
@run_async
async def test_the_adapter_maps_library_errors_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    kind: PollErrorKind,
    retry_after: float | None,
) -> None:
    async def failing(self: Bot, **kwargs: object) -> None:
        raise raised

    for name in ("get_updates", "send_message", "get_me"):
        monkeypatch.setattr(Bot, name, failing)
    source = BotUpdateSource(SecretStr(FAKE_TOKEN))
    calls = (
        lambda: source.get_updates(None, 0),
        lambda: source.send_message(ALLOWED, "x"),
        source.get_username,
    )
    try:
        for call in calls:
            with pytest.raises(PollError) as caught:
                await call()
            assert caught.value.kind is kind
            assert caught.value.retry_after == retry_after
            assert caught.value.__cause__ is None
            assert caught.value.__suppress_context__
            assert FAKE_TOKEN not in str(caught.value)
    finally:
        await source.aclose()


def test_the_adapter_requires_a_secret_and_hides_it() -> None:
    with pytest.raises(TypeError, match="SecretStr"):
        BotUpdateSource(FAKE_TOKEN)  # type: ignore[arg-type]

    assert FAKE_TOKEN not in repr(BotUpdateSource(SecretStr(FAKE_TOKEN)))


# --- AC16: python-telegram-bot stays confined -----------------------------------------------


def _imports_telegram(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name == "telegram" or name.startswith("telegram.") for name in names):
            return True
    return False


def test_python_telegram_bot_is_imported_by_two_modules_only() -> None:
    root = Path(trading_bot.__file__).parent
    importers = {
        path.relative_to(root).as_posix() for path in root.rglob("*.py") if _imports_telegram(path)
    }

    assert importers == {"notifications/telegram.py", "telegram_bot/poller.py"}

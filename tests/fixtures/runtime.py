"""Test doubles for ``Runtime`` (spec 017): no network, no 100-year calendar.

``offline_runtime(settings)`` is a ``Runtime`` built with the cached NYSE test calendar, an
in-memory provider that records ``aclose`` and a recording Telegram sender, so the FastAPI
lifespan can run in a test exactly as in production otherwise.

Always import this module as ``tests.fixtures.runtime``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.fake_provider import FakeMarketDataProvider
from trading_bot.config import Settings
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.runtime import Runtime
from trading_bot.telegram_bot.poller import IncomingMessage, PollError

__all__ = [
    "ClosableFakeProvider",
    "FakeUpdateSource",
    "RecordingSender",
    "offline_runtime",
]


class ClosableFakeProvider(FakeMarketDataProvider):
    """``FakeMarketDataProvider`` plus the ``aclose`` the runtime awaits at shutdown."""

    def __init__(self, *, calendar: MarketCalendar, cache_dir: Path) -> None:
        super().__init__(calendar=calendar)
        self.cache_dir = cache_dir
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class RecordingSender:
    """A ``MessageSender`` that records messages and raises scripted errors, first in first out."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    failures: deque[Exception] = field(default_factory=deque)
    closed: bool = False

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.failures:
            raise self.failures.popleft()
        self.sent.append((chat_id, text))

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeUpdateSource:
    """An ``UpdateSource`` fed by the test: queued batches and errors, first in first out.

    With nothing queued, ``get_updates`` waits like a long poll until it is cancelled or more
    is queued, so a running poller idles instead of spinning.
    """

    batches: deque[Sequence[IncomingMessage] | PollError] = field(default_factory=deque)
    sent: list[tuple[int, str]] = field(default_factory=list)
    offsets: list[tuple[int | None, int]] = field(default_factory=list)
    username: str | None = "trading_test_bot"
    username_errors: deque[PollError] = field(default_factory=deque)
    send_errors: deque[PollError] = field(default_factory=deque)
    closed: bool = False
    _wakeup: asyncio.Event | None = None

    def queue(self, item: Sequence[IncomingMessage] | PollError) -> None:
        self.batches.append(item)
        if self._wakeup is not None:
            self._wakeup.set()

    async def get_updates(self, offset: int | None, timeout: int) -> Sequence[IncomingMessage]:
        self.offsets.append((offset, timeout))
        if not self.batches:
            if timeout == 0:
                return ()
            self._wakeup = asyncio.Event()
            await self._wakeup.wait()
            self._wakeup = None
        item = self.batches.popleft()
        if isinstance(item, PollError):
            raise item
        return item

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.send_errors:
            raise self.send_errors.popleft()
        self.sent.append((chat_id, text))

    async def get_username(self) -> str | None:
        if self.username_errors:
            raise self.username_errors.popleft()
        return self.username

    async def aclose(self) -> None:
        self.closed = True


def offline_runtime(settings: Settings) -> Runtime:
    def build_sender(token: SecretStr) -> RecordingSender:
        return RecordingSender()

    def build_update_source(token: SecretStr) -> FakeUpdateSource:
        return FakeUpdateSource()

    return Runtime(
        settings,
        build_calendar=nyse_test_calendar,
        build_provider=lambda calendar, cache_dir: ClosableFakeProvider(
            calendar=calendar, cache_dir=cache_dir
        ),
        build_sender=build_sender,
        build_update_source=build_update_source,
    )

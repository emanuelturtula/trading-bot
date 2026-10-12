"""Test doubles for ``Runtime`` (spec 017): no network, no 100-year calendar.

``offline_runtime(settings)`` is a ``Runtime`` built with the cached NYSE test calendar, an
in-memory provider that records ``aclose`` and a recording Telegram sender, so the FastAPI
lifespan can run in a test exactly as in production otherwise.

Always import this module as ``tests.fixtures.runtime``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.fake_provider import FakeMarketDataProvider
from trading_bot.config import Settings
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.runtime import Runtime

__all__ = ["ClosableFakeProvider", "RecordingSender", "offline_runtime"]


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


def offline_runtime(settings: Settings) -> Runtime:
    def build_sender(token: SecretStr) -> RecordingSender:
        return RecordingSender()

    return Runtime(
        settings,
        build_calendar=nyse_test_calendar,
        build_provider=lambda calendar, cache_dir: ClosableFakeProvider(
            calendar=calendar, cache_dir=cache_dir
        ),
        build_sender=build_sender,
    )

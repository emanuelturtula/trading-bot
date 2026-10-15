"""``Runtime`` and the FastAPI lifespan (spec 017, AC1-AC6, AC11).

The runtime runs for real (database, scheduler, engine) with the cached NYSE test calendar, an
in-memory provider and a recording Telegram sender: no network and no 100-year calendar. The
scheduler's catch-up job may fire during a test; with no ticker configured it is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.runtime import (
    ClosableFakeProvider,
    FakeUpdateSource,
    RecordingSender,
    offline_runtime,
)
from trading_bot import runtime as runtime_module
from trading_bot.config import Settings
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.instance_lock import LOCK_FILENAME, InstanceLock, InstanceLockError
from trading_bot.main import create_app
from trading_bot.notifications.telegram import TelegramNotifier
from trading_bot.persistence.owner import default_owner_id
from trading_bot.persistence.repositories.telegram import SqlTelegramRepository
from trading_bot.runtime import Runtime
from trading_bot.scheduler.service import SchedulerService
from trading_bot.secretbox import new_master_key
from trading_bot.telegram_bot.supervisor import UserRouter

FAKE_TOKEN = "123456789" + ":" + "x" * 35


def settings_for(data_dir: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, data_dir=data_dir, **overrides)  # type: ignore[arg-type]


def owner_of(runtime: Runtime) -> int:
    with runtime.database.session() as session:
        return default_owner_id(session)


class Recorded:
    """A runtime whose provider and sender record when they are closed."""

    def __init__(self, events: list[str], *, provider_fails: bool = False) -> None:
        self.events = events
        self.provider_fails = provider_fails
        self.providers: list[ClosableFakeProvider] = []
        self.senders: list[RecordingSender] = []

    def runtime(self, settings: Settings) -> Runtime:
        events = self.events
        fails = self.provider_fails

        class Provider(ClosableFakeProvider):
            async def aclose(self) -> None:
                events.append("provider")
                if fails:
                    raise OSError("boom")
                await super().aclose()

        class Sender(RecordingSender):
            async def aclose(self) -> None:
                events.append("notifier")
                await super().aclose()

        class Source(FakeUpdateSource):
            async def aclose(self) -> None:
                events.append("poller")
                await super().aclose()

        def build_provider(calendar: MarketCalendar, cache_dir: Path) -> Provider:
            provider = Provider(calendar=calendar, cache_dir=cache_dir)
            self.providers.append(provider)
            return provider

        def build_sender(token: object) -> Sender:
            sender = Sender()
            self.senders.append(sender)
            return sender

        return Runtime(
            settings,
            build_calendar=nyse_test_calendar,
            build_provider=build_provider,
            build_sender=build_sender,
            build_update_source=lambda token: Source(),
        )


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the scheduler and lock shutdown steps next to the provider's and the sender's."""
    recorded: list[str] = []
    original_close = SchedulerService.aclose
    original_release = InstanceLock.release

    async def aclose(self: SchedulerService) -> None:
        recorded.append("scheduler")
        await original_close(self)

    def release(self: InstanceLock) -> None:
        if self.held:
            recorded.append("lock")
        original_release(self)

    monkeypatch.setattr(SchedulerService, "aclose", aclose)
    monkeypatch.setattr(InstanceLock, "release", release)
    return recorded


def test_start_builds_everything_and_close_undoes_it_in_reverse(
    tmp_path: Path, events: list[str]
) -> None:
    recorded = Recorded(events)
    settings = settings_for(tmp_path, telegram_bot_token=FAKE_TOKEN, telegram_allowed_chat_ids="5")

    async def scenario() -> None:
        runtime = recorded.runtime(settings)
        await runtime.start()
        assert runtime.scheduler.running
        assert isinstance(runtime.notifier, UserRouter)
        owner = owner_of(runtime)
        assert runtime.bots.running(owner)  # no TB_MASTER_KEY: the bot of the environment
        assert isinstance(runtime.bots.notifier_for(owner), TelegramNotifier)
        assert (tmp_path / LOCK_FILENAME).exists()
        cache_dir = recorded.providers[0].cache_dir
        assert cache_dir.is_dir()
        await runtime.aclose()
        await runtime.aclose()  # idempotent
        assert not cache_dir.exists()
        with pytest.raises(RuntimeError, match="not started"):
            _ = runtime.database

    asyncio.run(scenario())

    assert events == ["poller", "scheduler", "notifier", "provider", "lock"]
    assert recorded.providers[0].closed
    assert recorded.senders[0].closed


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"telegram_bot_token": FAKE_TOKEN},  # token but no chat
        {"telegram_allowed_chat_ids": "5"},  # chat but no token
    ],
)
def test_without_telegram_configured_signals_are_only_logged(
    tmp_path: Path, overrides: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        runtime = offline_runtime(settings_for(tmp_path, **overrides))
        await runtime.start()
        try:
            assert isinstance(runtime.notifier, UserRouter)
            owner = owner_of(runtime)
            assert not runtime.bots.running(owner)  # no token or no chat: nobody to answer
            assert runtime.bots.notifier_for(owner) is None
        finally:
            await runtime.aclose()

    with caplog.at_level(logging.WARNING, logger="trading_bot.runtime"):
        asyncio.run(scenario())

    assert "telegram is not configured" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_a_failing_shutdown_step_does_not_skip_the_others(
    tmp_path: Path, events: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    recorded = Recorded(events, provider_fails=True)

    async def scenario() -> None:
        runtime = recorded.runtime(settings_for(tmp_path))
        await runtime.start()
        await runtime.aclose()

    with caplog.at_level(logging.ERROR, logger="trading_bot.runtime"):
        asyncio.run(scenario())

    assert events == ["scheduler", "provider", "lock"]
    assert "shutdown of the provider failed: OSError" in caplog.text
    assert "boom" not in caplog.text


def test_a_failed_startup_closes_what_was_built_and_propagates(
    tmp_path: Path, events: list[str]
) -> None:
    def broken_sender(token: object) -> RecordingSender:
        raise RuntimeError("sender failed")

    async def scenario() -> None:
        runtime = Runtime(
            settings_for(tmp_path, telegram_bot_token=FAKE_TOKEN, telegram_allowed_chat_ids="5"),
            build_calendar=nyse_test_calendar,
            build_provider=lambda calendar, cache_dir: ClosableFakeProvider(
                calendar=calendar, cache_dir=cache_dir
            ),
            build_sender=broken_sender,
        )
        with pytest.raises(RuntimeError, match="sender failed"):
            await runtime.start()
        # Everything was released: a new runtime starts on the same directory.
        again = offline_runtime(settings_for(tmp_path))
        await again.start()
        await again.aclose()

    asyncio.run(scenario())

    assert events[:2] == ["scheduler", "lock"]


def test_a_second_runtime_on_the_same_directory_refuses_to_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        first = offline_runtime(settings_for(tmp_path))
        await first.start()
        try:
            with pytest.raises(InstanceLockError):
                await offline_runtime(settings_for(tmp_path)).start()
            assert first.scheduler.running  # the first one is untouched
        finally:
            await first.aclose()

    with caplog.at_level(logging.ERROR, logger="trading_bot.runtime"):
        asyncio.run(scenario())

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert LOCK_FILENAME in errors[0]


def test_the_close_delay_setting_reaches_the_schedule(tmp_path: Path) -> None:
    async def fire_times(delay: int) -> dict[str, datetime | None]:
        runtime = offline_runtime(settings_for(tmp_path, candle_close_delay_seconds=delay))
        await runtime.start()
        try:
            return {tf.value: when for tf, when in runtime.scheduler.next_fire_times()}
        finally:
            await runtime.aclose()

    async def scenario() -> tuple[dict[str, datetime | None], dict[str, datetime | None]]:
        return await fire_times(0), await fire_times(300)

    delay = timedelta(seconds=300)
    started = datetime.now(UTC)
    immediate, delayed = asyncio.run(scenario())
    finished = datetime.now(UTC)

    for timeframe, when in immediate.items():
        fire = delayed[timeframe]
        assert when is not None
        assert fire is not None
        close = fire - delay
        if close != when:
            # A candle closed less than the delay ago: its delayed fire is still ahead, while
            # the immediate schedule already moved on to the next close.
            assert started - delay <= close <= finished
            assert close < when


def test_the_lifespan_serves_health_with_the_bot_running(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path, environment="beta"), build_runtime=offline_runtime)

    with TestClient(app) as client:
        assert app.state.runtime.scheduler.running
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_open_database_failure_aborts_startup_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_open(data_dir: Path) -> object:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(runtime_module, "open_database", failing_open)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="migration failed"):
            await offline_runtime(settings_for(tmp_path)).start()

    asyncio.run(scenario())

    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()


# --- spec 025: the bots stored in the database ------------------------------------------------


def test_without_a_master_key_users_cannot_store_bots(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        runtime = offline_runtime(settings_for(tmp_path))
        await runtime.start()
        try:
            assert not runtime.bots.enabled
        finally:
            await runtime.aclose()

    with caplog.at_level(logging.WARNING, logger="trading_bot.runtime"):
        asyncio.run(scenario())

    assert "TB_MASTER_KEY is not set" in caplog.text


def test_an_invalid_master_key_is_reported_without_its_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    invalid = "not-a-fernet-key-" + "q" * 10

    async def scenario() -> None:
        runtime = offline_runtime(settings_for(tmp_path, master_key=invalid))
        await runtime.start()
        try:
            assert not runtime.bots.enabled
        finally:
            await runtime.aclose()

    with caplog.at_level(logging.ERROR, logger="trading_bot.runtime"):
        asyncio.run(scenario())

    assert "TB_MASTER_KEY is not a valid key" in caplog.text
    assert invalid not in caplog.text


def test_with_a_master_key_the_environment_bot_is_imported_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(
        tmp_path,
        master_key=new_master_key(),
        telegram_bot_token=FAKE_TOKEN,
        telegram_allowed_chat_ids="5,7",
    )

    async def run_once() -> None:
        runtime = offline_runtime(settings)
        await runtime.start()
        try:
            owner = owner_of(runtime)
            assert runtime.bots.enabled
            assert runtime.bots.running(owner)
            with runtime.database.session() as session:
                repository = SqlTelegramRepository(session)
                stored = repository.bot(owner)
                chats = [chat.chat_id for chat in repository.chats(owner)]
            assert stored is not None
            assert FAKE_TOKEN not in stored.token_ciphertext
            assert chats == [5, 7]
        finally:
            await runtime.aclose()

    with caplog.at_level(logging.INFO, logger="trading_bot.runtime"):
        asyncio.run(run_once())
        asyncio.run(run_once())

    assert caplog.text.count("imported the bot of TB_TELEGRAM_BOT_TOKEN") == 1
    assert "TB_TELEGRAM_BOT_TOKEN is ignored" in caplog.text
    assert FAKE_TOKEN not in caplog.text

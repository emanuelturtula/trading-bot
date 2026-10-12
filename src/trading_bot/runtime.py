"""The running bot: every long-lived component, built once and closed in reverse (spec 017).

``Runtime.start`` builds, inside the running loop and in this order (AC1): the instance lock, the
``Database`` (migrations included), the NYSE ``MarketCalendar``, the Yahoo provider, the
notifier, the ``SignalEngine``, the ``TimeframeRunner`` and one ``SchedulerService``, then starts
the scheduler. ``Runtime.aclose`` undoes it in reverse order (AC2); each step is isolated, so a
failure is logged by class name and the following steps still run.

A failure while starting closes whatever was already built and propagates (AC3): the FastAPI
lifespan never yields, uvicorn exits non-zero and the deploy rolls back.

When Telegram is configured, the command poller (spec 018) starts last and stops first.

The builders of the calendar, the provider, the notifier's sender and the poller's update source
are injectable so tests run without the network and without the 100-year calendar.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Final, Protocol

from pydantic import SecretStr

from trading_bot.config import Settings
from trading_bot.data.provider import MarketDataProvider
from trading_bot.data.yahoo.factory import build_yfinance_provider
from trading_bot.domain.market_calendar.nyse import (
    NYSE_FIRST_SUPPORTED_DAY,
    NYSE_LAST_SUPPORTED_DAY,
    build_nyse_calendar,
)
from trading_bot.domain.market_calendar.sessions import MarketCalendar
from trading_bot.engine.signal_engine import SignalEngine
from trading_bot.engine.sql_unit_of_work import sql_unit_of_work
from trading_bot.instance_lock import InstanceLock, InstanceLockError
from trading_bot.notifications.log_notifier import LogNotifier
from trading_bot.notifications.notifier import SignalNotification
from trading_bot.notifications.routing import OwnerRouter
from trading_bot.notifications.telegram import BotMessageSender, MessageSender, TelegramNotifier
from trading_bot.persistence.database import Database, open_database
from trading_bot.persistence.owner import default_owner_id
from trading_bot.scheduler.policy import SchedulerPolicy
from trading_bot.scheduler.runner import TimeframeRunner
from trading_bot.scheduler.service import SchedulerService
from trading_bot.scheduler.slots import skip_unpublished_hours
from trading_bot.telegram_bot.commands import CommandHandler
from trading_bot.telegram_bot.poller import BotUpdateSource, TelegramPoller, UpdateSource
from trading_bot.telegram_bot.unit_of_work import sql_command_unit_of_work

__all__ = ["ClosableProvider", "Runtime", "RuntimeNotifier"]

_LOGGER_NAME: Final = "trading_bot.runtime"
# Decision D7: how much longer to wait for a run still in flight after the scheduler's own drain.
EXTRA_DRAIN: Final = timedelta(seconds=10)


class ClosableProvider(MarketDataProvider, Protocol):
    async def aclose(self) -> None: ...


class RuntimeNotifier(Protocol):
    async def notify(self, notification: SignalNotification) -> None: ...

    async def aclose(self) -> None: ...


CalendarBuilder = Callable[[], MarketCalendar]
ProviderBuilder = Callable[[MarketCalendar, Path], ClosableProvider]
SenderBuilder = Callable[[SecretStr], MessageSender]
UpdateSourceBuilder = Callable[[SecretStr], UpdateSource]


def _build_calendar() -> MarketCalendar:
    return build_nyse_calendar(NYSE_FIRST_SUPPORTED_DAY, NYSE_LAST_SUPPORTED_DAY)


def _build_provider(calendar: MarketCalendar, cache_dir: Path) -> ClosableProvider:
    return build_yfinance_provider(calendar=calendar, cache_dir=cache_dir)


class Runtime:
    """Owns the lock, the database, the provider, the notifier and the scheduler."""

    def __init__(
        self,
        settings: Settings,
        *,
        build_calendar: CalendarBuilder = _build_calendar,
        build_provider: ProviderBuilder = _build_provider,
        build_sender: SenderBuilder = BotMessageSender,
        build_update_source: UpdateSourceBuilder = BotUpdateSource,
    ) -> None:
        self._settings = settings
        self._build_calendar = build_calendar
        self._build_provider = build_provider
        self._build_sender = build_sender
        self._build_update_source = build_update_source
        self._stack: AsyncExitStack | None = None
        self._database: Database | None = None
        self._notifier: RuntimeNotifier | None = None
        self._provider: ClosableProvider | None = None
        self._service: SchedulerService | None = None
        self._poller: TelegramPoller | None = None
        self._logger = logging.getLogger(_LOGGER_NAME)

    @property
    def database(self) -> Database:
        if self._database is None:
            raise RuntimeError("the runtime is not started")
        return self._database

    @property
    def provider(self) -> MarketDataProvider:
        if self._provider is None:
            raise RuntimeError("the runtime is not started")
        return self._provider

    @property
    def notifier(self) -> RuntimeNotifier:
        if self._notifier is None:
            raise RuntimeError("the runtime is not started")
        return self._notifier

    @property
    def scheduler(self) -> SchedulerService:
        if self._service is None:
            raise RuntimeError("the runtime is not started")
        return self._service

    @property
    def poller(self) -> TelegramPoller | None:
        """The command poller, or ``None`` when Telegram is not configured."""
        return self._poller

    async def start(self) -> None:
        if self._stack is not None:
            raise RuntimeError("the runtime is already started")
        stack = AsyncExitStack()
        self._stack = stack
        try:
            await self._start(stack)
        except InstanceLockError as error:
            self._logger.error("startup failed: %s", error)  # names the lock file, nothing else
            await self.aclose()
            raise
        except BaseException as error:
            self._logger.error("startup failed: %s", type(error).__name__)
            await self.aclose()
            raise

    async def aclose(self) -> None:
        """Close everything in reverse order. Idempotent; never raises an ``Exception``."""
        stack, self._stack = self._stack, None
        if stack is None:
            return
        await stack.aclose()
        self._database = None
        self._notifier = None
        self._provider = None
        self._service = None
        self._poller = None

    async def _start(self, stack: AsyncExitStack) -> None:
        settings = self._settings
        # Validated first: a bad configuration must fail before anything is opened.
        policy = SchedulerPolicy(
            close_delay=timedelta(seconds=settings.candle_close_delay_seconds),
            misfire_grace=timedelta(seconds=settings.scheduler_misfire_grace_seconds),
        )

        lock = InstanceLock(settings.data_dir)
        lock.acquire()
        self._push(stack, "instance lock", _sync(lock.release))

        database = open_database(settings.data_dir)
        self._push(stack, "database", _sync(database.dispose))
        self._database = database

        calendar = await asyncio.to_thread(self._build_calendar)

        cache_dir = Path(tempfile.mkdtemp(prefix="tb-yf-"))
        self._push(stack, "provider cache", _sync(lambda: shutil.rmtree(cache_dir, True)))
        provider = self._build_provider(calendar, cache_dir)
        self._push(stack, "provider", provider.aclose)
        self._provider = provider

        notifier = self._make_notifier(calendar)
        self._push(stack, "notifier", notifier.aclose)
        self._notifier = notifier

        should_run = skip_unpublished_hours(calendar)
        unit_of_work = sql_unit_of_work(database)
        with database.session() as session:
            owner_id = default_owner_id(session)
        engine = SignalEngine(
            provider=provider, unit_of_work=unit_of_work, notifier=OwnerRouter(notifier, owner_id)
        )
        runner = TimeframeRunner(
            engine=engine,
            calendar=calendar,
            unit_of_work=unit_of_work,
            policy=policy,
            should_run=should_run,
        )
        service = SchedulerService(
            runner=runner, calendar=calendar, policy=policy, should_run=should_run
        )

        async def stop_scheduler() -> None:
            await service.aclose()
            if runner.in_flight and not await runner.drain(EXTRA_DRAIN):  # D7
                self._logger.warning(
                    "shutdown: a run was still in flight %ds after the scheduler stopped",
                    int(EXTRA_DRAIN.total_seconds()),
                )

        self._push(stack, "scheduler", stop_scheduler)
        service.start()
        self._service = service

        token = settings.telegram_bot_token
        if isinstance(notifier, TelegramNotifier) and token is not None:
            handler = CommandHandler(
                unit_of_work=sql_command_unit_of_work(database),
                provider=provider,
                next_fire_times=service.next_fire_times,
                production=settings.environment == "prod",
                environment=settings.environment,
                version=settings.version,
                display_tz=settings.display_zone,
            )
            poller = TelegramPoller(
                source=self._build_update_source(token),
                handler=handler,
                allowed_chat_ids=settings.telegram_chat_ids,
            )
            self._push(stack, "telegram poller", poller.aclose)
            poller.start()
            self._poller = poller
        self._logger.info(
            "started: environment %s, %s notifier", settings.environment, _kind(notifier)
        )

    def _make_notifier(self, calendar: MarketCalendar) -> RuntimeNotifier:
        settings = self._settings
        token = settings.telegram_bot_token
        chat_ids = settings.telegram_chat_ids
        if token is None or not token.get_secret_value() or not chat_ids:
            self._logger.warning(
                "telegram is not configured (token or chat ids missing): signals are only logged"
            )
            return LogNotifier()
        return TelegramNotifier(
            sender=self._build_sender(token),
            chat_ids=chat_ids,
            production=settings.environment == "prod",
            display_tz=settings.display_zone,
            calendar=calendar,
        )

    def _push(self, stack: AsyncExitStack, name: str, close: Callable[[], Awaitable[None]]) -> None:
        """Register ``close`` so that a failure is logged by class name and never propagates."""

        async def guarded() -> None:
            try:
                await close()
            except Exception as error:
                self._logger.error("shutdown of the %s failed: %s", name, type(error).__name__)

        stack.push_async_callback(guarded)


def _sync(function: Callable[[], object]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        function()

    return run


def _kind(notifier: RuntimeNotifier) -> str:
    return "telegram" if isinstance(notifier, TelegramNotifier) else "log"

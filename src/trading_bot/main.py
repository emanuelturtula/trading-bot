"""FastAPI application factory.

Run with a single worker (the bot will own the scheduler and the Telegram poller):
``uvicorn trading_bot.main:create_app --factory --workers 1``
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from trading_bot.config import Environment, Settings, get_settings
from trading_bot.logging_setup import configure_logging
from trading_bot.runtime import Runtime


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: Environment


def create_app(
    settings: Settings | None = None,
    *,
    build_runtime: Callable[[Settings], Runtime] = Runtime,  # tests inject fakes
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start the bot (spec 017): lock, database, calendar, provider, notifier, scheduler.

        A failure propagates: the lifespan never yields, uvicorn exits non-zero, the container
        never becomes healthy and ``deploy/deploy.py`` restores the previous deployment.
        """
        runtime = build_runtime(settings)
        await runtime.start()
        app.state.runtime = runtime
        app.state.database = runtime.database
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="trading-bot",
        version=settings.version,
        docs_url=None if settings.environment == "prod" else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok", version=settings.version, environment=settings.environment
        )

    return app

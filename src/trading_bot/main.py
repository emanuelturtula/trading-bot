"""FastAPI application factory.

Run with a single worker (the bot will own the scheduler and the Telegram poller):
``uvicorn trading_bot.main:create_app --factory --workers 1``
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from trading_bot.config import Environment, Settings, get_settings
from trading_bot.logging_setup import configure_logging
from trading_bot.persistence.database import open_database


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: Environment


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the database, migrations included, before anything else starts.

        A failure propagates: the lifespan never yields, uvicorn exits non-zero, the container
        never becomes healthy and ``deploy/deploy.py`` restores the previous deployment. The
        migration is fast and nothing else is scheduled yet, so it needs no worker thread.
        """
        database = open_database(settings.data_dir)
        app.state.database = database
        try:
            yield
        finally:
            database.dispose()

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

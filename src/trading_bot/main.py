"""FastAPI application factory.

Run with a single worker (the bot will own the scheduler and the Telegram poller):
``uvicorn trading_bot.main:create_app --factory --workers 1``
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from trading_bot.config import Environment, Settings, get_settings
from trading_bot.logging_setup import configure_logging


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: Environment


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    app = FastAPI(
        title="trading-bot",
        version=settings.version,
        docs_url=None if settings.environment == "prod" else "/docs",
        redoc_url=None,
    )

    @app.get("/health")
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok", version=settings.version, environment=settings.environment
        )

    return app

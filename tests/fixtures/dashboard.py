"""A dashboard application and a browser driving it (specs 020 and 023).

Shared by the dashboard test modules, which must not import each other.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fixtures.runtime import ClosableFakeProvider, offline_runtime
from trading_bot.config import Settings
from trading_bot.dashboard.auth import hash_password
from trading_bot.dashboard.unit_of_work import DashboardRepositories, sql_dashboard_unit_of_work
from trading_bot.main import create_app

# What ``TestClient`` returns: an ``httpx2`` response, whatever the installed ``httpx``.
type Response = Any

__all__ = ["ADMIN", "CSRF", "HASH", "PASSWORD", "SECRET", "Browser", "make_app"]

# Built at runtime (CLAUDE.md rule 2): no secret-shaped literal in the repository.
PASSWORD = "-".join(["correct", "horse", "battery", "staple"])
SECRET = "k" * 40
HASH = hash_password(PASSWORD)
ADMIN = "admin"
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def make_app(data_dir: Path, **overrides: object) -> FastAPI:
    values: dict[str, object] = {
        "data_dir": data_dir,
        "environment": "prod",
        "dashboard_password_hash": HASH,
        "session_secret": SECRET,
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type, call-arg]
    return create_app(settings, build_runtime=offline_runtime)


class Browser:
    """A ``TestClient`` with the helpers a signed-in user needs."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    @property
    def app(self) -> FastAPI:
        app = self.client.app
        assert isinstance(app, FastAPI)
        return app

    @property
    def provider(self) -> ClosableFakeProvider:
        provider = self.app.state.runtime.provider
        assert isinstance(provider, ClosableFakeProvider)
        return provider

    def login(self, password: str = PASSWORD, username: str = ADMIN) -> Response:
        return self.client.post(
            "/dashboard/login", data={"username": username, "password": password}
        )

    def get(self, path: str) -> Response:
        return self.client.get(path)

    def csrf(self) -> str:
        match = CSRF.search(self.client.get("/dashboard/").text)
        assert match is not None
        return match.group(1)

    def post(self, path: str, data: dict[str, str | list[str]] | None = None) -> Response:
        payload: dict[str, str | list[str]] = {"csrf": self.csrf()}
        payload.update(data or {})
        return self.client.post(path, data=payload)

    def follow(self, response: Response) -> Response:
        assert response.status_code == 303, response.text
        return self.client.get(response.headers["location"])

    def repositories[T](self, work: Callable[[DashboardRepositories], T]) -> T:
        unit_of_work = sql_dashboard_unit_of_work(self.app.state.runtime.database)
        with unit_of_work() as repositories:
            return work(repositories)

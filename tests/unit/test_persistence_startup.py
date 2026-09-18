"""Startup wiring, ``/health`` and empty or migrated volumes (spec 012, T9, T10, AC14-AC18)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

from tests.fixtures.database import temporary_database
from trading_bot import main as main_module
from trading_bot.config import Settings
from trading_bot.main import create_app
from trading_bot.persistence.database import Database
from trading_bot.persistence.engine import DATABASE_FILENAME, database_path
from trading_bot.persistence.migrator import current_revision, head_revision

MIGRATOR_LOGGER = "trading_bot.persistence.migrator"


def make_app(data_dir: Path) -> FastAPI:
    settings = Settings(_env_file=None, data_dir=data_dir)  # type: ignore[call-arg]
    return create_app(settings)


def stored_revisions(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        return [str(value) for value in rows.scalars().all()]


def migrator_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == MIGRATOR_LOGGER]


@contextmanager
def captured_logs(caplog: pytest.LogCaptureFixture, level: int) -> Iterator[None]:
    """Capture every logger after ``create_app`` replaced the root handlers.

    ``configure_logging`` installs the application handler and drops the others, pytest's
    included, so the capturing handler is re-attached once the app has been built. Records
    emitted while preparing the test are dropped: only the startup under test is examined.
    """
    root = logging.getLogger()
    with caplog.at_level(level):
        caplog.clear()
        root.addHandler(caplog.handler)
        try:
            yield
        finally:
            root.removeHandler(caplog.handler)


def sidecar_files(data_dir: Path) -> list[str]:
    return sorted(path.name for path in data_dir.iterdir())


# --- T9: lifespan and /health (AC14, AC17, AC18) -------------------------------------------


def test_building_the_app_opens_no_database(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    assert not database_path(tmp_path).exists()
    assert not hasattr(app.state, "database")


def test_a_client_without_the_context_manager_opens_no_database(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path))

    assert client.get("/health").status_code == 200
    assert not database_path(tmp_path).exists()


def test_the_lifespan_stores_a_migrated_database_and_disposes_it(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    disposed: list[Engine] = []

    with TestClient(app):
        database = app.state.database
        assert isinstance(database, Database)
        event.listen(database.engine, "engine_disposed", disposed.append)
        assert current_revision(database.engine) == head_revision()
        assert disposed == []

    assert len(disposed) == 1
    assert sidecar_files(tmp_path) == [DATABASE_FILENAME]  # the WAL was checkpointed away


def test_health_is_unchanged_and_touches_no_connection(tmp_path: Path) -> None:
    """``/health`` answers without reaching the database at all (AC17, decision D79).

    Statements are recorded, not new connections: the pool keeps the connection the migration
    opened, so a probe reusing it would open none. The pool checkout is recorded too, which
    also catches a probe that takes a raw DBAPI connection.
    """
    app = make_app(tmp_path)
    statements: list[str] = []
    checkouts: list[object] = []

    def record_statement(conn: object, cursor: object, statement: str, *rest: object) -> None:
        statements.append(statement)

    with TestClient(app) as client:
        engine = app.state.database.engine
        event.listen(engine, "before_cursor_execute", record_statement)
        event.listen(engine.pool, "checkout", lambda *args: checkouts.append(args))

        response = client.get("/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "version", "environment"}
    assert statements == []
    assert checkouts == []


def test_health_never_names_the_data_directory_or_the_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "marker-2f7c1d"

    with TestClient(make_app(data_dir)) as client:
        body = client.get("/health").text

    assert "marker-2f7c1d" not in body
    assert DATABASE_FILENAME not in body
    assert "sqlite" not in body.lower()


def test_a_full_startup_and_shutdown_leaks_nothing_into_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "marker-2f7c1d"
    app = make_app(data_dir)

    with captured_logs(caplog, logging.DEBUG), TestClient(app) as client:
        client.get("/health")

    messages = [record.getMessage() for record in caplog.records]
    for message in messages:
        assert "marker-2f7c1d" not in message
        assert DATABASE_FILENAME not in message
        assert "sqlite+pysqlite" not in message
        assert "sqlite://" not in message
    assert migrator_messages(caplog) == ["database schema upgraded from empty to 0001"]


def test_a_failed_migration_aborts_the_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container never becomes healthy, so ``deploy/deploy.py`` rolls back (AC16)."""

    def failing_open(data_dir: Path) -> Database:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(main_module, "open_database", failing_open)
    app = make_app(tmp_path)

    with pytest.raises(RuntimeError, match="migration failed"), TestClient(app):
        pass

    assert not hasattr(app.state, "database")


# --- T10: empty and migrated volumes (AC15) ------------------------------------------------


def test_two_consecutive_startups_on_the_same_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "volume"
    first = make_app(data_dir)
    second = make_app(data_dir)

    with captured_logs(caplog, logging.INFO):
        with TestClient(first):
            first_revision = current_revision(first.state.database.engine)
        with TestClient(second):
            database = second.state.database
            second_revision = current_revision(database.engine)
            rows = stored_revisions(database.engine)

    assert first_revision == head_revision()
    assert second_revision == head_revision()
    assert rows == [head_revision()]
    assert migrator_messages(caplog) == [
        "database schema upgraded from empty to 0001",
        "database schema already at revision 0001",
    ]


def test_a_startup_over_an_already_migrated_volume(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    app = make_app(data_dir)

    with captured_logs(caplog, logging.INFO), TestClient(app):
        rows = stored_revisions(app.state.database.engine)

    assert rows == [head_revision()]
    assert migrator_messages(caplog) == ["database schema already at revision 0001"]


def test_a_database_migrated_by_a_newer_image_aborts_the_startup(tmp_path: Path) -> None:
    """Rolling an image back after a migration fails loudly instead of writing (§14)."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database, database.session() as session:
        session.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    app = make_app(data_dir)

    with pytest.raises(CommandError, match="9999"), TestClient(app):
        pass

    assert not hasattr(app.state, "database")

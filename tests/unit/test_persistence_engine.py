"""Engine, pragmas, foreign keys and sessions (spec 012, T2, T3, AC3-AC7)."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading_bot.persistence import database as database_module
from trading_bot.persistence import migrator as migrator_module
from trading_bot.persistence.database import Database, open_database
from trading_bot.persistence.engine import (
    BUSY_TIMEOUT_MS,
    create_database_engine,
    create_session_factory,
    database_path,
)
from trading_bot.persistence.migrator import current_revision, head_revision

# The pragmas every connection of a factory engine must report (spec 012, Design 4.2).
EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": BUSY_TIMEOUT_MS,
    "synchronous": 2,  # FULL
}


def read_pragmas(connection: Connection) -> dict[str, object]:
    names = ("journal_mode", "foreign_keys", "busy_timeout", "synchronous")
    return {name: connection.exec_driver_sql(f"PRAGMA {name}").scalar() for name in names}


def create_parent_child_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id))"
        )


def insert_orphan_child(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO child (id, parent_id) VALUES (1, 404)")


# --- T2: engine and pragmas (AC3, AC4) -----------------------------------------------------


def test_the_engine_handles_a_path_with_a_space_and_a_hash(tmp_path: Path) -> None:
    """A hand-built ``sqlite:///{path}`` URL would lose everything after the ``#``."""
    path = database_path(tmp_path / "trading bot #1")

    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM marker").scalar() == 0
    finally:
        engine.dispose()

    assert path.exists()


def test_the_engine_creates_no_file_before_the_first_connection(tmp_path: Path) -> None:
    path = database_path(tmp_path / "state")

    engine = create_database_engine(path)
    try:
        assert path.parent.is_dir()
        assert not path.exists()

        with engine.connect():
            pass

        assert path.exists()
    finally:
        engine.dispose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_a_created_parent_directory_is_private(tmp_path: Path) -> None:
    path = database_path(tmp_path / "outer" / "inner")

    engine = create_database_engine(path)
    engine.dispose()

    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_an_existing_parent_directory_keeps_its_mode(tmp_path: Path) -> None:
    """The container's ``/app/data`` already exists and must not be touched."""
    data_dir = tmp_path / "existing"
    data_dir.mkdir(mode=0o755)

    engine = create_database_engine(database_path(data_dir))
    engine.dispose()

    assert data_dir.stat().st_mode & 0o777 == 0o755


def test_never_echoes_statements(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        assert not engine.echo  # never set, so SQLAlchemy logs no statement and no path
    finally:
        engine.dispose()


def test_the_pragmas_are_set_on_the_first_connection(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        with engine.connect() as connection:
            assert read_pragmas(connection) == EXPECTED_PRAGMAS
    finally:
        engine.dispose()


def test_the_pragmas_are_set_on_a_connection_from_another_thread(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    found: list[dict[str, object]] = []

    def read_from_thread() -> None:
        with engine.connect() as connection:
            found.append(read_pragmas(connection))

    try:
        with engine.connect() as connection:
            read_pragmas(connection)
        thread = threading.Thread(target=read_from_thread)
        thread.start()
        thread.join(timeout=30)
    finally:
        engine.dispose()

    assert found == [EXPECTED_PRAGMAS]


def test_the_pragmas_are_set_again_after_dispose(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        with engine.connect() as connection:
            read_pragmas(connection)

        engine.dispose()

        with engine.connect() as connection:
            assert read_pragmas(connection) == EXPECTED_PRAGMAS
    finally:
        engine.dispose()


def test_the_busy_timeout_keyword_is_honoured(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path), busy_timeout_ms=137)
    try:
        with engine.connect() as connection:
            assert read_pragmas(connection)["busy_timeout"] == 137
    finally:
        engine.dispose()


# --- T3: foreign keys and sessions (AC5, AC6) ----------------------------------------------


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        create_parent_child_schema(engine)

        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            insert_orphan_child(engine)
    finally:
        engine.dispose()


def test_a_bare_engine_accepts_the_same_orphan_row(tmp_path: Path) -> None:
    """Control: the pragma rejects the row, not SQLite itself (spec 012, AC5)."""
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        create_parent_child_schema(engine)

        insert_orphan_child(engine)

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM child").scalar() == 1
    finally:
        engine.dispose()


def test_the_session_factory_keeps_attributes_readable_after_commit(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        factory = create_session_factory(engine)

        assert factory.kw["expire_on_commit"] is False

        with factory() as session:
            assert isinstance(session, Session)
            value = session.execute(text("SELECT 7")).scalar()
            session.commit()
            assert value == 7
    finally:
        engine.dispose()


def recording_database(engine: Engine) -> tuple[Database, list[str]]:
    """A ``Database`` whose sessions record the unit-of-work calls they receive."""
    events: list[str] = []

    class RecordingSession(Session):
        def commit(self) -> None:
            events.append("commit")
            super().commit()

        def rollback(self) -> None:
            events.append("rollback")
            super().rollback()

        def close(self) -> None:
            events.append("close")
            super().close()

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=RecordingSession)
    return Database(engine=engine, session_factory=factory), events


def stored_notes(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT text FROM note ORDER BY id").scalars().all()
    return [str(row) for row in rows]


def create_note_table(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE note (id INTEGER PRIMARY KEY, text TEXT)")


def test_the_session_commits_on_a_clean_exit(tmp_path: Path) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        create_note_table(engine)
        database, events = recording_database(engine)

        with database.session() as session:
            session.execute(text("INSERT INTO note (text) VALUES ('kept')"))

        assert events == ["commit", "close"]
        assert stored_notes(engine) == ["kept"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), KeyboardInterrupt(), asyncio.CancelledError()]
)
def test_the_session_rolls_back_and_re_raises_on_any_base_exception(
    tmp_path: Path, error: BaseException
) -> None:
    engine = create_database_engine(database_path(tmp_path))
    try:
        create_note_table(engine)
        database, events = recording_database(engine)

        with pytest.raises(type(error)), database.session() as session:  # noqa: PT012
            session.execute(text("INSERT INTO note (text) VALUES ('dropped')"))
            raise error

        assert events == ["rollback", "close"]
        assert stored_notes(engine) == []
    finally:
        engine.dispose()


def test_dispose_closes_the_pool_and_leaves_no_wal_file(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    engine = create_database_engine(path)
    create_note_table(engine)
    database, _ = recording_database(engine)
    with database.session() as session:
        session.execute(text("INSERT INTO note (text) VALUES ('kept')"))
    assert path.with_name(path.name + "-wal").exists()

    database.dispose()

    assert path.exists()
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


# --- T3: open_database (AC7) ---------------------------------------------------------------


def test_open_database_migrates_and_returns_a_usable_handle(tmp_path: Path) -> None:
    database = open_database(tmp_path, busy_timeout_ms=200)
    try:
        assert current_revision(database.engine) == head_revision()
        assert database_path(tmp_path).exists()
        with database.session() as session:
            assert session.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar() == 1
    finally:
        database.dispose()


def test_open_database_disposes_the_engine_when_the_migration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed: list[Engine] = []

    def spy_create_engine(path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Engine:
        engine = create_database_engine(path, busy_timeout_ms=busy_timeout_ms)
        event.listen(engine, "engine_disposed", disposed.append)
        return engine

    def failing_migration(engine: Engine) -> str:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(database_module, "create_database_engine", spy_create_engine)
    # ``database.py`` imports the migrator inside the function, so that importing the handle
    # drags no Alembic into the Telegram and API layers (spec 013, AC28): patch it at its home.
    monkeypatch.setattr(migrator_module, "run_migrations", failing_migration)

    with pytest.raises(RuntimeError, match="migration failed"):
        open_database(tmp_path, busy_timeout_ms=200)

    assert len(disposed) == 1

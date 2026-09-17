"""Adversarial cases for the persistence layer (spec 012, T15, AC4-AC8, AC16).

Every case here is a situation a well-behaved caller never creates on purpose, but that the
engine factory, the migrator or the lifespan must still fail loudly (never silently) or handle
safely: a data directory that turns out to be a file, a corrupted database file, contention
between two writers, a WAL reader racing an open write transaction, pathological
``TB_DATA_DIR`` values and misuse of the public API surface (``open_database`` called twice,
``session()`` re-entered, ``Database`` mutated).

No wall-clock or elapsed-time assertions: the busy-timeout case asserts the error class only.
No platform-dependent expectations: Windows does not enforce POSIX directory permissions and
rejects control characters in path components that Linux accepts, so those two cases either
skip on non-POSIX or accept both a clean success and a clean ``OSError``, never a specific
outcome tied to one platform.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import DatabaseError, OperationalError

from trading_bot.config import Settings
from trading_bot.main import create_app
from trading_bot.persistence import database as database_module
from trading_bot.persistence.database import Database, open_database
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.migrator import current_revision, head_revision


@contextmanager
def captured_logs(caplog: pytest.LogCaptureFixture, level: int) -> Iterator[None]:
    """Capture every logger after ``create_app`` has already replaced the root handlers.

    ``configure_logging`` (called from ``create_app``) drops pytest's own capturing handler
    from the root logger, so re-attaching it here, after the app was built, is what makes a
    "no record contains X" assertion meaningful instead of silently observing zero records no
    matter what the code under test logs (mirrors ``test_persistence_startup.py``).
    """
    root = logging.getLogger()
    with caplog.at_level(level):
        caplog.clear()
        root.addHandler(caplog.handler)
        try:
            yield
        finally:
            root.removeHandler(caplog.handler)


# --- A data directory that is a file --------------------------------------------------------


def test_a_data_directory_that_is_a_file_fails_loudly_and_creates_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-a-directory"
    data_dir.write_text("this is a file, not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_database_engine(database_path(data_dir))

    assert data_dir.is_file()  # untouched: mkdir failed before any connection was attempted
    assert data_dir.read_text(encoding="utf-8") == "this is a file, not a directory"


# --- A database file full of garbage ---------------------------------------------------------


def test_a_garbage_database_file_fails_loudly_with_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted file is rejected outright: no table is created, no engine is leaked.

    Unlike ``test_open_database_disposes_the_engine_when_the_migration_fails`` (T3), which
    injects a ``RuntimeError`` to pin the ``try``/``except`` in ``open_database``, this drives
    the same disposal path with a real ``DatabaseError`` raised by the SQLite driver itself, so
    a regression in how that particular exception is handled would still be caught.
    """
    garbage = b"this is not a valid SQLite file, just forty-something bytes of noise"
    path = database_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(garbage)

    disposed: list[Engine] = []

    def spying_create_database_engine(data_dir: Path, *, busy_timeout_ms: int) -> Engine:
        engine = create_database_engine(data_dir, busy_timeout_ms=busy_timeout_ms)
        event.listen(engine, "engine_disposed", disposed.append)
        return engine

    monkeypatch.setattr(database_module, "create_database_engine", spying_create_database_engine)

    with pytest.raises(DatabaseError, match="file is not a database"):
        open_database(tmp_path, busy_timeout_ms=200)

    assert len(disposed) == 1  # AC7: a failed migration disposes the engine before re-raising
    # No partial state: the bytes are exactly what was written, no sidecar file appeared.
    assert path.read_bytes() == garbage
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_a_failed_startup_over_a_garbage_database_puts_no_path_into_our_own_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC16 and AC18 together, through the real lifespan (not a direct ``open_database`` call)."""
    marker = "marker-9a41cd"
    data_dir = tmp_path / marker
    data_dir.mkdir()
    database_path(data_dir).write_bytes(b"garbage, not a database")
    settings = Settings(_env_file=None, data_dir=data_dir)  # type: ignore[call-arg]
    app = create_app(settings)

    with captured_logs(caplog, logging.DEBUG), pytest.raises(DatabaseError), TestClient(app):
        pass

    assert not hasattr(app.state, "database")
    messages = [record.getMessage() for record in caplog.records]
    assert messages != []  # control: the driver's own record does get captured, so this is real
    for message in messages:
        assert marker not in message
        assert "trading_bot.db" not in message


# --- A read-only data directory (skipped where the mode cannot be enforced) -----------------


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX permissions are not reliably enforced on Windows"
)
def test_a_read_only_data_directory_fails_loudly(tmp_path: Path) -> None:
    data_dir = tmp_path / "readonly"
    data_dir.mkdir(mode=0o500)  # r-x------: the directory exists, writing into it is refused
    try:
        with pytest.raises(OperationalError):
            open_database(data_dir, busy_timeout_ms=200)
    finally:
        data_dir.chmod(0o700)  # let pytest clean up tmp_path afterwards


# --- Two writers contending under a short busy timeout ---------------------------------------


def test_two_writers_with_a_short_busy_timeout_raise_the_locked_error_class(
    tmp_path: Path,
) -> None:
    """The error class is asserted, never how long the second writer waited (testing rules)."""
    first = open_database(tmp_path, busy_timeout_ms=50)
    second = open_database(tmp_path, busy_timeout_ms=50)
    try:
        with first.session() as session:
            session.execute(text("CREATE TABLE contended (id INTEGER)"))

        # Hold an uncommitted write transaction open on the first handle.
        holder = first.session_factory()
        holder.execute(text("INSERT INTO contended VALUES (1)"))
        try:
            with pytest.raises(OperationalError, match="locked"), second.session() as session:
                session.execute(text("INSERT INTO contended VALUES (2)"))
        finally:
            holder.rollback()
            holder.close()
    finally:
        first.dispose()
        second.dispose()


# --- A WAL reader racing an open write transaction --------------------------------------------


def test_a_wal_reader_sees_committed_rows_but_not_an_open_writers_uncommitted_one(
    tmp_path: Path,
) -> None:
    path = database_path(tmp_path)
    writer_engine = create_database_engine(path, busy_timeout_ms=200)
    reader_engine = create_database_engine(path, busy_timeout_ms=200)
    try:
        with writer_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE seen (id INTEGER)")
            connection.exec_driver_sql("INSERT INTO seen VALUES (1)")

        writer = writer_engine.connect()
        writer.exec_driver_sql("BEGIN IMMEDIATE")
        writer.exec_driver_sql("INSERT INTO seen VALUES (2)")  # not committed yet
        try:
            with reader_engine.connect() as reader:
                # WAL: the reader is never blocked by the open writer, and sees the last
                # *committed* snapshot, which does not include row 2 yet.
                rows = reader.exec_driver_sql("SELECT id FROM seen ORDER BY id").scalars().all()
            assert rows == [1]
        finally:
            writer.exec_driver_sql("COMMIT")
            writer.close()

        with reader_engine.connect() as reader:
            rows_after_commit = (
                reader.exec_driver_sql("SELECT id FROM seen ORDER BY id").scalars().all()
            )
        assert rows_after_commit == [1, 2]
    finally:
        writer_engine.dispose()
        reader_engine.dispose()


# --- A pathological TB_DATA_DIR: 4 000 characters ---------------------------------------------


def test_a_4000_character_data_dir_is_accepted_by_settings_and_never_truncated(
    tmp_path: Path,
) -> None:
    long_name = "a" * 4000
    settings = Settings(_env_file=None, data_dir=tmp_path / long_name)  # type: ignore[call-arg]

    assert settings.data_dir.is_absolute()
    assert long_name in str(settings.data_dir)  # AC1 strips only surrounding whitespace


def test_a_4000_character_data_dir_fails_with_a_clean_os_error_at_the_filesystem_boundary(
    tmp_path: Path,
) -> None:
    """Whatever the OS' path-length limit is, the failure is a plain ``OSError``, not a crash.

    Windows raises ``FileNotFoundError`` here (no long-path support enabled); Linux raises
    ``OSError: [Errno 36] File name too long`` for a single 4 000-character path component.
    Both are ``OSError``, so this is the platform-independent assertion the testing rules ask
    for; a hand-rolled path-building bug could instead raise an unrelated exception or, worse,
    silently write somewhere else.
    """
    long_name = "a" * 4000
    settings = Settings(_env_file=None, data_dir=tmp_path / long_name)  # type: ignore[call-arg]

    with pytest.raises(OSError):
        create_database_engine(database_path(settings.data_dir))


# --- A path with an embedded newline -----------------------------------------------------------


def test_a_data_dir_with_an_embedded_newline_is_preserved_not_stripped(tmp_path: Path) -> None:
    """Only the ends are stripped (AC1): an internal control character is not whitespace."""
    value = tmp_path / "line1\nline2"

    settings = Settings(_env_file=None, data_dir=value)  # type: ignore[call-arg]

    assert "\n" in str(settings.data_dir)


def test_a_path_with_a_newline_either_migrates_cleanly_or_fails_with_an_os_error(
    tmp_path: Path,
) -> None:
    """Windows rejects control characters in a path component; POSIX filesystems accept them.

    Whichever branch the platform takes, ``open_database`` must not do anything else: no
    partial file, no exception type other than ``OSError``, and a real migration to head when
    it does succeed.
    """
    data_dir = tmp_path / "line1\nline2"

    try:
        database = open_database(data_dir, busy_timeout_ms=200)
    except OSError:
        return
    try:
        assert current_revision(database.engine) == head_revision()
    finally:
        database.dispose()


# --- open_database called twice on the same path -----------------------------------------------


def test_open_database_called_twice_on_one_path_is_safe_and_cross_visible(tmp_path: Path) -> None:
    first = open_database(tmp_path, busy_timeout_ms=200)
    try:
        second = open_database(tmp_path, busy_timeout_ms=200)
        try:
            assert current_revision(second.engine) == head_revision()

            with first.session() as session:
                session.execute(text("CREATE TABLE cross_visible (id INTEGER)"))
                session.execute(text("INSERT INTO cross_visible VALUES (1)"))

            with second.session() as session:
                count = session.execute(text("SELECT COUNT(*) FROM cross_visible")).scalar()
            assert count == 1
        finally:
            second.dispose()
    finally:
        first.dispose()


# --- session() re-entered -----------------------------------------------------------------------


def test_session_can_be_re_entered_and_each_call_is_an_independent_unit_of_work(
    tmp_path: Path,
) -> None:
    database = open_database(tmp_path, busy_timeout_ms=200)
    try:
        with database.session() as outer:
            outer.execute(text("CREATE TABLE nested (id INTEGER)"))

            with database.session() as inner:
                assert inner is not outer
                inner.execute(text("INSERT INTO nested VALUES (1)"))
                # inner commits when this block exits, independently of the outer session

            # The outer session, still open, observes the inner session's committed insert.
            count = outer.execute(text("SELECT COUNT(*) FROM nested")).scalar()
            assert count == 1
    finally:
        database.dispose()


# --- Database is frozen -------------------------------------------------------------------------


def test_database_is_frozen_and_assignment_raises(tmp_path: Path) -> None:
    database = open_database(tmp_path, busy_timeout_ms=200)
    try:
        with pytest.raises(dataclasses.FrozenInstanceError):
            database.engine = database.engine  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            database.session_factory = database.session_factory  # type: ignore[misc]
    finally:
        database.dispose()


def test_database_control_a_plain_mutable_dataclass_accepts_the_same_assignment() -> None:
    """Control: assignment is not rejected for every dataclass, so the asserts above are real."""

    @dataclasses.dataclass  # no frozen=True, unlike Database
    class Mutable:
        value: int

    mutable = Mutable(value=1)
    mutable.value = 2  # does not raise

    assert mutable.value == 2
    assert dataclasses.is_dataclass(Database)

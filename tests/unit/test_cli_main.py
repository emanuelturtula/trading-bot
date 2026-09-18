"""The command-line entry point, its grammar and its refusals (spec 013, T10, AC20-AC22, AC38).

The database is always one built by ``tests/fixtures/database.py`` under ``tmp_path``; the
subprocess checks run ``python -m trading_bot.cli`` exactly as the container does.
"""

from __future__ import annotations

import io
import os
import runpy
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import trading_bot
from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import run_cli
from trading_bot.cli import main as cli_main
from trading_bot.cli.main import _configure_standard_streams as configure_standard_streams
from trading_bot.logging_setup import configure_logging
from trading_bot.persistence.database import Database
from trading_bot.persistence.engine import DATABASE_FILENAME, create_database_engine, database_path
from trading_bot.persistence.migrator import alembic_config
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

SRC = str(Path(trading_bot.__file__).resolve().parent.parent)
BASELINE = "0001"
HEAD = "0002"

SUBCOMMANDS = [
    ("tickers", "list"),
    ("tickers", "add"),
    ("tickers", "remove"),
    ("tickers", "enable"),
    ("tickers", "disable"),
    ("rules", "list"),
    ("rules", "remove"),
    ("rules", "enable"),
    ("rules", "disable"),
    ("assignments", "list"),
    ("assignments", "add"),
    ("assignments", "remove"),
    ("config", "export"),
    ("config", "import"),
]

MUTATING = [
    ["tickers", "add", "AAPL"],
    ["tickers", "enable", "AAPL"],
    ["tickers", "disable", "AAPL"],
    ["tickers", "remove", "AAPL"],
]


def run_module(argv: list[str], *, data_dir: Path) -> subprocess.CompletedProcess[str]:
    """``python -m trading_bot.cli`` in a subprocess, as the container invokes it."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = SRC
    environment["TB_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "-m", "trading_bot.cli", *argv],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )


@contextmanager
def counted_sessions(monkeypatch: pytest.MonkeyPatch, counter: list[int]) -> Iterator[None]:
    """Count how many ``Database.session()`` blocks a command opens (AC38)."""
    original = Database.session

    def counting(self: Database) -> object:
        counter.append(1)
        return original(self)

    monkeypatch.setattr(Database, "session", counting)
    yield


# --- the entry point -----------------------------------------------------------------------


def test_the_module_entry_point_returns_the_exit_code_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__main__.py`` is ``raise SystemExit(main(sys.argv[1:]))`` and nothing else."""
    monkeypatch.setattr(sys, "argv", ["trading_bot.cli", "--help"])

    with pytest.raises(SystemExit) as stop:
        runpy.run_module("trading_bot.cli", run_name="__main__")

    assert stop.value.code == 0


def test_the_standard_streams_are_reconfigured_when_they_support_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision D94: an accented name must not raise on a console that cannot encode it."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    plain = io.StringIO()  # a stream that does not support it is left alone
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", plain)

    configure_standard_streams()

    assert stream.errors == "backslashreplace"
    assert not plain.closed


def test_a_database_that_cannot_be_opened_exits_three(tmp_path: Path) -> None:
    data_dir = tmp_path / "volume"
    data_dir.mkdir()
    (data_dir / DATABASE_FILENAME).write_bytes(b"this is not a database")

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 3
    assert result.errors == ["error: the database cannot be opened"]


def test_a_data_directory_that_cannot_be_opened_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(data_dir: Path) -> Database:
        raise PermissionError("refused")

    monkeypatch.setattr(cli_main, "connect_database", refuse)

    result = run_cli(["tickers", "list"], data_dir=tmp_path)

    assert result.code == 3
    assert result.errors == ["error: the data directory cannot be opened"]


def test_a_database_error_inside_a_command_never_reaches_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement and its parameters would carry a whole rule document (AC26)."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass

    def refuse(*args: object, **kwargs: object) -> None:
        raise IntegrityError("INSERT INTO tickers ...", {"symbol": "SECRET"}, Exception("boom"))

    monkeypatch.setattr(SqlTickerRepository, "add", refuse)

    result = run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: the database refused the change and nothing was written"]
    assert "SECRET" not in result.err
    assert "INSERT" not in result.err


def test_the_module_runs_as_a_subprocess_and_reports_its_help(tmp_path: Path) -> None:
    completed = run_module(["--help"], data_dir=tmp_path)

    assert completed.returncode == 0
    assert "python -m trading_bot.cli" in completed.stdout
    assert "tickers" in completed.stdout


def test_the_module_runs_a_command_as_a_subprocess(tmp_path: Path) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass

    completed = run_module(["tickers", "add", "AAPL"], data_dir=data_dir)

    assert completed.returncode == 0
    assert completed.stdout.splitlines()[-1] == "created ticker AAPL 1d (enabled)"


def test_the_module_reports_a_missing_database_as_an_environment_failure(tmp_path: Path) -> None:
    completed = run_module(["tickers", "list"], data_dir=tmp_path / "empty")

    assert completed.returncode == 3
    assert "revision" in completed.stderr


# --- help and usage errors (AC20) ----------------------------------------------------------


def test_the_program_help_needs_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TB_DATA_DIR", raising=False)

    result = run_cli(["--help"])

    assert result.code == 0
    assert "Manage the tickers, rules and assignments" in result.out


@pytest.mark.parametrize(("group", "name"), SUBCOMMANDS, ids=[f"{g} {n}" for g, n in SUBCOMMANDS])
def test_every_subcommand_has_help_without_a_database(
    monkeypatch: pytest.MonkeyPatch, group: str, name: str
) -> None:
    monkeypatch.delenv("TB_DATA_DIR", raising=False)

    result = run_cli([group, name, "--help"])

    assert result.code == 0
    assert f"{group} {name}" in result.out


def test_the_grammar_holds_exactly_fourteen_subcommands() -> None:
    """Decision D101: five tickers, four rules, three assignments and the config pair."""
    assert len(SUBCOMMANDS) == 14


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-group"),
        pytest.param(["whatever"], id="unknown-group"),
        pytest.param(["tickers"], id="no-command"),
        pytest.param(["tickers", "whatever"], id="unknown-command"),
        pytest.param(["tickers", "list", "--nope"], id="unknown-option"),
        pytest.param(["tickers", "add"], id="missing-argument"),
        pytest.param(["tickers", "add", "AAPL", "--timeframe", "1D"], id="unknown-timeframe"),
        pytest.param(["config", "import", "file", "--on-conflict", "merge"], id="unknown-policy"),
    ],
)
def test_a_usage_error_exits_two(argv: list[str]) -> None:
    result = run_cli(argv)

    assert result.code == 2
    assert result.err != ""


# --- opening the database (AC21, AC22) -----------------------------------------------------


def test_a_missing_database_exits_three_and_creates_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / "never-started"

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 3
    assert result.err == (
        "error: the database schema is at revision none, but revision 0002 is required;"
        " start the application first\n"
    )
    assert not data_dir.exists()


def test_an_unmigrated_database_exits_three_and_leaves_the_revision_untouched(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "volume"
    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=200)
    config = alembic_config()
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, BASELINE)
    finally:
        engine.dispose()

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 3
    assert "0001" in result.err
    assert "0002" in result.err

    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=200)
    try:
        with engine.connect() as connection:
            stored = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()

    assert stored == BASELINE


def test_a_data_directory_that_is_a_file_exits_three(tmp_path: Path) -> None:
    impostor = tmp_path / "not-a-directory"
    impostor.write_text("", encoding="utf-8")

    result = run_cli(["tickers", "list"], data_dir=impostor)

    assert result.code == 3
    assert impostor.read_text(encoding="utf-8") == ""


def test_the_data_dir_option_wins_over_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "chosen"
    ignored = tmp_path / "ignored"
    monkeypatch.setenv("TB_DATA_DIR", str(ignored))
    with temporary_database(chosen):
        pass

    result = run_cli(["tickers", "add", "AAPL"], data_dir=chosen)

    assert result.code == 0
    assert (chosen / DATABASE_FILENAME).is_file()
    assert not ignored.exists()


def test_the_setting_is_used_when_no_option_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    monkeypatch.setenv("TB_DATA_DIR", str(data_dir))

    result = run_cli(["tickers", "add", "AAPL"])

    assert result.code == 0
    assert result.lines == ["created ticker AAPL 1d (enabled)"]


# --- one transaction per command (AC38) ----------------------------------------------------


@pytest.mark.parametrize("argv", MUTATING, ids=[" ".join(argv) for argv in MUTATING])
def test_every_mutating_command_opens_exactly_one_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)
    opened: list[int] = []

    with counted_sessions(monkeypatch, opened):
        result = run_cli(argv, data_dir=data_dir)

    assert result.code in (0, 1)
    assert len(opened) == 1


def test_a_command_emits_no_alembic_record_on_standard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alembic's records describe a migration, which belongs to the application's startup.

    The handler ``configure_logging`` installs writes to ``sys.stderr``, not to the injected
    stream, so the real one is captured here.
    """
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    monkeypatch.setenv("TB_LOG_LEVEL", "INFO")
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    monkeypatch.undo()  # restore the real stream before the handler is rebuilt
    configure_logging("INFO")

    assert result.code == 0
    assert captured.getvalue() == ""
    assert "alembic" not in captured.getvalue()


def test_a_read_only_command_opens_one_session_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    opened: list[int] = []

    with counted_sessions(monkeypatch, opened):
        result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == ""
    assert len(opened) == 1

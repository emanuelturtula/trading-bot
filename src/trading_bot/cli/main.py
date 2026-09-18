"""The configuration command line: parser, dispatch and the shared helpers (spec 013, §9).

``argparse`` from the standard library, so the image needs no extra dependency and no console
script: ``python -m trading_bot.cli`` works inside the container. Commands write through an
injected ``TextIO``, never ``print``, which keeps every message assertable with ``io.StringIO``
and ruff's ``T20`` enabled.

The command line **never migrates** (decision D95): it opens an already-migrated database with
``connect_database`` and refuses anything else with exit code ``3``. Every mutating subcommand
opens exactly one ``Database.session()``; ``--dry-run`` does the whole unit of work and then
raises a private sentinel so that session rolls back (decision D107).
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trading_bot.config import Settings
from trading_bot.domain.timeframe import Timeframe
from trading_bot.logging_setup import configure_logging
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.database import Database, connect_database
from trading_bot.persistence.errors import PersistenceError, SchemaMismatchError

__all__ = ["main"]

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3

MAX_NAME_ECHO = 80  # characters of a rule name echoed in a message (spec 006 D8)

type SubParsers = argparse._SubParsersAction[argparse.ArgumentParser]
type Handler = Callable[["Context", argparse.Namespace], int]


class RejectedError(Exception):
    """The request was refused and nothing was written (exit code ``1``)."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)


class UnusableEnvironmentError(Exception):
    """The data directory, the database or the output file cannot be used (exit code ``3``)."""


class _DryRunRollbackError(Exception):
    """Private sentinel: it rolls the unit of work back without changing #11's contract."""


class Action(StrEnum):
    """What a command did, or would do, to one item."""

    CREATE = "create"
    REPLACE = "replace"  # a conflict resolved in favour of the file (config import)
    ENABLE = "enable"
    DISABLE = "disable"
    SKIP = "skip"
    REMOVE = "remove"
    UNCHANGED = "unchanged"


_PAST_TENSE = {
    Action.CREATE: "created",
    Action.REPLACE: "replaced",
    Action.ENABLE: "enabled",
    Action.DISABLE: "disabled",
    Action.SKIP: "skipped",
    Action.REMOVE: "removed",
    Action.UNCHANGED: "unchanged",
}


@dataclass(frozen=True, slots=True)
class Context:
    """What every command is given: the database, the output stream, a clock and the mode."""

    database: Database
    out: TextIO
    clock: Clock
    dry_run: bool

    def emit(self, line: str) -> None:
        self.out.write(line + "\n")

    def report(self, action: Action, subject: str, detail: str = "") -> None:
        """One report line, in the plain or the conditional form (spec 013, §9.8)."""
        if self.dry_run:
            if action is Action.UNCHANGED:
                line = f"dry run: would leave {subject} unchanged"
            else:
                line = f"dry run: would {action.value} {subject}"
        else:
            line = f"{_PAST_TENSE[action]} {subject}"
        self.emit(f"{line}: {detail}" if detail else line)

    def finish(self) -> int:
        """Close a mutating command, stating that a dry run wrote nothing."""
        if self.dry_run:
            self.emit("dry run: nothing was written")
        return EXIT_OK


@contextmanager
def unit_of_work(context: Context) -> Iterator[Session]:
    """One ``Database.session()``; a dry run rolls it back through the private sentinel."""
    try:
        with context.database.session() as session:
            yield session
            if context.dry_run:
                raise _DryRunRollbackError
    except _DryRunRollbackError:
        return


def timeframe_option(parser: argparse.ArgumentParser) -> None:
    """``--timeframe``, defaulting to the project's default timeframe (spec 004)."""
    parser.add_argument(
        "--timeframe",
        type=Timeframe.parse,
        choices=list(Timeframe),
        default=Timeframe.D1,
        help="candle timeframe (default: 1d)",
    )


def dry_run_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do the whole unit of work and roll it back, writing nothing",
    )


def quoted(name: str) -> str:
    """A rule name as the report shows it: ``repr()``-escaped and bounded.

    A stored name is already 1 to 80 printable characters (spec 006 D8), but the name an
    operator types is untrusted input and is truncated before it reaches a message.
    """
    if len(name) <= MAX_NAME_ECHO:
        return repr(name)
    return repr(name[:MAX_NAME_ECHO] + "...")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def build_parser() -> argparse.ArgumentParser:
    """The whole grammar of spec 013 §9.1, built without touching the environment."""
    # Imported here so the command modules can import this one: the cycle is broken at the
    # only point where it would close, and by the time this runs the module is complete.
    from trading_bot.cli import assignments, config, rules, tickers

    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.cli",
        description="Manage the tickers, rules and assignments of the trading bot.",
        epilog="The bot only notifies signals: it never places orders.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory holding the database (default: the TB_DATA_DIR setting)",
    )
    groups = parser.add_subparsers(dest="group", required=True, metavar="<group>")
    tickers.add_parser(groups)
    rules.add_parser(groups)
    assignments.add_parser(groups)
    config.add_parser(groups)
    return parser


def main(argv: Sequence[str], *, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    """Run one command and return its exit code (spec 013, §9.2)."""
    _configure_standard_streams()
    parser = build_parser()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            args = parser.parse_args(list(argv))
        except SystemExit as stop:  # --help exits 0, a usage error exits 2
            return int(stop.code) if isinstance(stop.code, int) else EXIT_USAGE

    settings = Settings()
    # Before any work, so a library record of this run is redacted too (AC26).
    configure_logging(settings.log_level, settings.secret_values())
    # Alembic's own records describe a migration, which belongs to the application's startup
    # (spec 012, §7.3). This process only reads the stored revision, so its plugin and context
    # chatter would be noise on every command; the application's records are untouched.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    chosen: Path | None = args.data_dir
    data_dir = chosen if chosen is not None else settings.data_dir
    handler: Handler = args.handler
    dry_run = bool(getattr(args, "dry_run", False))

    try:
        database = connect_database(data_dir)
    except SchemaMismatchError as error:
        return _fail(err, f"{error}; start the application first", EXIT_ENVIRONMENT)
    except OSError:
        # The message never names the path: it is infrastructure detail (spec 012, D79).
        return _fail(err, "the data directory cannot be opened", EXIT_ENVIRONMENT)
    except SQLAlchemyError:
        # A file that is not a database, or one another process holds: never a traceback.
        return _fail(err, "the database cannot be opened", EXIT_ENVIRONMENT)
    try:
        context = Context(database=database, out=out, clock=system_clock, dry_run=dry_run)
        return handler(context, args)
    except RejectedError as error:
        return _fail(err, str(error), EXIT_REJECTED)
    except UnusableEnvironmentError as error:
        return _fail(err, str(error), EXIT_ENVIRONMENT)
    except PersistenceError as error:
        return _fail(err, str(error), EXIT_REJECTED)
    except ValueError as error:
        return _fail(err, str(error), EXIT_REJECTED)
    except SQLAlchemyError:
        # Never ``str(error)``: it carries the statement and its parameters, which for a rule
        # would be the whole document. The unit of work rolled back, so nothing was written.
        return _fail(err, "the database refused the change and nothing was written", EXIT_REJECTED)
    finally:
        database.dispose()


def _fail(err: TextIO, message: str, code: int) -> int:
    err.write(f"error: {message}\n")
    return code


def _configure_standard_streams() -> None:
    """Make an accented rule name printable on a console that cannot encode it (D94)."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")

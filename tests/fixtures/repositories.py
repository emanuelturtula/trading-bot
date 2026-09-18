"""Repositories, clocks, rules and the command-line runner for the tests (spec 013, §15.1).

The factory is annotated with the ``Protocol``s, so mypy proves that each implementation
conforms to the port the rest of the application depends on. ``fixed_clock`` returns a
deterministic sequence built from literal instants: no test reads the wall clock, which is what
keeps the Windows gate and the Linux CI in agreement.

Always import this module as ``tests.fixtures.repositories``.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

from sqlalchemy.orm import Session

from tests.fixtures.rules import rule_payload, simple_condition
from trading_bot.cli.main import main
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.schema import Rule, parse_rule
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    RuleRepository,
    TickerRepository,
)
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository


@dataclass(frozen=True, slots=True)
class Repositories:
    """The three ports of one unit of work, typed as the ``Protocol``s the callers see."""

    tickers: TickerRepository
    rules: RuleRepository
    assignments: AssignmentRepository


DEFAULT_START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def repositories(session: Session, *, clock: Clock | None = None) -> Repositories:
    """Build the three repositories over ``session``, sharing one clock."""
    ticks = clock if clock is not None else fixed_clock(DEFAULT_START)
    return Repositories(
        tickers=SqlTickerRepository(session, clock=ticks),
        rules=SqlRuleRepository(session, clock=ticks),
        assignments=SqlAssignmentRepository(session, clock=ticks),
    )


def fixed_clock(start: datetime, step: timedelta = timedelta(seconds=1)) -> Clock:
    """A clock that returns ``start``, then ``start + step``, then ``start + 2 * step``, ..."""
    ticks = count()

    def clock() -> datetime:
        return start + step * next(ticks)

    return clock


def frozen_clock(instant: datetime) -> Clock:
    """A clock that always returns ``instant``."""
    return fixed_clock(instant, timedelta(0))


def sample_rule(name: str = "Sample rule", **overrides: JsonValue) -> Rule:
    """A valid rule built from ``tests.fixtures.rules``, with its name and fields overridable."""
    payload = rule_payload({"all": [simple_condition()]}, name=name, **overrides)
    return parse_rule(payload)


@dataclass(frozen=True, slots=True)
class CliResult:
    """What one command-line run produced: its exit code and both streams."""

    code: int
    out: str
    err: str

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()

    @property
    def errors(self) -> list[str]:
        return self.err.splitlines()


def run_cli(argv: Sequence[str], *, data_dir: Path | None = None) -> CliResult:
    """Run ``trading_bot.cli`` in process with both streams captured."""
    out = io.StringIO()
    err = io.StringIO()
    arguments = list(argv) if data_dir is None else ["--data-dir", str(data_dir), *argv]
    code = main(arguments, out=out, err=err)
    return CliResult(code=code, out=out.getvalue(), err=err.getvalue())


SNAPSHOT_QUERIES = (
    "SELECT id, symbol, timeframe, enabled, created_at FROM tickers ORDER BY id",
    "SELECT id, name, signal, timeframe, definition_json, enabled, created_at, updated_at"
    " FROM rules ORDER BY id",
    "SELECT ticker_id, rule_id, timeframe, created_at FROM ticker_rules"
    " ORDER BY ticker_id, rule_id",
    "SELECT name, seq FROM sqlite_sequence ORDER BY name",
)


def database_snapshot(data_dir: Path) -> str:
    """Every row of the three tables **and** ``sqlite_sequence``, as stable text (AC37, V13).

    ``sqlite_sequence`` is part of the snapshot on purpose: a dry run must not burn an
    identifier, which is what would happen if the rollback did not cover it.
    """
    engine = create_database_engine(database_path(data_dir), busy_timeout_ms=200)
    parts: list[str] = []
    try:
        with engine.connect() as connection:
            for query in SNAPSHOT_QUERIES:
                rows = [repr(tuple(row)) for row in connection.exec_driver_sql(query)]
                parts.append("\n".join([query, *rows]))
    finally:
        engine.dispose()
    return "\n".join(parts)

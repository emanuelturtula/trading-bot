"""Repositories, clocks, rules, signals and the command-line runner (spec 013, 15.1; 014, 15).

The factory is annotated with the ``Protocol``s, so mypy proves that each implementation
conforms to the port the rest of the application depends on. ``fixed_clock`` returns a
deterministic sequence built from literal instants: no test reads the wall clock, which is what
keeps the Windows gate and the Linux CI in agreement.

Always import this module as ``tests.fixtures.repositories``.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

from sqlalchemy.orm import Session

from tests.fixtures.rules import rule_payload, simple_condition
from trading_bot.cli.main import main
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.schema import Rule, parse_rule
from trading_bot.domain.signals import Side, Signal
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.engine import create_database_engine, database_path
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.repositories.protocols import (
    AssignmentRepository,
    BotStateRepository,
    RuleRepository,
    SignalRepository,
    TickerRepository,
)
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository


@dataclass(frozen=True, slots=True)
class Repositories:
    """The five ports of one unit of work, typed as the ``Protocol``s the callers see."""

    tickers: TickerRepository
    rules: RuleRepository
    assignments: AssignmentRepository
    signals: SignalRepository
    state: BotStateRepository


DEFAULT_START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
# A representative ``1d`` nominal close: 00:00 New York of the next session, in UTC. It is the
# default because most stored signals are daily; a caller that needs another candle, another
# timeframe or an instant off every grid passes ``candle_close_ts``.
SAMPLE_CLOSE = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
SAMPLE_VALUES: Mapping[str, float] = {"rsi(length=14).value": 28.4, "close.value": 187.5}


def repositories(session: Session, *, clock: Clock | None = None) -> Repositories:
    """Build the five repositories over ``session``, sharing one clock."""
    ticks = clock if clock is not None else fixed_clock(DEFAULT_START)
    return Repositories(
        tickers=SqlTickerRepository(session, clock=ticks),
        rules=SqlRuleRepository(session, clock=ticks),
        assignments=SqlAssignmentRepository(session, clock=ticks),
        signals=SqlSignalRepository(session, clock=ticks),
        state=SqlBotStateRepository(session, clock=ticks),
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


def sample_signal(
    ticker: StoredTicker,
    rule: StoredRule,
    *,
    candle_close_ts: datetime = SAMPLE_CLOSE,
    close_price: float = 187.5,
    indicator_values: Mapping[str, float] | None = None,
    side: Side | None = None,
) -> Signal:
    """A signal for two stored rows, with synthetic values (user decision D2).

    ``rule_id`` is ``str(rule.id)``, which is what the engine builds it from (decision D114).
    """
    return Signal(
        ticker=ticker.symbol,
        timeframe=ticker.timeframe,
        rule_id=str(rule.id),
        side=side if side is not None else rule.rule.signal,
        candle_close_ts=candle_close_ts,
        close_price=close_price,
        indicator_values=SAMPLE_VALUES if indicator_values is None else indicator_values,
    )


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
    "SELECT id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
    " indicator_values_json, created_at, notified_at FROM signals ORDER BY id",
    'SELECT "key", value, updated_at FROM bot_state ORDER BY "key"',
    "SELECT name, seq FROM sqlite_sequence ORDER BY name",
)


def database_snapshot(data_dir: Path) -> str:
    """Every row of the five tables **and** ``sqlite_sequence``, as stable text (AC37, V13).

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

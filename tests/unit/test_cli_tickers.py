"""The ticker subcommands (spec 013, T17, AC21, AC33, AC36, AC37; spec 014, T11, AC23).

Every expected line is a literal, never recomputed with the code under test, and every dry run
is checked against a full dump of the five tables plus ``sqlite_sequence``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import (
    database_snapshot,
    repositories,
    run_cli,
    sample_rule,
    sample_signal,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

SIGNAL_CLOSE = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)

DRY_RUNS = [
    ["tickers", "add", "MSFT"],
    ["tickers", "remove", "AAPL", "--force"],
    ["tickers", "enable", "NVDA"],
    ["tickers", "disable", "AAPL"],
]


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def seeded(tmp_path: Path) -> Path:
    """AAPL 1d assigned to one rule, NVDA 1d disabled and a second unassigned rule."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        handles.tickers.add("NVDA", Timeframe.D1, enabled=False)
        rule = handles.rules.add(sample_rule("Daily breakout"))
        handles.rules.add(sample_rule("Unassigned rule"))
        handles.assignments.assign(aapl.id, rule.id)


# --- add -----------------------------------------------------------------------------------


def test_add_creates_an_enabled_daily_ticker(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["tickers", "add", "AAPL"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["created ticker AAPL 1d (enabled)"]


def test_add_honours_the_timeframe_and_the_disabled_flag(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(
        ["tickers", "add", "AAPL", "--timeframe", "1h", "--disabled"], data_dir=data_dir
    )

    assert result.code == 0
    assert result.lines == ["created ticker AAPL 1h (disabled)"]


def test_add_normalizes_the_symbol(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["tickers", "add", "  aapl  "], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["created ticker AAPL 1d (enabled)"]


def test_add_refuses_an_existing_pair(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "add", "aapl"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: ticker AAPL 1d already exists"]


def test_add_refuses_a_malformed_symbol(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["tickers", "add", "AA PL"], data_dir=data_dir)

    assert result.code == 1
    assert result.err.startswith("error: invalid ticker")
    assert run_cli(["tickers", "list"], data_dir=data_dir).out == ""


# --- remove --------------------------------------------------------------------------------


def test_remove_deletes_a_ticker_without_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "remove", "NVDA"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed ticker NVDA 1d"]


def test_remove_refuses_a_ticker_with_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "remove", "AAPL"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: ticker AAPL 1d has 1 assignment; pass --force to remove them with it"
    ]
    assert run_cli(["assignments", "list"], data_dir=data_dir).lines == [
        "AAPL 1d -> 'Daily breakout'"
    ]


def test_remove_with_force_reports_the_assignments_it_took(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "remove", "AAPL", "--force"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed ticker AAPL 1d with 1 assignment"]
    assert run_cli(["assignments", "list"], data_dir=data_dir).out == ""
    assert run_cli(["rules", "list"], data_dir=data_dir).lines == [
        "'Daily breakout' 1d BUY disabled 0 tickers",
        "'Unassigned rule' 1d BUY disabled 0 tickers",
    ]


def test_remove_refuses_a_missing_ticker(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "remove", "TSLA"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no ticker TSLA 1d is stored"]


# --- enable and disable --------------------------------------------------------------------


def test_enable_switches_a_disabled_ticker(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "enable", "NVDA"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["enabled ticker NVDA 1d"]


def test_enable_on_an_enabled_ticker_is_reported_as_unchanged(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "enable", "AAPL"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["unchanged ticker AAPL 1d (enabled)"]


def test_disable_switches_an_enabled_ticker(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "disable", "AAPL"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["disabled ticker AAPL 1d"]


def test_disable_on_a_missing_ticker_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "disable", "TSLA", "--timeframe", "4h"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no ticker TSLA 4h is stored"]


# --- list ----------------------------------------------------------------------------------


def test_list_prints_nothing_on_an_empty_database(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == ""


def test_list_is_ordered_by_symbol_and_timeframe(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    run_cli(["tickers", "add", "AAPL", "--timeframe", "1h"], data_dir=data_dir)

    result = run_cli(["tickers", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "AAPL 1d enabled 1 rule",
        "AAPL 1h enabled 0 rules",
        "NVDA 1d disabled 0 rules",
    ]


# --- dry runs (AC37) -----------------------------------------------------------------------


@pytest.mark.parametrize("argv", DRY_RUNS, ids=[" ".join(argv) for argv in DRY_RUNS])
def test_a_dry_run_leaves_the_database_byte_identical(tmp_path: Path, argv: list[str]) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli([*argv, "--dry-run"], data_dir=data_dir)

    assert result.code == 0
    assert database_snapshot(data_dir) == before


def test_a_dry_run_uses_the_conditional_form(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "add", "MSFT", "--dry-run"], data_dir=data_dir)

    assert result.lines == [
        "dry run: would create ticker MSFT 1d (enabled)",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_removal_reports_the_assignments(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "remove", "AAPL", "--force", "--dry-run"], data_dir=data_dir)

    assert result.lines == [
        "dry run: would remove ticker AAPL 1d with 1 assignment",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_flag_change_names_the_flag(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "enable", "NVDA", "--dry-run"], data_dir=data_dir)

    assert result.lines == [
        "dry run: would enable ticker NVDA 1d",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_no_op_uses_the_unchanged_form(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["tickers", "enable", "AAPL", "--dry-run"], data_dir=data_dir)

    assert result.lines == [
        "dry run: would leave ticker AAPL 1d (enabled) unchanged",
        "dry run: nothing was written",
    ]


def test_a_dry_run_of_a_rejected_request_still_exits_one(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli(["tickers", "remove", "AAPL", "--dry-run"], data_dir=data_dir)

    assert result.code == 1
    assert database_snapshot(data_dir) == before


def test_a_dry_run_does_not_burn_an_identifier(tmp_path: Path) -> None:
    """``sqlite_sequence`` rolls back too, so the next real add gets the same id (D107)."""
    data_dir = seeded(tmp_path)

    run_cli(["tickers", "add", "MSFT", "--dry-run"], data_dir=data_dir)
    run_cli(["tickers", "add", "MSFT"], data_dir=data_dir)

    with temporary_database(data_dir) as database, database.session() as session:
        stored = repositories(session).tickers.get_by_symbol("MSFT", Timeframe.D1)

    assert stored is not None
    assert stored.id == 3


# --- removal with a signal history (spec 014, T11, AC23) ----------------------------------


def with_signals(tmp_path: Path, count: int = 2) -> Path:
    """The seeded database plus ``count`` signals on AAPL 1d, and one on NVDA 1d."""
    data_dir = seeded(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        tools = repositories(session)
        aapl = tools.tickers.get_by_symbol("AAPL", Timeframe.D1)
        nvda = tools.tickers.get_by_symbol("NVDA", Timeframe.D1)
        rule = tools.rules.get_by_name("Daily breakout")
        assert aapl is not None
        assert nvda is not None
        assert rule is not None
        for index in range(count):
            tools.signals.record(
                sample_signal(aapl, rule, candle_close_ts=SIGNAL_CLOSE + timedelta(days=index))
            )
        tools.signals.record(sample_signal(nvda, rule))
    return data_dir


def test_remove_refuses_a_ticker_with_assignments_and_signals(tmp_path: Path) -> None:
    data_dir = with_signals(tmp_path)

    result = run_cli(["tickers", "remove", "AAPL"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: ticker AAPL 1d has 1 assignment and 2 signals; pass --force to remove them with it"
    ]


def test_remove_refuses_a_ticker_that_only_has_signals(tmp_path: Path) -> None:
    """Without counting signals, a year of history would vanish without ``--force``."""
    data_dir = with_signals(tmp_path)

    result = run_cli(["tickers", "remove", "NVDA"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == [
        "error: ticker NVDA 1d has 1 signal; pass --force to remove them with it"
    ]


def test_remove_with_force_reports_the_assignments_and_the_signals(tmp_path: Path) -> None:
    data_dir = with_signals(tmp_path)

    result = run_cli(["tickers", "remove", "AAPL", "--force"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed ticker AAPL 1d with 1 assignment and 2 signals"]
    assert run_cli(["signals", "list"], data_dir=data_dir).lines == [
        "NVDA|1d|1|2024-01-03T05:00:00+00:00 BUY 'Daily breakout' close 187.5"
        " (id 3, recorded 2026-01-02T03:04:07+00:00, not notified)"
    ]


def test_remove_with_force_reports_a_history_without_assignments(tmp_path: Path) -> None:
    data_dir = with_signals(tmp_path)

    result = run_cli(["tickers", "remove", "NVDA", "--force"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed ticker NVDA 1d with 1 signal"]


def test_a_dry_run_of_a_removal_reports_the_signals_and_writes_nothing(tmp_path: Path) -> None:
    data_dir = with_signals(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli(["tickers", "remove", "AAPL", "--force", "--dry-run"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [
        "dry run: would remove ticker AAPL 1d with 1 assignment and 2 signals",
        "dry run: nothing was written",
    ]
    assert database_snapshot(data_dir) == before


def test_a_refused_removal_leaves_the_signals_untouched(tmp_path: Path) -> None:
    data_dir = with_signals(tmp_path)
    before = database_snapshot(data_dir)

    result = run_cli(["tickers", "remove", "AAPL"], data_dir=data_dir)

    assert result.code == 1
    assert database_snapshot(data_dir) == before


def test_a_ticker_with_neither_assignments_nor_signals_is_removed_without_force(
    tmp_path: Path,
) -> None:
    data_dir = with_signals(tmp_path)
    run_cli(["tickers", "add", "MSFT"], data_dir=data_dir)

    result = run_cli(["tickers", "remove", "MSFT"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ["removed ticker MSFT 1d"]
